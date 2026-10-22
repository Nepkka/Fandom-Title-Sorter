from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from PIL import Image, ImageFile

from ..config import Settings
from ..domain.decision import DecisionSettings, decide
from ..domain.models import (
    ORIGINAL_TAG,
    Action,
    Decision,
    Fandom,
    ImageMeta,
    SearchResult,
    Source,
    TargetKind,
)
from ..domain.naming import display_character, normalize_key
from ..domain.rules import FandomMap
from ..domain.similar import closest_fandom, fandom_name_from_tag, is_junk_tag
from ..net.ratelimit import BudgetExhausted, SearchUnavailable
from ..providers.base import DiscoveryHit
from ..storage.db import Database
from ..storage.index import PhashIndex
from ..storage.paths import (
    MovePlan,
    hash_file,
    image_size,
    is_junk_file,
    long_path,
    perceptually_hash,
    plan_and_move,
    remove_file,
    size_of,
)
from . import events

UPLOAD_TARGET_BYTES = 1_200_000

NEIGHBOUR_LIMIT = 20

logger = logging.getLogger(__name__)

EventHandler = Callable[[events.RunEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RunOptions:
    input_root: str
    output_root: str
    sep_characters: bool = True
    move: bool = True
    use_saucenao: bool = True
    limit: int | None = None
    min_similarity: float = 0.55
    review_below_confidence: float = 0.5
    phash_distance: int = 6
    rescan_unknown: bool = False


@dataclass
class FileOutcome:
    path: str
    image_id: int | None
    decision: Decision
    target: str | None = None
    moved: bool = False
    reused: bool = False
    deleted: bool = False
    signals: list[SearchResult] = field(default_factory=list)
    hits: list[DiscoveryHit] = field(default_factory=list)
    elapsed: float = 0.0
    note: str = ""


@dataclass
class RunStats:
    run_id: str
    total: int = 0
    moved: int = 0
    review: int = 0
    unknown: int = 0
    failed: int = 0
    reused: int = 0
    deferred: int = 0
    deleted: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


def prepare_upload(path: str, target_bytes: int = UPLOAD_TARGET_BYTES) -> bytes:
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    with Image.open(long_path(path)) as image:
        source = image
        icc = image.info.get("icc_profile")
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            source = background
        elif image.mode != "RGB":
            source = image.convert("RGB")

        buffer = io.BytesIO()
        quality = 90
        while True:
            buffer.seek(0)
            buffer.truncate(0)
            source.save(
                buffer,
                format="JPEG",
                quality=quality,
                **({"icc_profile": icc} if icc else {}),
            )
            if buffer.tell() <= target_bytes or quality <= 55:
                break
            quality -= 10
        return buffer.getvalue()


class Sorter:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        fandom_map: FandomMap,
        *,
        engines: Iterable[object] = (),
        boards: dict[Source, object] | None = None,
        saucenao: object | None = None,
        board_route: Callable[[str], bool] | None = None,
        topup: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._map = fandom_map
        self._engines = list(engines)
        self._boards = boards or {}
        self._saucenao = saucenao
        self._board_route = board_route
        self._topup = topup
        self._index: PhashIndex | None = None
        self._reachable: dict[Source, object] | None = None
        self._learn_lock = asyncio.Lock()
        self._skipped: set[str] = set()
        self._topped_up: set[str] = set()
        self._topup_lock = asyncio.Lock()

    def _reachable_boards(self) -> dict[Source, object]:
        if self._reachable is not None:
            return self._reachable
        if self._board_route is None:
            self._reachable = dict(self._boards)
            return self._reachable
        reachable: dict[Source, object] = {}
        for source, board in self._boards.items():
            hosts = getattr(board, "hosts", None) or (str(getattr(board, "host", "")),)
            if any(self._board_route(host) for host in hosts):
                reachable[source] = board
        self._reachable = reachable
        return reachable

    async def process(
        self,
        path: str,
        options: RunOptions,
        run_id: str,
        cancel: asyncio.Event | None = None,
    ) -> FileOutcome:
        started = time.monotonic()
        if await asyncio.to_thread(is_junk_file, path):
            return self._drop_junk(path, options, started)

        decision_settings = DecisionSettings(
            min_similarity=options.min_similarity,
            review_below_confidence=options.review_below_confidence,
        )

        size = size_of(path)
        mtime = os.path.getmtime(long_path(path))

        cached = self._db.decision_for_unchanged(path, size, mtime)
        if cached is not None:
            cached_id, cached_decision = cached
            rescan = options.rescan_unknown and cached_decision.kind is TargetKind.UNKNOWN
            if not rescan:
                outcome = FileOutcome(
                    path=path,
                    image_id=cached_id,
                    decision=cached_decision,
                    reused=True,
                    elapsed=time.monotonic() - started,
                    note="решение из кэша (файл не менялся)",
                )
                await self._finalize(outcome, options, run_id)
                return outcome

        phash = await asyncio.to_thread(perceptually_hash, path)
        width, height = await asyncio.to_thread(image_size, path)
        meta = ImageMeta(
            path=path,
            size=size,
            width=width,
            height=height,
            mtime=mtime,
            md5=await asyncio.to_thread(hash_file, path, "md5"),
            phash=phash,
            ext=os.path.splitext(path)[1].lower(),
        )
        image_id = self._db.upsert_image(meta)
        self._phash_index().add(image_id, phash)

        reused = await asyncio.to_thread(self._reuse_from_index, image_id, phash, options)
        if reused is not None:
            outcome = FileOutcome(
                path=path,
                image_id=image_id,
                decision=reused,
                reused=True,
                elapsed=time.monotonic() - started,
                note="взято от похожей картинки в библиотеке",
            )
            await self._finalize(outcome, options, run_id)
            return outcome

        hits = self._filename_hits(path)
        asked: list[str] = []
        for hit in hits:
            if hit.source is not None and str(hit.source) not in asked:
                asked.append(str(hit.source))
        signals: list[SearchResult] = self._hits_to_signals(
            [hit for hit in hits if hit.has_tags]
        )
        by_name = self._name_decision(signals, decision_settings)
        if by_name is not None:
            outcome = FileOutcome(
                path=path,
                image_id=image_id,
                decision=by_name,
                signals=signals,
                hits=hits,
                elapsed=time.monotonic() - started,
                note="определено по имени файла",
            )
            await self._finalize(outcome, options, run_id)
            return outcome

        unavailable: list[str] = []
        skipped: list[str] = []
        note = ""
        image_bytes: bytes | None = None

        reachable = self._reachable_boards()
        if not reachable:
            note = "борды недоступны: рабочих прокси нет"

        if cancel is not None and cancel.is_set():
            return self._stopped(path, options, image_id, started)

        if reachable and meta.md5:
            found = await asyncio.gather(
                *(board.by_md5(meta.md5) for board in reachable.values())
            )
            for result in found:
                if result is not None:
                    signals.append(result)

        signals.extend(await self._resolve_hits(hits, reachable))

        if not self._knows_fandom(signals) and self._engines:
            for engine in self._engines:
                name = str(getattr(engine, "name", "поиск"))
                if name in self._skipped:
                    if name not in skipped:
                        skipped.append(name)
                    continue
                if image_bytes is None:
                    image_bytes = await self._upload(path)
                    if image_bytes is None:
                        return self._unreadable(path, image_id, started)
                try:
                    found = await self._engine_search(engine, name, image_bytes, phash)
                except BudgetExhausted as error:
                    self._skip(name, error)
                    skipped.append(name)
                    continue
                except SearchUnavailable as error:
                    unavailable.append(str(error))
                    continue
                if name not in asked:
                    asked.append(name)
                hits.extend(found)
                signals.extend(
                    self._hits_to_signals(
                        [hit for hit in found if hit.has_tags or hit.raw_tags]
                    )
                )
                signals.extend(await self._resolve_hits(found, reachable))
                if self._knows_fandom(signals):
                    break

        if not self._knows_fandom(signals) and self._saucenao is not None:
            if options.use_saucenao:
                if cancel is not None and cancel.is_set():
                    return self._stopped(path, options, image_id, started)
                if image_bytes is None:
                    image_bytes = await self._upload(path)
                    if image_bytes is None:
                        return self._unreadable(path, image_id, started)
                try:
                    found = await self._saucenao.search(image_bytes, phash=phash)
                    if "saucenao" not in asked:
                        asked.append("saucenao")
                    hits.extend(found)
                    signals.extend(self._hits_to_signals(found))
                except BudgetExhausted as error:
                    return await self._deferred(
                        path,
                        image_id,
                        signals,
                        hits,
                        options,
                        run_id,
                        started,
                        f"Суточный запас SauceNAO исчерпан: {error}",
                    )
                except SearchUnavailable as error:
                    unavailable.append(str(error))
            else:
                note = "SauceNAO выключен в настройках"

        if unavailable and not self._knows_fandom(signals):
            return await self._deferred(
                path,
                image_id,
                signals,
                hits,
                options,
                run_id,
                started,
                "Источник не ответил, картинку не спросили: " + "; ".join(unavailable),
            )

        learned = await self._learn_fandom(signals, path, options.min_similarity)
        decision = decide(signals, self._map, decision_settings)
        self._teach_tags(signals, decision)
        self._note_unmatched(signals, path)
        if learned:
            note = "; ".join(
                part for part in (note, f"новый фандом «{learned}» добавлен в словарь") if part
            )
        if skipped and decision.kind is TargetKind.UNKNOWN:
            note = "; ".join(
                part
                for part in (
                    note,
                    "не спросили: "
                    + ", ".join(sorted(set(skipped)))
                    + " — суточный запас исчерпан, файл можно перепроверить позже",
                )
                if part
            )
        if unavailable:
            note = "; ".join(
                part
                for part in (
                    note,
                    "не ответили: " + "; ".join(dict.fromkeys(unavailable)),
                )
                if part
            )
        if decision.kind is TargetKind.UNKNOWN and asked:
            note = "; ".join(
                part
                for part in (
                    note,
                    "спросили: " + ", ".join(dict.fromkeys(asked)) + " — ответа с тегами нет",
                )
                if part
            )
        outcome = FileOutcome(
            path=path,
            image_id=image_id,
            decision=decision,
            signals=signals,
            hits=hits,
            elapsed=time.monotonic() - started,
            note=note,
        )
        await self._finalize(outcome, options, run_id)
        return outcome

    async def _upload(self, path: str) -> bytes | None:
        try:
            return await asyncio.to_thread(prepare_upload, path)
        except Exception as error:  # noqa: BLE001
            logger.warning("файл не читается: %s — %s", path, error)
            return None

    async def _unreadable(
        self, path: str, image_id: int | None, started: float
    ) -> FileOutcome:
        decision = Decision(
            kind=TargetKind.UNKNOWN,
            confidence=0.0,
            reason="Не удалось прочитать файл",
        )
        return FileOutcome(
            path=path,
            image_id=image_id,
            decision=decision,
            elapsed=time.monotonic() - started,
            note="файл не читается",
        )

    async def _engine_search(
        self, engine: object, name: str, image: bytes, phash: int | None
    ) -> list[DiscoveryHit]:
        try:
            return await engine.search(image, phash=phash)
        except BudgetExhausted:
            if not await self._top_up(str(getattr(engine, "host", ""))):
                raise
            return await engine.search(image, phash=phash)

    async def _top_up(self, host: str) -> bool:
        if self._topup is None or not host or host in self._topped_up:
            return False
        async with self._topup_lock:
            if host in self._topped_up:
                return False
            self._topped_up.add(host)
            started = time.monotonic()
            try:
                added = await asyncio.wait_for(
                    self._topup(host), timeout=self._settings.fts_topup_timeout
                )
            except Exception as error:  # noqa: BLE001
                logger.warning("%s: адресов сверх запаса не добрать — %s", host, error)
                return False
            logger.info(
                "%s: запас кончился, проверено ещё адресов: %s за %.1f с",
                host,
                "есть новые" if added else "новых нет",
                time.monotonic() - started,
            )
            return added

    def _skip(self, name: str, error: Exception) -> None:
        if name in self._skipped:
            return
        self._skipped.add(name)
        logger.warning(
            "%s: суточный запас исчерпан (%s) — источник пропущен до конца прогона",
            name,
            error,
        )

    async def _deferred(
        self,
        path: str,
        image_id: int,
        signals: list[SearchResult],
        hits: list[DiscoveryHit],
        options: RunOptions,
        run_id: str,
        started: float,
        reason: str,
    ) -> FileOutcome:
        self._db.enqueue(image_id, reason)
        if image_id is not None:
            self._db.forget_decision(image_id)
        decision = Decision(
            kind=TargetKind.UNKNOWN,
            confidence=0.0,
            reason=f"{reason} — файл отложен",
        )
        outcome = FileOutcome(
            path=path,
            image_id=image_id,
            decision=decision,
            signals=signals,
            hits=hits,
            elapsed=time.monotonic() - started,
            note="отложено до завтра",
        )
        await self._finalize(outcome, options, run_id, defer=True)
        return outcome

    def _stopped(
        self, path: str, options: RunOptions, image_id: int, started: float
    ) -> FileOutcome:
        decision = Decision(
            kind=TargetKind.UNKNOWN,
            confidence=0.0,
            reason="Прогон остановлен",
        )
        return FileOutcome(
            path=path,
            image_id=image_id,
            decision=decision,
            elapsed=time.monotonic() - started,
            note="остановлено",
        )

    def _drop_junk(self, path: str, options: RunOptions, started: float) -> FileOutcome:
        reason = "Файл недокачан или не картинка"
        decision = Decision(
            kind=TargetKind.UNKNOWN,
            action=Action.REVIEW,
            reason=reason,
        )
        if not options.move:
            return FileOutcome(
                path=path,
                image_id=None,
                decision=decision,
                elapsed=time.monotonic() - started,
                note=reason + " — оставлен на месте",
            )
        removed = remove_file(path)
        note = reason + (" — удалён" if removed else " — не удалось удалить")
        logger.info("%s — %s", path, note)
        return FileOutcome(
            path=path,
            image_id=None,
            decision=decision,
            deleted=removed,
            elapsed=time.monotonic() - started,
            note=note,
        )

    def _knows_fandom(self, signals: list[SearchResult]) -> bool:
        """Знаем, куда класть: есть фандом в словаре или автор для Original."""
        for signal in signals:
            if signal.artist_tags and signal.source is not Source.PIXIV:
                return True
            for tag in signal.fandom_tags:
                if normalize_key(tag) == ORIGINAL_TAG or self._map.resolve(tag).best:
                    return True
        return False

    def _characters(self, outcome: FileOutcome) -> list[str]:
        fandom = outcome.decision.fandom
        names: list[str] = []
        for signal in outcome.signals:
            if fandom is not None and not any(
                (best := self._map.resolve(tag).best) is not None
                and best.fandom.name == fandom
                for tag in signal.fandom_tags
            ):
                continue
            names.extend(display_character(tag) for tag in signal.character_tags)
        return list(dict.fromkeys(names))

    def _teach_tags(self, signals: list[SearchResult], decision: Decision) -> None:
        fandom = decision.fandom
        if not fandom or decision.kind is TargetKind.UNKNOWN or decision.action is not Action.MOVE:
            return
        pairs = [
            (tag, fandom)
            for signal in signals
            if signal.source is Source.PIXIV
            for tag in signal.raw_tags
            if normalize_key(tag) != ORIGINAL_TAG and not is_junk_tag(tag)
        ]
        if pairs:
            self._db.learn_tags(pairs)

    def _note_unmatched(self, signals: list[SearchResult], path: str) -> None:
        unknown = [
            tag
            for signal in signals
            for tag in signal.fandom_tags
            if normalize_key(tag) != ORIGINAL_TAG
            and not is_junk_tag(tag)
            and not self._map.resolve(tag).matches
        ]
        if unknown:
            self._db.record_unmatched(unknown, path)

    def _new_tag(self, signals: list[SearchResult], min_similarity: float) -> str | None:
        if not self._settings.fts_auto_fandom:
            return None
        counts = self._count_new_tags(signals, min_similarity, fallback=False)
        if not counts:
            counts = self._count_new_tags(signals, min_similarity, fallback=True)
        if not counts:
            return None
        return max(counts.values(), key=lambda item: (item[1], len(item[0])))[0]

    def _count_new_tags(
        self, signals: list[SearchResult], min_similarity: float, fallback: bool
    ) -> dict[str, tuple[str, int]]:
        counts: dict[str, tuple[str, int]] = {}
        for signal in signals:
            if signal.similarity < min_similarity:
                continue
            if (signal.source is Source.TRACEMOE) is not fallback:
                continue
            tags = signal.fandom_tags
            if signal.source is Source.TRACEMOE:
                tags = tags[:1]
            for tag in tags:
                if normalize_key(tag) == ORIGINAL_TAG or is_junk_tag(tag):
                    continue
                if self._map.resolve(tag).matches:
                    continue
                key = normalize_key(tag)
                seen, count = counts.get(key, (tag, 0))
                counts[key] = (seen, count + 1)
        return counts

    async def _learn_fandom(
        self, signals: list[SearchResult], path: str, min_similarity: float
    ) -> str | None:
        tag = self._new_tag(signals, min_similarity)
        if not tag:
            return None
        name = fandom_name_from_tag(tag)
        if not name or self._map.resolve(name).matches:
            return None
        async with self._learn_lock:
            if self._map.resolve(tag).matches:
                return None
            similar_to, score = closest_fandom(tag, self._db.load_rules())
            if not self._db.add_fandom(Fandom(name=name, aliases=(tag,))):
                self._db.add_alias(name, tag)
            self._map = FandomMap(self._db.load_rules())
            if score >= self._settings.fts_fandom_similarity:
                self._db.record_suggestion(tag, name, similar_to, score)
                logger.info(
                    "новый фандом «%s» похож на «%s» (%.2f) — на уточнение: %s",
                    name,
                    similar_to,
                    score,
                    path,
                )
            else:
                logger.info("в словарь добавлен фандом «%s» по тегу %s: %s", name, tag, path)
            return name

    def _filename_hits(self, path: str) -> list[DiscoveryHit]:
        from ..providers.filename import resolve_from_filename

        return resolve_from_filename(path, self._map)

    def _name_decision(
        self, signals: list[SearchResult], settings: DecisionSettings
    ) -> Decision | None:
        if not signals:
            return None
        for signal in signals:
            for tag in signal.fandom_tags:
                if normalize_key(tag) == ORIGINAL_TAG:
                    continue
                if self._map.resolve(tag).best is None:
                    return None
        decision = decide(signals, self._map, settings)
        if decision.action is not Action.MOVE:
            return None
        if decision.kind is TargetKind.FANDOM:
            return decision if decision.fandom and (
                decision.character or decision.multi_character
            ) else None
        if decision.kind is TargetKind.ORIGINAL and decision.artist:
            return decision
        return None

    def _named_first(self, targets: list[str], options: RunOptions) -> list[str]:
        settings = DecisionSettings(
            min_similarity=options.min_similarity,
            review_below_confidence=options.review_below_confidence,
        )
        named = {
            path
            for path in targets
            if self._name_decision(
                self._hits_to_signals(
                    [hit for hit in self._filename_hits(path) if hit.has_tags]
                ),
                settings,
            )
            is not None
        }
        if not named:
            return targets
        return sorted(targets, key=lambda path: path not in named)

    def _phash_index(self) -> PhashIndex:
        if self._index is None:
            self._index = PhashIndex.from_db(self._db)
        return self._index

    def _reuse_from_index(
        self, image_id: int, phash: int, options: RunOptions
    ) -> Decision | None:
        revision = self._db.rules_revision()
        for other_id, distance in self._phash_index().nearest(
            phash, options.phash_distance
        )[:NEIGHBOUR_LIMIT]:
            if other_id == image_id:
                continue
            stored = self._db.decision_for(other_id, revision)
            if stored is None or stored.kind is TargetKind.UNKNOWN:
                continue
            similarity = 1.0 - distance / 64.0
            return Decision(
                kind=stored.kind,
                action=stored.action,
                fandom=stored.fandom,
                character=stored.character,
                artist=stored.artist,
                multi_character=stored.multi_character,
                confidence=min(stored.confidence, similarity),
                reason=f"Наследовано от похожей картинки (dHash {distance}/64)",
                alternatives=stored.alternatives,
            )
        return None

    async def _by_pixiv_id(self, board: object | None, post_id: str) -> SearchResult | None:
        lookup = getattr(board, "by_pixiv_id", None)
        if lookup is None:
            return None
        return await lookup(post_id)

    async def _resolve_hits(
        self, hits: list[DiscoveryHit], boards: dict[Source, object] | None = None
    ) -> list[SearchResult]:
        available = self._boards if boards is None else boards
        signals: list[SearchResult] = []
        seen_posts: set[str] = set()

        post_lookups: list[tuple[object, str, Source | None]] = []
        for hit in hits:
            if hit.source is None:
                continue
            board = available.get(hit.source)
            if board is None:
                continue
            post_id = hit.post_id
            if not post_id and hit.url:
                post_id = board.post_id_from_url(hit.url)
            if not post_id or post_id in seen_posts:
                continue
            seen_posts.add(post_id)
            post_lookups.append((board, post_id, hit.source))

        if post_lookups:
            found = await asyncio.gather(
                *(board.by_id(post_id) for board, post_id, _source in post_lookups)
            )
            danbooru = available.get(Source.DANBOORU)
            mirrors = (available.get(Source.YANDERE), available.get(Source.KONACHAN))
            for (_board, post_id, source), result in zip(post_lookups, found, strict=True):
                if result is not None:
                    signals.append(result)
                    if result.fandom_tags:
                        continue
                if source is not Source.PIXIV:
                    continue
                alternates = await asyncio.gather(
                    *(self._by_pixiv_id(board, post_id) for board in (danbooru, *mirrors))
                )
                signals.extend(item for item in alternates if item is not None)

        found_md5: set[str] = set()
        md5s = {hit.md5 for hit in hits if hit.md5}
        if md5s:
            pairs = [(md5, board) for md5 in md5s for board in available.values()]
            found = await asyncio.gather(*(board.by_md5(md5) for md5, board in pairs))
            for (md5, _board), result in zip(pairs, found, strict=True):
                if result is not None:
                    signals.append(result)
                    found_md5.add(md5)

        seen_urls: set[str] = set()
        for hit in hits:
            if hit.has_tags or not hit.url:
                continue
            if hit.md5 and hit.md5 in found_md5:
                continue
            key = hit.url.rstrip("/").lower()
            if key in seen_urls:
                continue
            seen_urls.add(key)
            signals.append(
                SearchResult(
                    source=hit.engine,
                    post_id=hit.post_id,
                    post_url=hit.url,
                    similarity=hit.similarity,
                )
            )

        unique: dict[str, SearchResult] = {}
        for signal in signals:
            current = unique.get(signal.evidence_key)
            if current is None or (self._tagged(signal) and not self._tagged(current)):
                unique[signal.evidence_key] = signal
        return list(unique.values())

    @staticmethod
    def _tagged(signal: SearchResult) -> bool:
        return bool(signal.fandom_tags or signal.artist_tags or signal.character_tags)

    def _hits_to_signals(self, hits: list[DiscoveryHit]) -> list[SearchResult]:
        return [
            SearchResult(
                source=hit.engine,
                post_id=hit.post_id,
                post_url=hit.url,
                fandom_tags=hit.fandom_tags or self._fandom_from_raw(hit.raw_tags),
                artist_tags=hit.artist_tags,
                character_tags=hit.character_tags,
                similarity=hit.similarity,
            )
            for hit in hits
        ]

    def _fandom_from_raw(self, tags: tuple[str, ...]) -> tuple[str, ...]:
        found: list[str] = []
        for tag in tags:
            if normalize_key(tag) == ORIGINAL_TAG:
                name = ORIGINAL_TAG
            else:
                match = self._map.resolve(tag).best
                if match is None:
                    continue
                name = match.fandom.name
            if name not in found:
                found.append(name)
        return tuple(found)

    async def _finalize(
        self,
        outcome: FileOutcome,
        options: RunOptions,
        run_id: str,
        *,
        defer: bool = False,
    ) -> None:
        if outcome.image_id is not None:
            for signal in outcome.signals:
                self._db.save_search(outcome.image_id, signal)
            if not defer:
                self._db.save_decision(outcome.image_id, outcome.decision, run_id=run_id)

        if defer:
            return

        if not options.move or outcome.decision.action is Action.REVIEW:
            return

        if outcome.image_id is not None and self._db.was_moved_to(
            outcome.image_id, outcome.path
        ):
            self._db.dequeue(outcome.image_id)
            return

        entry: list[int] = []

        def intend(planned: MovePlan) -> None:
            entry.append(
                int(
                    self._db.add_journal(
                        run_id,
                        outcome.image_id,
                        planned.source,
                        planned.target,
                        planned.size,
                    )
                )
            )

        try:
            plan: MovePlan = await asyncio.to_thread(
                plan_and_move,
                options.output_root,
                outcome.decision,
                outcome.path,
                options.sep_characters,
                None,
                intend,
            )
        except Exception:
            if entry:
                self._db.drop_journal_entry(entry[0])
            raise

        outcome.target = plan.target
        outcome.moved = True
        self._db.commit_move(entry[0], outcome.image_id, plan.target, plan.size)

    async def run(
        self,
        options: RunOptions,
        on_event: EventHandler | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunStats:
        run_id = uuid.uuid4().hex[:12]
        stats = RunStats(run_id=run_id)

        targets = self._collect(options.input_root)
        deferred = [
            path
            for _image_id, path in self._db.queued()
            if os.path.exists(long_path(path))
        ]
        targets = deferred + [path for path in targets if path not in set(deferred)]
        if options.limit is not None:
            targets = targets[: options.limit]
        targets = self._named_first(targets, options)
        stats.total = len(targets)
        logger.info(
            "сортировка %s: файлов %d, ввод %s, вывод %s, перемещение %s, SauceNAO %s",
            run_id,
            stats.total,
            options.input_root,
            options.output_root,
            "да" if options.move else "нет",
            "да" if options.use_saucenao else "нет",
        )
        await self._emit(on_event, events.BATCH, run_id, total=stats.total)

        semaphore = asyncio.Semaphore(max(1, self._settings.fts_max_concurrency))

        async def worker(index: int, path: str) -> None:
            async with semaphore:
                if cancel is not None and cancel.is_set():
                    return
                await self._emit(on_event, events.FILE_STARTED, run_id, path=path, index=index)
                try:
                    outcome = await self.process(path, options, run_id, cancel)
                except Exception as error:  # noqa: BLE001
                    stats.failed += 1
                    logger.exception("файл не обработан: %s", path)
                    await self._emit(
                        on_event,
                        events.FILE_FAILED,
                        run_id,
                        path=path,
                        error=f"{type(error).__name__}: {error}",
                    )
                    return

                if outcome.deleted:
                    stats.deleted += 1
                    await self._emit(
                        on_event, events.FILE_DROPPED, run_id, path=path, reason=outcome.note
                    )
                    return
                deferred = outcome.note.startswith("отложено")
                if outcome.moved:
                    stats.moved += 1
                    await self._emit(
                        on_event, events.FILE_MOVED, run_id, path=path, target=outcome.target
                    )
                elif deferred:
                    stats.deferred += 1
                    await self._emit(on_event, events.DEFERRED, run_id, path=path)
                if outcome.reused:
                    stats.reused += 1
                elif (
                    outcome.decision.kind is TargetKind.UNKNOWN
                    and outcome.decision.action is Action.MOVE
                    and not deferred
                ):
                    stats.unknown += 1
                if outcome.decision.action is Action.REVIEW:
                    stats.review += 1

                await self._emit(
                    on_event,
                    events.FILE_DECIDED,
                    run_id,
                    path=path,
                    target_kind=str(outcome.decision.kind),
                    action=str(outcome.decision.action),
                    fandom=outcome.decision.fandom,
                    character=outcome.decision.character,
                    characters=self._characters(outcome),
                    artist=outcome.decision.artist,
                    confidence=outcome.decision.confidence,
                    reason=outcome.decision.reason,
                    note=outcome.note,
                    target=outcome.target,
                    signals=[
                        {
                            "source": str(signal.source),
                            "similarity": signal.similarity,
                            "post": signal.post_url,
                            "fandom": list(signal.fandom_tags),
                            "artist": list(signal.artist_tags),
                            "character": list(signal.character_tags),
                        }
                        for signal in outcome.signals
                    ],
                    elapsed=round(outcome.elapsed, 3),
                )

        await asyncio.gather(*(worker(index, path) for index, path in enumerate(targets, start=1)))

        stats.finished_at = time.time()
        logger.info(
            "сортировка %s завершена за %.1f с: перемещено %d, на проверку %d, без определения %d, "
            "ошибок %d, из кэша %d, отложено %d, удалено %d",
            run_id,
            stats.elapsed,
            stats.moved,
            stats.review,
            stats.unknown,
            stats.failed,
            stats.reused,
            stats.deferred,
            stats.deleted,
        )
        await self._emit(
            on_event,
            events.DONE,
            run_id,
            total=stats.total,
            moved=stats.moved,
            review=stats.review,
            unknown=stats.unknown,
            failed=stats.failed,
            reused=stats.reused,
            deferred=stats.deferred,
            deleted=stats.deleted,
            elapsed=round(stats.elapsed, 1),
        )
        return stats

    @staticmethod
    def _collect(root: str) -> list[str]:
        from ..storage.paths import iter_files

        return sorted(iter_files(root))

    @staticmethod
    async def _emit(
        on_event: EventHandler | None, kind: str, run_id: str, **payload: object
    ) -> None:
        if on_event is None:
            return
        await on_event(events.RunEvent(kind=kind, run_id=run_id, payload=dict(payload)))
