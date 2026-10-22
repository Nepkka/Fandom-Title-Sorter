from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from .config import Settings, get_settings
from .domain.rules import FandomMap, fandoms_from_dict, merge_duplicate_aliases
from .jobs.learn import learn_from_library
from .jobs.runner import RunOptions, Sorter
from .logging_setup import setup_logging
from .net.browser import build_browser
from .net.cache import HttpCache
from .net.client import Fetcher
from .net.proxy import VALIDATION_SKIP, VALIDATION_TARGETS, ProxyPool
from .net.ratelimit import AdaptiveLimiter, DailyBudget, RollingWindow
from .net.solver import CloudflareSolver
from .providers.boards import build_boards
from .providers.iqdb import IqdbEngine
from .providers.saucenao import SaucenaoEngine
from .providers.tracemoe import TraceMoeEngine
from .storage.db import Database


class Context:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_dirs()
        setup_logging(settings)
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.repair_moved_paths()
        self.cache = HttpCache(settings.cache_dir / "http.db")
        self.pool = ProxyPool(settings, self.db)
        self.solver = (
            CloudflareSolver(settings.fts_cf_solver_url)
            if settings.fts_cf_solver_url
            else None
        )
        self.browser = build_browser(settings)
        self.fetcher = Fetcher(
            settings,
            cache=self.cache,
            proxies=self.pool,
            solver=self.solver,
            browser=self.browser,
        )

    def limiter(self) -> AdaptiveLimiter:
        return AdaptiveLimiter(
            RollingWindow(self.settings.saucenao_per_window, self.settings.saucenao_window_seconds),
            DailyBudget(self.db, "saucenao", self.settings.saucenao_daily_budget_per_ip),
            max_limit=max(
                self.settings.saucenao_per_window, self.settings.saucenao_max_per_window
            ),
        )

    def iqdb_limiter(self) -> AdaptiveLimiter:
        return AdaptiveLimiter(
            RollingWindow(self.settings.iqdb_per_window, self.settings.iqdb_window_seconds),
            DailyBudget(self.db, "iqdb", self.settings.iqdb_daily_budget_per_ip),
            max_limit=max(self.settings.iqdb_per_window, self.settings.iqdb_max_per_window),
        )

    def tracemoe_limiter(self) -> AdaptiveLimiter:
        return AdaptiveLimiter(
            RollingWindow(
                self.settings.tracemoe_per_window, self.settings.tracemoe_window_seconds
            ),
            DailyBudget(self.db, "tracemoe", self.settings.tracemoe_daily_budget_per_ip),
            max_limit=max(
                self.settings.tracemoe_per_window, self.settings.tracemoe_max_per_window
            ),
        )

    def fandom_map(self) -> FandomMap:
        rules = self.db.load_rules()
        return FandomMap(rules)


def _rules_import(context: Context, path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    fandoms = merge_duplicate_aliases(fandoms_from_dict(data))
    count = context.db.replace_rules(fandoms)
    return count


def _cmd_rules_import(args: argparse.Namespace) -> int:
    if not os.path.exists(args.path):
        print(f"файл не найден: {args.path}", file=sys.stderr)
        return 2
    context = Context(get_settings())
    count = _rules_import(context, args.path)
    print(f"записей в словаре: {count}")
    print("записей в базе:", context.db.rule_count())
    return 0


def _cmd_proxies_refresh(args: argparse.Namespace) -> int:
    context = Context(get_settings())
    started = time.time()

    async def scenario() -> dict[str, int]:
        report = await context.pool.refresh(concurrency=args.concurrency)
        return report

    report = asyncio.run(scenario())
    empty = [name for name, count in report.items() if count == 0 and not name.startswith("_")]
    print(f"собрано за {time.time() - started:.1f} с:")
    for name, count in sorted(report.items(), key=lambda item: -item[1]):
        print(f"   {name:24s} {count}")
    if empty:
        print("источники без данных:", ", ".join(empty))
    return 0


def _cmd_proxies_check(args: argparse.Namespace) -> int:
    context = Context(get_settings())
    host = args.host

    async def scenario() -> int:
        return await context.pool.validate(host, limit=args.limit, concurrency=args.concurrency)

    ready = asyncio.run(scenario())
    print(f"рабочих прокси для {host}: {ready}")
    return 0


def _cmd_proxies_harvest(args: argparse.Namespace) -> int:
    context = Context(get_settings())

    async def scenario() -> int:
        return await context.pool.harvest(
            args.host, args.target, batch=args.batch, concurrency=args.concurrency
        )

    ready = asyncio.run(scenario())
    print(f"рабочих прокси для {args.host}: {ready} (цель {args.target})")
    return 0


def _cmd_proxies_status(args: argparse.Namespace) -> int:
    context = Context(get_settings())
    summary = context.pool.summary()
    print("всего прокси:", summary.get("total", 0))
    for host in VALIDATION_TARGETS:
        if host in VALIDATION_SKIP:
            print(f"   {host:22s} не проверяется")
            continue
        print(f"   {host:22s} рабочих: {summary.get(host, 0)}")
    print("проверочные адреса:", ", ".join(VALIDATION_TARGETS))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.concurrency:
        settings.fts_max_concurrency = int(args.concurrency)
    context = Context(settings)
    fandom_map = context.fandom_map()
    if len(fandom_map) == 0:
        print("словарь пуст: сначала `rules-import путь\\к\\FTS_dict.json`", file=sys.stderr)
        return 2

    options = RunOptions(
        input_root=args.input,
        output_root=args.output,
        sep_characters=not args.flat,
        move=not args.no_move,
        use_saucenao=not args.no_saucenao,
        limit=args.limit,
        min_similarity=settings.fts_min_similarity,
        review_below_confidence=settings.fts_review_below_confidence,
    )

    async def scenario() -> None:
        async with context.fetcher:
            print("маршруты к бордам:")
            for host, verdict in (await context.pool.warmup(sorted(VALIDATION_TARGETS))).items():
                print(f"   {host:22s} {verdict}")

            boards = build_boards(context.fetcher, fandom_map, context.db.tag_lookup())
            engines = [
                IqdbEngine(
                    context.fetcher,
                    limiter=context.iqdb_limiter(),
                    min_similarity=settings.iqdb_min_similarity,
                ),
                TraceMoeEngine(
                    context.fetcher,
                    limiter=context.tracemoe_limiter(),
                    min_similarity=settings.tracemoe_min_similarity,
                ),
            ]
            saucenao = (
                SaucenaoEngine(
                    context.fetcher,
                    context.limiter(),
                    min_similarity=settings.saucenao_min_similarity,
                )
                if options.use_saucenao
                else None
            )
            sorter = Sorter(
                settings,
                context.db,
                fandom_map,
                engines=engines,
                boards=boards,
                saucenao=saucenao,
                board_route=context.pool.has_route,
                topup=context.pool.top_up,
            )

            async def on_event(event) -> None:
                if event.kind == "file_decided":
                    payload = event.payload
                    marker = {
                        "move": "→",
                        "review": "?",
                    }.get(str(payload.get("action")), "·")
                    print(
                        f"{marker} {str(payload.get('target_kind')):8s} "
                        f"{(payload.get('fandom') or '—')[:26]:26s} "
                        f"{(payload.get('character') or '')[:20]:20s} "
                        f"conf={payload.get('confidence')} "
                        f"{payload.get('reason', '')[:60]}"
                    )
                elif event.kind == "done":
                    print("итог:", json.dumps(event.payload, ensure_ascii=False))

            stats = await sorter.run(options, on_event=on_event)
            print(
                f"сортировка {stats.run_id}: файлов {stats.total}, перемещено {stats.moved}, "
                f"на проверку {stats.review}, без определения {stats.unknown}, "
                f"ошибок {stats.failed}, из кэша {stats.reused}, "
                f"отложено {stats.deferred}, время {stats.elapsed:.1f} с"
            )

    asyncio.run(scenario())
    return 0


def _cmd_learn_tags(args: argparse.Namespace) -> int:
    settings = get_settings()
    context = Context(settings)
    fandom_map = context.fandom_map()
    library = (
        args.library
        or context.db.get_setting("output_root", "")
        or settings.fts_output_root
    )
    if not os.path.isdir(library):
        print(f"библиотека не найдена: {library}", file=sys.stderr)
        return 2

    def learn(pairs: list[tuple[str, str]]) -> int:
        return context.db.learn_tags(
            (tag, fandom) for tag, fandom in pairs if fandom_map.resolve(fandom).best is not None
        )

    def report(index: int, total: int, fandom: str) -> None:
        if index % 100 == 0 or index == total:
            print(f"{index}/{total} {fandom} — тегов в карте: {context.db.tag_count()}", flush=True)

    async def scenario() -> dict[str, int]:
        async with context.fetcher:
            return await learn_from_library(
                context.fetcher,
                learn,
                library,
                limit=args.limit,
                pause=args.pause,
                on_page=report,
            )

    stats = asyncio.run(scenario())
    print(
        f"страниц {stats['pages']}, с тегами {stats['tagged']}, "
        f"голосов {stats['votes']}, без ответа {stats['missing']}"
    )
    print("тегов в карте:", context.db.tag_count())
    return 0


def _cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app, token_for_display

    settings = get_settings()
    port = args.port or settings.fts_port or 8765
    app = create_app(settings)
    token = token_for_display(settings)
    print(f"FandomTitleSorter API на http://{settings.fts_host}:{port}")
    print(f"интерфейс: http://{settings.fts_host}:{port}/?token={token}")
    uvicorn.run(app, host=settings.fts_host, port=port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fts", description="FandomTitleSorter v2")
    sub = parser.add_subparsers(dest="command", required=True)

    rules = sub.add_parser("rules-import", help="загрузить словарь фандомов")
    rules.add_argument("path")
    rules.set_defaults(func=_cmd_rules_import)

    refresh = sub.add_parser("proxies-refresh", help="собрать пул бесплатных прокси")
    refresh.add_argument("--concurrency", type=int, default=20)
    refresh.set_defaults(func=_cmd_proxies_refresh)

    check = sub.add_parser("proxies-check", help="проверить прокси против конкретного хоста")
    check.add_argument("host", choices=sorted(VALIDATION_TARGETS))
    check.add_argument("--limit", type=int, default=200)
    check.add_argument("--concurrency", type=int, default=100)
    check.set_defaults(func=_cmd_proxies_check)

    status = sub.add_parser("proxies-status", help="состояние пула")
    status.set_defaults(func=_cmd_proxies_status)

    harvest = sub.add_parser("proxies-harvest", help="набрать N рабочих прокси для хоста")
    harvest.add_argument("host", choices=sorted(VALIDATION_TARGETS))
    harvest.add_argument("--target", type=int, default=40)
    harvest.add_argument("--batch", type=int, default=1500)
    harvest.add_argument("--concurrency", type=int, default=150)
    harvest.set_defaults(func=_cmd_proxies_harvest)

    run = sub.add_parser("run", help="разобрать папку")
    run.add_argument("--input", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--no-move", action="store_true", help="только разбор, без перемещения")
    run.add_argument("--no-saucenao", action="store_true")
    run.add_argument("--flat", action="store_true", help="не раскладывать по персонажам")
    run.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="сколько файлов обрабатывать одновременно (по умолчанию из настроек)",
    )
    run.set_defaults(func=_cmd_run)

    api = sub.add_parser("api", help="поднять локальный сервер для интерфейса")
    api.add_argument("--port", type=int, default=0)
    api.set_defaults(func=_cmd_api)

    learn = sub.add_parser("learn-tags", help="выучить связку «тег → фандом» по библиотеке")
    learn.add_argument(
        "--library", default=None, help="корень библиотеки (по умолчанию output_root)"
    )
    learn.add_argument("--limit", type=int, default=None, help="сколько страниц pixiv опросить")
    learn.add_argument("--pause", type=float, default=0.3, help="пауза между запросами, с")
    learn.set_defaults(func=_cmd_learn_tags)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
