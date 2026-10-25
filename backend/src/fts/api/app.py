from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..domain.models import Action, Decision, Fandom, TargetKind
from ..domain.naming import safe_component, target_directory, target_for
from ..domain.rules import FandomMap, merge_duplicate_aliases, rules_revision
from ..jobs.runner import RunOptions, Sorter
from ..logging_setup import setup_logging
from ..net.browser import build_browser
from ..net.cache import HttpCache
from ..net.client import Fetcher
from ..net.proxy import VALIDATION_SKIP, VALIDATION_TARGETS, ProxyPool
from ..net.ratelimit import AdaptiveLimiter, DailyBudget, RollingWindow
from ..net.solver import CloudflareSolver
from ..providers.boards import build_boards
from ..providers.iqdb import IqdbEngine
from ..providers.saucenao import SaucenaoEngine
from ..providers.tracemoe import TraceMoeEngine
from ..storage.db import Database
from ..storage.paths import long_path, merge_folder, plan_and_move, prune_thumbs
from .jobs import JobManager

TOKEN_HEADER = "X-FTS-Token"

REVISION_HEADER = "X-FTS-Rules-Revision"
THUMB_CACHE_HEADERS = {"Cache-Control": "private, max-age=600"}

logger = logging.getLogger(__name__)


class RunRequest(BaseModel):
    input_root: str
    output_root: str
    move: bool = True
    use_saucenao: bool | None = None
    use_iqdb: bool = True
    use_tracemoe: bool = True
    sep_characters: bool | None = None
    limit: int | None = Field(default=None, ge=1)
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    review_below_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rescan_unknown: bool = False


class SettingsPayload(BaseModel):
    input_root: str | None = None
    output_root: str | None = None
    sep_characters: bool | None = None
    concurrency: int | None = Field(default=None, ge=1, le=256)
    limit: int | None = Field(default=None, ge=0)
    saucenao_keys: str | None = None


class RulePayload(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    priority: int = 0


class DecisionPayload(BaseModel):
    kind: Literal["fandom", "original", "unknown"] = "fandom"
    fandom: str | None = None
    character: str | None = None
    artist: str | None = None
    multi_character: bool = False
    move: bool = False


class RuleFromTagPayload(BaseModel):
    tag: str = Field(min_length=1)
    mode: Literal["alias", "new"] = "alias"
    name: str = ""
    priority: int = 0


class SuggestionPayload(BaseModel):
    tag: str = Field(min_length=1)
    name: str = ""


def _forget_known_unmatched(db: Database) -> None:
    fandom_map = FandomMap(db.load_rules())
    known = [
        item["tag"]
        for item in db.unmatched_tags(limit=1000)
        if fandom_map.resolve(item["tag"]).best is not None
    ]
    if known:
        db.forget_unmatched(known)


def prune_thumb_cache(settings: Settings) -> dict[str, int]:
    removed, freed = prune_thumbs(
        settings.thumbs_dir,
        max_age_seconds=max(1, settings.fts_thumbs_max_age_days) * 86400,
        max_bytes=max(0, settings.fts_thumbs_max_bytes),
    )
    if removed:
        logger.info("кэш превью: удалено %d файлов, освобождено %.1f МБ", removed, freed / 1e6)
    return {"removed": removed, "freed_bytes": freed}


def decision_from_payload(payload: DecisionPayload) -> Decision:
    return Decision(
        kind=TargetKind(payload.kind),
        action=Action.MOVE,
        fandom=payload.fandom or None,
        character=None if payload.multi_character else (payload.character or None),
        artist=payload.artist or None,
        multi_character=payload.multi_character,
        confidence=1.0,
        reason="Изменено вручную",
        manual=True,
    )


def target_preview(
    settings: Settings, db: Database, image_id: int, payload: DecisionPayload
) -> dict[str, Any]:
    row = db.image_row(image_id)
    if row is None:
        raise HTTPException(status_code=404, detail="изображение не найдено")
    output_root = db.get_setting("output_root", settings.fts_output_root)
    if not output_root:
        raise HTTPException(status_code=400, detail="не задана папка вывода")

    decision = decision_from_payload(payload)
    sep_characters = (
        db.get_setting("sep_characters", str(settings.fts_sep_characters)) == "True"
    )
    def check(candidate: str) -> bool:
        return os.path.exists(long_path(candidate))

    directory = target_directory(output_root, decision, sep_characters)
    filename = os.path.basename(str(row["path"]))
    target = target_for(output_root, decision, str(row["path"]), check, sep_characters)
    return {
        "target": target,
        "folder": directory,
        "relative": os.path.relpath(target, output_root),
        "folder_exists": check(directory),
        "name_taken": check(os.path.join(directory, filename)),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    log_path = setup_logging(settings)

    db = Database(settings.db_path)
    cache = HttpCache(settings.cache_dir / "http.db")
    pool = ProxyPool(settings, db)
    token = settings.resolve_api_token()
    settings.fts_max_concurrency = int(
        db.get_setting("concurrency", str(settings.fts_max_concurrency))
    )
    dropped = cache.purge_expired(max_age_seconds=settings.fts_http_cache_max_age_days * 86400)
    if dropped:
        logger.info("очищено записей HTTP-кэша: %d", dropped)

    repaired = db.repair_moved_paths()
    if repaired:
        logger.warning("восстановлено путей после незавершённого перемещения: %d", repaired)
    prune_thumb_cache(settings)
    logger.info(
        "запуск: база %s, в словаре %d, лог %s", db.path, db.rule_count(), log_path or "выключен"
    )

    limiter = AdaptiveLimiter(
        RollingWindow(settings.saucenao_per_window, settings.saucenao_window_seconds),
        DailyBudget(db, "saucenao", settings.saucenao_daily_budget_per_ip),
        max_limit=max(settings.saucenao_per_window, settings.saucenao_max_per_window),
    )
    iqdb_limiter = AdaptiveLimiter(
        RollingWindow(settings.iqdb_per_window, settings.iqdb_window_seconds),
        DailyBudget(db, "iqdb", settings.iqdb_daily_budget_per_ip),
        max_limit=max(settings.iqdb_per_window, settings.iqdb_max_per_window),
    )
    tracemoe_limiter = AdaptiveLimiter(
        RollingWindow(settings.tracemoe_per_window, settings.tracemoe_window_seconds),
        DailyBudget(db, "tracemoe", settings.tracemoe_daily_budget_per_ip),
        max_limit=max(settings.tracemoe_per_window, settings.tracemoe_max_per_window),
    )
    solver = (
        CloudflareSolver(settings.fts_cf_solver_url) if settings.fts_cf_solver_url else None
    )
    browser = build_browser(settings)

    async def run_job(
        options: RunOptions,
        on_event,
        cancel: asyncio.Event,
    ):
        async with Fetcher(
            settings, cache=cache, proxies=pool, solver=solver, browser=browser
        ) as fetcher:
            fandom_map = FandomMap(db.load_rules())
            sorter = Sorter(
                settings,
                db,
                fandom_map,
                engines=[
                    IqdbEngine(
                        fetcher,
                        limiter=iqdb_limiter,
                        min_similarity=settings.iqdb_min_similarity,
                    ),
                    TraceMoeEngine(
                        fetcher,
                        limiter=tracemoe_limiter,
                        min_similarity=settings.tracemoe_min_similarity,
                    ),
                ],
                boards=build_boards(fetcher, fandom_map, db.tag_lookup()),
                saucenao=(
                    SaucenaoEngine(
                        fetcher,
                        limiter,
                        min_similarity=settings.saucenao_min_similarity,
                        keys=[
                            line.strip()
                            for line in db.get_setting("saucenao_keys", "").splitlines()
                            if line.strip()
                        ],
                    )
                    if options.use_saucenao
                    else None
                ),
                board_route=pool.has_route,
                topup=pool.top_up,
            )
            try:
                return await sorter.run(options, on_event=on_event, cancel=cancel)
            finally:
                await asyncio.to_thread(prune_thumb_cache, settings)

    jobs = JobManager(run_job)

    app = FastAPI(title="FandomTitleSorter", version="0.2.0")
    app.state.ctx = SimpleNamespace(
        settings=settings,
        db=db,
        cache=cache,
        pool=pool,
        jobs=jobs,
        limiter=limiter,
        solver=solver,
        browser=browser,
        token=token,
    )

    def check_token(request: Request) -> None:
        provided = request.headers.get(TOKEN_HEADER) or request.query_params.get("token")
        if provided != token:
            raise HTTPException(status_code=401, detail="неверный токен")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin:
            host = (urlsplit(origin).hostname or "").lower()
            if host not in {"localhost", "127.0.0.1", "::1"}:
                return JSONResponse(
                    {"detail": "запросы с чужих origin запрещены"}, status_code=403
                )
        return await call_next(request)

    @app.middleware("http")
    async def no_cache_index(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/index.html"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(check_token)])
    async def status() -> dict[str, Any]:
        saucenao_routes = pool.routes_for("saucenao.com")
        return {
            "ok": True,
            "rules": db.rule_count(),
            "proxies": pool.summary(),
            "saucenao_budget_left": limiter.remaining_total(saucenao_routes),
            "input_root": db.get_setting("input_root", settings.fts_input_root),
            "output_root": db.get_setting("output_root", settings.fts_output_root),
            "browser": browser.summary() if browser is not None else {"available": 0},
            "cloudflare_solver": solver.stats() if solver is not None else {},
            "queued": db.queue_size(),
        }

    @app.get("/api/settings", dependencies=[Depends(check_token)])
    async def get_settings_endpoint() -> dict[str, Any]:
        return {
            "input_root": db.get_setting("input_root", settings.fts_input_root),
            "output_root": db.get_setting("output_root", settings.fts_output_root),
            "sep_characters": db.get_setting("sep_characters", str(settings.fts_sep_characters))
            == "True",
            "concurrency": int(
                db.get_setting("concurrency", str(settings.fts_max_concurrency))
            ),
            "limit": int(db.get_setting("limit", "0")),
            "saucenao_keys": db.get_setting("saucenao_keys", ""),
            "limits": {
                "max_concurrency": settings.fts_max_concurrency,
                "per_host_concurrency": settings.fts_per_host_concurrency,
                "saucenao_per_window": settings.saucenao_per_window,
                "saucenao_window_seconds": settings.saucenao_window_seconds,
                "saucenao_daily_budget_per_ip": settings.saucenao_daily_budget_per_ip,
            },
        }

    @app.put("/api/settings", dependencies=[Depends(check_token)])
    async def put_settings(payload: SettingsPayload) -> dict[str, Any]:
        if payload.input_root is not None:
            db.set_setting("input_root", payload.input_root)
        if payload.output_root is not None:
            db.set_setting("output_root", payload.output_root)
        if payload.sep_characters is not None:
            db.set_setting("sep_characters", str(payload.sep_characters))
        if payload.concurrency is not None:
            db.set_setting("concurrency", str(payload.concurrency))
            settings.fts_max_concurrency = payload.concurrency
        if payload.limit is not None:
            db.set_setting("limit", str(payload.limit))
        if payload.saucenao_keys is not None:
            keys = [
                line.strip()
                for line in payload.saucenao_keys.splitlines()
                if line.strip()
            ]
            db.set_setting("saucenao_keys", "\n".join(dict.fromkeys(keys)))
        return await get_settings_endpoint()

    @app.post("/api/scan", dependencies=[Depends(check_token)])
    async def scan(payload: dict[str, Any]) -> dict[str, Any]:
        root = str(payload.get("root") or db.get_setting("input_root", settings.fts_input_root))
        if not root or not os.path.isdir(long_path(root)):
            raise HTTPException(status_code=400, detail=f"папка не найдена: {root}")
        from ..storage.paths import iter_files

        files = sorted(iter_files(root))
        return {"root": root, "count": len(files), "files": files[:500]}

    @app.get("/api/thumbs/by-path", dependencies=[Depends(check_token)])
    async def thumb_by_path(
        path: str = Query(min_length=1), size: int = Query(default=192, ge=32, le=1024)
    ):
        if not os.path.exists(long_path(path)):
            raise HTTPException(status_code=404, detail="файл не найден")
        mtime = int(os.path.getmtime(long_path(path)))

        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
        target = settings.thumbs_dir / f"p{digest}_{mtime}_{size}.webp"
        if not target.exists():

            def build() -> None:
                with Image.open(long_path(path)) as source:
                    preview = source.convert("RGB")
                    preview.thumbnail((size, size))
                    preview.save(long_path(target), format="WEBP", method=4, quality=82)

            await asyncio.to_thread(build)
        return FileResponse(
            long_path(target), media_type="image/webp", headers=THUMB_CACHE_HEADERS
        )

    @app.get("/api/thumbs/{image_id}", dependencies=[Depends(check_token)])
    async def thumb(image_id: int, size: int = Query(default=256, ge=32, le=1024)):
        row = db.image_row(image_id)
        if row is None:
            raise HTTPException(status_code=404, detail="изображение не найдено")
        source = row["path"]
        if not os.path.exists(long_path(source)):
            raise HTTPException(status_code=410, detail="файл больше не существует")

        mtime = int(os.path.getmtime(long_path(source)))
        target = settings.thumbs_dir / f"{image_id}_{mtime}_{size}.webp"
        if not target.exists():
            def build() -> None:
                with Image.open(long_path(source)) as opened:
                    preview = opened.convert("RGB")
                    preview.thumbnail((size, size))
                    preview.save(long_path(target), format="WEBP", method=4, quality=82)

            await asyncio.to_thread(build)
        return FileResponse(
            long_path(target), media_type="image/webp", headers=THUMB_CACHE_HEADERS
        )

    @app.get("/api/files", dependencies=[Depends(check_token)])
    async def files(
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        run_id = db.latest_run_id()
        if run_id is None:
            return {"items": [], "run_id": None}
        library = db.get_setting("output_root", settings.fts_output_root)
        items = db.recent_decisions(
            limit=limit, offset=offset, run_id=run_id, under=library or None
        )
        return {"items": items, "run_id": run_id}

    @app.get("/api/files/{image_id}", dependencies=[Depends(check_token)])
    async def file_details(image_id: int) -> dict[str, Any]:
        row = db.image_row(image_id)
        if row is None:
            raise HTTPException(status_code=404, detail="изображение не найдено")
        decision = db.decision_for(image_id)
        return {
            "image": row,
            "decision": asdict(decision) if decision else None,
            "searches": db.searches_for(image_id),
        }

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(check_token)])
    async def start_job(payload: RunRequest, response: Response) -> dict[str, Any]:
        if jobs.current is not None and jobs.current.status == "running":
            raise HTTPException(status_code=409, detail="сортировка уже идёт")
        limit_from_settings = int(db.get_setting("limit", "0")) or None
        db.set_setting("input_root", payload.input_root)
        db.set_setting("output_root", payload.output_root)
        options = RunOptions(
            input_root=payload.input_root,
            output_root=payload.output_root,
            sep_characters=(
                payload.sep_characters
                if payload.sep_characters is not None
                else db.get_setting("sep_characters", str(settings.fts_sep_characters)) == "True"
            ),
            move=payload.move,
            use_saucenao=payload.use_saucenao if payload.use_saucenao is not None else True,
            use_iqdb=payload.use_iqdb,
            use_tracemoe=payload.use_tracemoe,
            limit=payload.limit if payload.limit is not None else limit_from_settings,
            min_similarity=(
                payload.min_similarity
                if payload.min_similarity is not None
                else settings.fts_min_similarity
            ),
            review_below_confidence=(
                payload.review_below_confidence
                if payload.review_below_confidence is not None
                else settings.fts_review_below_confidence
            ),
            rescan_unknown=payload.rescan_unknown,
        )
        state = await jobs.start(options)
        response.headers["Location"] = f"/api/jobs/{state.job_id}"
        return asdict(state)

    @app.get("/api/jobs/current", dependencies=[Depends(check_token)])
    async def current_job() -> dict[str, Any]:
        state = jobs.current
        return {
            "job": asdict(state) if state else None,
            "history": [asdict(job) for job in jobs.history()],
        }

    @app.delete("/api/jobs/current", dependencies=[Depends(check_token)])
    async def cancel_job() -> dict[str, Any]:
        return {"cancelled": await jobs.cancel()}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(check_token)])
    async def job_status(job_id: str) -> dict[str, Any]:
        state = jobs.job(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="задача не найдена")
        return asdict(state)

    @app.get("/api/events")
    async def events(request: Request, token: str | None = None):
        if token != app.state.ctx.token:
            raise HTTPException(status_code=401, detail="неверный токен")

        last = request.headers.get("last-event-id")
        since = int(last) if last and last.isdigit() else None
        queue = jobs.subscribe()

        async def stream():
            sent = 0
            try:
                yield "retry: 2000\n\n"
                for seq, event in jobs.replay(since):
                    sent = max(sent, seq)
                    yield _sse(event.kind, event.payload, seq)
                yield _sse("snapshot", jobs.snapshot(), sent or None)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        seq, event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if seq <= sent:
                        continue
                    sent = seq
                    yield _sse(event.kind, event.payload, seq)
            finally:
                jobs.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/rules", dependencies=[Depends(check_token)])
    async def get_rules(request: Request, response: Response) -> Any:
        rules = db.load_rules()
        revision = rules_revision(rules)
        etag = f'"{revision}"'
        response.headers[REVISION_HEADER] = revision
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        provided = {item.strip() for item in request.headers.get("if-none-match", "").split(",")}
        if provided & {etag, revision, f"W/{etag}"}:
            return Response(
                status_code=304,
                headers={"ETag": etag, REVISION_HEADER: revision, "Cache-Control": "no-cache"},
            )
        return {
            "count": len(rules),
            "revision": revision,
            "rules": [asdict(rule) for rule in rules],
        }

    @app.put("/api/rules", dependencies=[Depends(check_token)])
    async def put_rules(payload: list[RulePayload], request: Request) -> dict[str, Any]:
        current = rules_revision(db.load_rules())
        provided = request.headers.get("If-Match")
        if provided and provided != current:
            raise HTTPException(
                status_code=409,
                detail="словарь изменён в другом окне — обнови список",
            )
        fandoms = [
            Fandom(
                name=item.name,
                aliases=tuple(item.aliases),
                priority=item.priority,
            )
            for item in payload
        ]
        count = db.replace_rules(merge_duplicate_aliases(fandoms))
        _forget_known_unmatched(db)
        return {"count": count, "revision": rules_revision(db.load_rules())}

    @app.get("/api/unmatched", dependencies=[Depends(check_token)])
    async def unmatched(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        return {"count": db.unmatched_count(), "items": db.unmatched_tags(limit=limit)}

    @app.post("/api/rules/from-tag", dependencies=[Depends(check_token)])
    async def rules_from_tag(payload: RuleFromTagPayload) -> dict[str, Any]:
        rules = db.load_rules()
        name = payload.name.strip()
        if payload.mode == "new":
            if not name:
                raise HTTPException(status_code=400, detail="не указано название фандома")
            for rule in rules:
                if rule.name.lower() == name.lower():
                    raise HTTPException(status_code=409, detail=f"в словаре уже есть: {rule.name}")
            rules.append(
                Fandom(name=name, aliases=(payload.tag,), priority=payload.priority)
            )
        else:
            target = next((rule for rule in rules if rule.name.lower() == name.lower()), None)
            if target is None:
                raise HTTPException(status_code=404, detail=f"нет такого в словаре: {name}")
            rules = [
                Fandom(
                    name=rule.name,
                    aliases=(*rule.aliases, payload.tag) if rule is target else rule.aliases,
                    priority=rule.priority,
                )
                for rule in rules
            ]

        count = db.replace_rules(merge_duplicate_aliases(rules))
        db.forget_unmatched([payload.tag])
        return {"count": count, "mode": payload.mode, "tag": payload.tag}

    @app.get("/api/suggestions", dependencies=[Depends(check_token)])
    async def get_suggestions(
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> dict[str, Any]:
        return {"count": db.suggestion_count(), "items": db.suggestions(limit=limit)}

    @app.post("/api/suggestions/merge", dependencies=[Depends(check_token)])
    async def merge_suggestion(payload: SuggestionPayload) -> dict[str, Any]:
        suggestion = next(
            (item for item in db.suggestions(limit=1000) if item["tag"] == payload.tag), None
        )
        if suggestion is None:
            raise HTTPException(status_code=404, detail="нет такого уточнения")
        rules = db.load_rules()
        target_name = (payload.name or suggestion["similar_to"]).strip()
        target = next(
            (rule for rule in rules if rule.name.lower() == target_name.lower()), None
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"нет такого фандома: {target_name}")
        merged = next((rule for rule in rules if rule.name == suggestion["fandom"]), None)
        extra = list(
            dict.fromkeys(
                (payload.tag, suggestion["fandom"], *(merged.aliases if merged else ()))
            )
        )
        aliases = tuple(dict.fromkeys((*target.aliases, *extra)))
        rules = [
            Fandom(
                name=rule.name,
                aliases=aliases if rule is target else rule.aliases,
                priority=rule.priority,
            )
            for rule in rules
            if merged is None or rule is not merged
        ]
        db.replace_rules(merge_duplicate_aliases(rules))

        output_root = db.get_setting("output_root", settings.fts_output_root)
        run_id = f"merge-{int(time.time())}"
        moved = 0
        if output_root and merged is not None:
            plans = merge_folder(
                os.path.join(output_root, safe_component(merged.name)),
                os.path.join(output_root, safe_component(target.name)),
                on_planned=lambda plan: db.add_journal(
                    run_id, None, plan.source, plan.target, plan.size
                ),
            )
            for plan in plans:
                db.move_path(plan.source, plan.target)
            moved = len(plans)

        db.forget_suggestion(payload.tag)
        db.forget_unmatched([payload.tag])
        return {
            "target": target.name,
            "moved": moved,
            "run_id": run_id,
            "revision": rules_revision(db.load_rules()),
        }

    @app.post("/api/suggestions/keep", dependencies=[Depends(check_token)])
    async def keep_suggestion(payload: SuggestionPayload) -> dict[str, Any]:
        if not db.forget_suggestion(payload.tag):
            raise HTTPException(status_code=404, detail="нет такого уточнения")
        return {"tag": payload.tag, "kept": True}

    @app.get("/api/proxies", dependencies=[Depends(check_token)])
    async def get_proxies() -> dict[str, Any]:
        summary = pool.summary()
        return {
            "total": summary.get("total", 0),
            "hosts": {host: summary.get(host, 0) for host in VALIDATION_TARGETS},
            "targets": VALIDATION_TARGETS,
            "skipped": sorted(VALIDATION_SKIP),
        }

    @app.post("/api/proxies/refresh", dependencies=[Depends(check_token)])
    async def refresh_proxies(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        report = await pool.refresh(concurrency=int((payload or {}).get("concurrency", 20)))
        return {"report": report, "total": db.proxy_count()}

    @app.post("/api/proxies/check", dependencies=[Depends(check_token)])
    async def start_check(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            state = await pool.start_check(
                limit=int((payload or {}).get("limit", 200)),
                concurrency=int((payload or {}).get("concurrency", 200)),
            )
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return state.to_dict()

    @app.get("/api/proxies/check", dependencies=[Depends(check_token)])
    async def check_state() -> dict[str, Any]:
        return pool.check_state.to_dict()

    @app.delete("/api/proxies/check", dependencies=[Depends(check_token)])
    async def stop_check() -> dict[str, Any]:
        return {"stopped": pool.stop_check()}

    @app.post("/api/files/{image_id}/decision", dependencies=[Depends(check_token)])
    async def set_decision(image_id: int, payload: DecisionPayload) -> dict[str, Any]:
        row = db.image_row(image_id)
        if row is None:
            raise HTTPException(status_code=404, detail="изображение не найдено")

        decision = decision_from_payload(payload)
        db.save_decision(image_id, decision, run_id=db.decision_run_id(image_id))

        target = None
        if payload.move:
            path = str(row["path"])
            if not os.path.exists(long_path(path)):
                raise HTTPException(status_code=410, detail="файл уже перемещён или удалён")
            output_root = db.get_setting("output_root", settings.fts_output_root)
            if not output_root:
                raise HTTPException(status_code=400, detail="не задана папка вывода")
            run_id = f"manual-{image_id}"
            entry: list[int] = []

            def intend(planned) -> None:
                entry.append(
                    int(
                        db.add_journal(
                            run_id, image_id, planned.source, planned.target, planned.size
                        )
                    )
                )

            try:
                plan = await asyncio.to_thread(
                    plan_and_move,
                    output_root,
                    decision,
                    path,
                    db.get_setting("sep_characters", str(settings.fts_sep_characters))
                    == "True",
                    None,
                    intend,
                )
            except OSError as error:
                if entry:
                    db.drop_journal_entry(entry[0])
                raise HTTPException(
                    status_code=500, detail=f"не удалось переместить: {error}"
                ) from error
            db.commit_move(entry[0], image_id, plan.target, plan.size)
            target = plan.target

        return {"decision": asdict(decision), "target": target}

    @app.post("/api/files/{image_id}/target", dependencies=[Depends(check_token)])
    async def preview_target(image_id: int, payload: DecisionPayload) -> dict[str, Any]:
        return target_preview(settings, db, image_id, payload)

    @app.post("/api/files/{image_id}/undo", dependencies=[Depends(check_token)])
    async def undo_file(image_id: int) -> dict[str, Any]:
        entry = db.last_move_for_image(image_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="этот файл ещё ни разу не перемещался")
        entry_id, source, target = entry
        if not os.path.exists(long_path(target)):
            raise HTTPException(status_code=410, detail="файла на новом месте уже нет")
        if os.path.exists(long_path(source)):
            raise HTTPException(status_code=409, detail="на старом месте уже что-то лежит")

        def restore() -> None:
            os.makedirs(long_path(os.path.dirname(source)), exist_ok=True)
            os.replace(long_path(target), long_path(source))

        await asyncio.to_thread(restore)
        db.drop_journal_entry(entry_id)
        db.move_image_path(image_id, source)
        return {"restored": source, "from": target}

    @app.get("/api/journal", dependencies=[Depends(check_token)])
    async def journal(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        return {"runs": db.journal_runs(limit=limit), "queued": db.queue_size()}

    @app.post("/api/journal/{run_id}/undo", dependencies=[Depends(check_token)])
    async def undo(run_id: str) -> dict[str, Any]:
        entries = db.journal_entries_for_run(run_id)
        if not entries:
            raise HTTPException(status_code=404, detail="в журнале нет такой сортировки")

        def restore() -> list[dict]:
            moved_back: list[dict] = []
            for entry in reversed(entries):
                source, target = str(entry["source"]), str(entry["target"])
                if os.path.exists(long_path(target)) and not os.path.exists(
                    long_path(source)
                ):
                    os.makedirs(long_path(os.path.dirname(source)), exist_ok=True)
                    os.replace(long_path(target), long_path(source))
                    moved_back.append(entry)
            return moved_back

        moved_back = await asyncio.to_thread(restore)
        db.finish_undo(run_id, moved_back)
        logger.info(
            "откат сортировки %s: возвращено %d из %d", run_id, len(moved_back), len(entries)
        )
        return {
            "run_id": run_id,
            "restored": len(moved_back),
            "skipped": len(entries) - len(moved_back),
            "total": len(entries),
        }

    if settings.frontend_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/",
            StaticFiles(directory=str(settings.frontend_dist), html=True),
            name="ui",
        )

    return app


def _sse(kind: str, payload: dict[str, Any], event_id: int | None = None) -> str:
    body = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {body}\n\n"


def token_for_display(settings: Settings | None = None) -> str:
    return (settings or get_settings()).resolve_api_token()


__all__ = [
    "TOKEN_HEADER",
    "RulePayload",
    "RunRequest",
    "SettingsPayload",
    "create_app",
    "token_for_display",
]
