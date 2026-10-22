from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence

from bs4 import BeautifulSoup

from ..domain.models import Source
from ..net.client import Fetched, Fetcher, fetch_routed
from ..net.ratelimit import AdaptiveLimiter, BudgetExhausted, RollingWindow, SearchUnavailable
from .base import DiscoveryHit, absolute_url

logger = logging.getLogger(__name__)

SAUCENAO_URL = "https://saucenao.com/search.php"
SAUCENAO_ORIGIN = "https://saucenao.com/"
SAUCENAO_HOST = "saucenao.com"

INDEX_SOURCES: dict[str, Source] = {
    "danbooru": Source.DANBOORU,
    "gelbooru": Source.GELBOORU,
    "yandere": Source.YANDERE,
    "yande.re": Source.YANDERE,
    "konachan": Source.KONACHAN,
    "sankaku": Source.DANBOORU,
    "anime-pictures": Source.ANIME_PICTURES,
    "e-shuushuu": Source.ESHUUSHUU,
    "deviantart2": Source.SAUCENAO,
    "deviantart": Source.SAUCENAO,
    "pixiv": Source.SAUCENAO,
    "twitter": Source.SAUCENAO,
    "seiga": Source.SAUCENAO,
    "nijie": Source.SAUCENAO,
}

_INDEX_NAME_RE = re.compile(r"Index\s*#\d+:\s*([^-]+?)\s*-", re.IGNORECASE)
_SIMILARITY_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_HANDLE_RE = re.compile(r"@([\w.\-]+)")

LABEL_FANDOM = ("material", "copyright", "characterseries", "series")
LABEL_ARTIST = ("author", "artist", "creator(s)", "creators", "creator", "member")
LABEL_CHARACTER = ("characters", "character")
LABEL_SOURCE = "source"


class SaucenaoEngine:
    name = Source.SAUCENAO
    key_gap = 1.5
    block_cooldown = 300.0
    clearance_debounce = 10.0

    def __init__(
        self,
        fetcher: Fetcher,
        limiter: AdaptiveLimiter | None = None,
        *,
        numres: int = 4,
        min_similarity: float = 0.0,
        keys: Sequence[str] = (),
    ) -> None:
        self._fetcher = fetcher
        self._limiter = limiter
        self._numres = numres
        self._min_similarity = min_similarity
        self._keys = [key.strip() for key in keys if key.strip()]
        self._dead_keys: set[str] = set()
        self._windows = {key: RollingWindow(4, 30.0) for key in self._keys}
        self._gate = asyncio.Lock()
        self._next_at = 0.0
        self._blocked_until = 0.0
        self._clearance: dict[str, str] | None = None
        self._clearance_at = 0.0
        self._clearance_version = 0
        self._clearance_lock = asyncio.Lock()

    async def _ensure_clearance(self) -> bool:
        async with self._clearance_lock:
            if time.monotonic() - self._clearance_at < self.clearance_debounce:
                return self._clearance is not None
            self._clearance_at = time.monotonic()
            started = time.monotonic()
            fresh = await self._fetcher.clearance(SAUCENAO_URL)
            spent = time.monotonic() - started
            if fresh is None:
                logger.info("SauceNAO: челлендж Cloudflare не пройден за %.1f с", spent)
                return False
            self._clearance = fresh
            self._clearance_version += 1
            logger.info("SauceNAO: челлендж Cloudflare пройден за %.1f с", spent)
            return True

    async def _pace(self) -> None:
        async with self._gate:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self.key_gap

    def _live_keys(self) -> list[str]:
        return [key for key in self._keys if key not in self._dead_keys]

    async def _acquire_key(self) -> tuple[str, RollingWindow]:
        while True:
            for key in self._live_keys():
                window = self._windows[key]
                if window.try_acquire():
                    return key, window
            alive = self._live_keys()
            if not alive:
                raise BudgetExhausted(
                    "SauceNAO: все ключи упёрлись в суточный лимит — добавь ещё в «Настройках»"
                )
            await asyncio.sleep(max(min(self._windows[key].wait_seconds() for key in alive), 0.05))

    async def search(self, image: bytes, *, phash: int | None = None) -> list[DiscoveryHit]:
        cache_key = f"saucenao:{phash}" if phash is not None else None
        if not self._keys:
            response = await self._search_without_key(image, cache_key)
        else:
            try:
                response = await self._search_with_key(image, cache_key)
            except BudgetExhausted as error:
                try:
                    response = await self._search_without_key(image, cache_key)
                except (BudgetExhausted, SearchUnavailable) as keyless:
                    logger.info("SauceNAO: ключи кончились, без ключа тоже не вышло — %s", keyless)
                    raise error from keyless
        if not response.ok:
            raise SearchUnavailable(f"SauceNAO не ответил: статус {response.status}")

        return await asyncio.to_thread(
            self.parse, response.text(), min_similarity=self._min_similarity
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Referer": SAUCENAO_ORIGIN}
        if self._clearance is not None:
            headers.update(self._clearance)
        return headers

    async def _challenge(self, version: int, cleared: bool) -> bool:
        if cleared:
            return False
        return self._clearance_version != version or await self._ensure_clearance()

    def _give_up(self, reason: str) -> SearchUnavailable:
        self._blocked_until = time.monotonic() + self.block_cooldown
        logger.warning("SauceNAO: %s — пауза %d с", reason, int(self.block_cooldown))
        return SearchUnavailable(f"SauceNAO: {reason}")

    async def _search_with_key(self, image: bytes, cache_key: str | None) -> Fetched:
        cleared = False
        while True:
            if time.monotonic() < self._blocked_until:
                raise SearchUnavailable(
                    "SauceNAO блокирует запросы (403) — файл отложен, попробуем позже"
                )
            if self._limiter is not None and self._limiter.budget.remaining("direct") <= 0:
                raise BudgetExhausted(
                    "SauceNAO: суточный запас IP исчерпан (100 поисков в сутки на IP)"
                )
            key, window = await self._acquire_key()
            await self._pace()
            version = self._clearance_version
            response = await self._fetcher.fetch(
                "POST",
                SAUCENAO_URL,
                data={
                    "db": "999",
                    "numres": str(self._numres),
                    "output_type": "0",
                    "api_key": key,
                },
                files={"file": ("image.jpg", image, "image/jpeg")},
                cache_key=cache_key,
                headers=self._headers(),
                batch=1,
                route=None,
            )
            if response.status == 403:
                if await self._challenge(version, cleared):
                    cleared = True
                    continue
                raise self._give_up("403 не снимается даже с cf_clearance")
            if response.status != 429:
                if self._limiter is not None and response.ok:
                    self._limiter.budget.spend("direct")
                return response
            text = response.text().lower()
            if "daily search limit" in text:
                self._dead_keys.add(key)
                if "ip based" in text or "your ip" in text:
                    self._dead_keys.update(self._keys)
                    if self._limiter is not None:
                        self._limiter.exhaust_route("direct")
                    logger.info(
                        "SauceNAO: суточный пул IP исчерпан — все ключи делят один пул, "
                        "остальные ключи больше не пробуем"
                    )
                continue
            window.on_rate_limited()

    async def _search_without_key(self, image: bytes, cache_key: str | None) -> Fetched:
        cleared = False
        while True:
            if time.monotonic() < self._blocked_until:
                raise SearchUnavailable(
                    "SauceNAO блокирует запросы (403) — файл отложен, попробуем позже"
                )
            version = self._clearance_version
            response = await fetch_routed(
                self._fetcher,
                self._limiter,
                SAUCENAO_HOST,
                "POST",
                SAUCENAO_URL,
                data={"db": "999", "numres": str(self._numres), "output_type": "0"},
                files={"file": ("image.jpg", image, "image/jpeg")},
                cache_key=cache_key,
                headers=self._headers(),
                batch=1,
                routes=[None],
            )
            if response.status == 403:
                if await self._challenge(version, cleared):
                    cleared = True
                    continue
                raise self._give_up("403 не снимается даже с cf_clearance")
            if response.status != 429 or self._limiter is None:
                break
            self._limiter.note_rate_limited(response.proxy)
            if "Daily Search Limit" in response.text():
                self._limiter.exhaust_route(response.proxy)
                raise BudgetExhausted(
                    "SauceNAO: суточный лимит без ключей исчерпан на этом IP"
                )
            break

        if self._limiter is not None and not response.from_cache:
            if not response.ok:
                self._limiter.note_failure(response.proxy)
            else:
                self._limiter.note_success(response.proxy)
        return response

    @staticmethod
    def parse(html: str, *, min_similarity: float = 0.0) -> list[DiscoveryHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[DiscoveryHit] = []

        for block in soup.find_all("div", class_="result"):
            preview = block.find("img", id=re.compile(r"^resImage"))
            index_name = ""
            if preview is not None:
                found = _INDEX_NAME_RE.search(preview.get("title", "") or "")
                if found:
                    index_name = found.group(1).strip().lower()
            source = INDEX_SOURCES.get(index_name)

            similarity_block = block.find("div", class_="resultsimilarityinfo")
            similarity = 0.0
            if similarity_block is not None:
                found = _SIMILARITY_RE.search(similarity_block.get_text(" ", strip=True))
                if found:
                    similarity = float(found.group(1)) / 100.0
            if similarity < min_similarity:
                continue

            url = None
            column = block.find("div", class_="resultcontentcolumn")
            for anchor in column.find_all("a", href=True) if column else []:
                href = anchor["href"]
                if href.startswith(("http", "//")):
                    url = absolute_url(href)
                    break

            labels = SaucenaoEngine._labels(column)
            fandom = tuple(_split_tags(labels, LABEL_FANDOM))
            artist = list(_split_tags(labels, LABEL_ARTIST))
            handle = _handle_from_source(labels.get(LABEL_SOURCE, [])) or _handle_from_url(
                url or ""
            )
            if handle and handle not in artist:
                artist.append(handle)
            character = tuple(_split_tags(labels, LABEL_CHARACTER))

            if not (url or fandom or artist or character):
                continue

            hits.append(
                DiscoveryHit(
                    engine=Source.SAUCENAO,
                    similarity=similarity,
                    source=source,
                    url=url,
                    fandom_tags=fandom,
                    artist_tags=tuple(artist),
                    character_tags=character,
                )
            )
        return hits

    @staticmethod
    def _labels(column) -> dict[str, list[str]]:
        labels: dict[str, list[str]] = {}
        if column is None:
            return labels
        current: str | None = None
        buffer: list[str] = []
        for child in column.children:
            name = getattr(child, "name", None)
            if name == "strong":
                text = child.get_text(" ", strip=True)
                if text.endswith(":"):
                    if current:
                        labels[current] = buffer
                    current = text[:-1].strip().lower()
                    buffer = []
                    continue
            elif name == "br":
                continue
            if hasattr(child, "get_text"):
                text = child.get_text(" ", strip=True)
            else:
                text = str(child).strip()
            if text:
                buffer.append(text)
        if current:
            labels[current] = buffer
        return labels


def _handle_from_source(values: list[str]) -> str:
    """Из строки «Source: Twitter @Norman_Maggot» — ник автора."""
    for value in values:
        match = _HANDLE_RE.search(value or "")
        if match:
            return match.group(1)
    return ""


_HANDLE_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:twitter|x)\.com/([\w.\-]+)/status/", re.IGNORECASE),
    re.compile(r"pixiv\.net/@([\w.\-]+)", re.IGNORECASE),
    re.compile(r"deviantart\.com/([\w.\-]+)/art/", re.IGNORECASE),
    re.compile(r"instagram\.com/([\w.\-]+)/p/", re.IGNORECASE),
    re.compile(r"https?://([\w.\-]+)\.tumblr\.com/", re.IGNORECASE),
)

_URL_SEGMENTS_SKIP = frozenset({"i", "intent", "share", "www", "mobile"})


def _handle_from_url(url: str) -> str:
    for pattern in _HANDLE_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            handle = match.group(1)
            if handle.lower() not in _URL_SEGMENTS_SKIP:
                return handle
    return ""


def _split_tags(labels: dict[str, list[str]], keys: tuple[str, ...]) -> list[str]:
    for key in keys:
        values = labels.get(key)
        if not values:
            continue
        parts: list[str] = []
        for value in values:
            for part in re.split(r"[,;]", value):
                part = part.strip()
                if part and part not in parts:
                    parts.append(part)
        if parts:
            return parts
    return []


__all__ = ["SaucenaoEngine", "BudgetExhausted", "SAUCENAO_URL", "INDEX_SOURCES"]
