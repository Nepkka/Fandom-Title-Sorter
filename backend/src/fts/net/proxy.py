from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass

import httpx

from ..config import Settings
from ..storage.db import Database
from .client import NETWORK_ERRORS, socks_remote_dns
from .sources import SOURCES, ProxyRecord, ProxySource, SourceFormat, dedupe, parse

logger = logging.getLogger(__name__)

VALIDATION_TARGETS: dict[str, str] = {
    "danbooru.donmai.us": "https://danbooru.donmai.us/posts.json?limit=1",
    "gelbooru.com": "https://gelbooru.com/index.php?page=dapi&s=post&q=index&limit=1&json=1",
    "yande.re": "https://yande.re/post.json?limit=1",
    "konachan.com": "https://konachan.com/post.json?limit=1",
    "e-shuushuu.net": "https://e-shuushuu.net/",
    "www.zerochan.net": "https://www.zerochan.net/",
    "saucenao.com": "https://saucenao.com/",
    "iqdb.org": "https://iqdb.org/",
    "api.trace.moe": "https://api.trace.moe/me",
}

VALIDATION_SKIP: frozenset[str] = frozenset({"danbooru.donmai.us", "gelbooru.com"})

SUPPORTED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

PROXY_STALE_AFTER = 3600.0

CANDIDATES_TTL = 10.0


@dataclass
class CheckState:
    running: bool = False
    total: int = 0
    checked: int = 0
    dropped: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def usable_proxy(key: str) -> bool:
    scheme = key.split("://", 1)[0].lower()
    return scheme in SUPPORTED_SCHEMES


@dataclass(slots=True)
class CheckResult:
    proxy: str
    host: str
    ok: bool
    latency_ms: int | None
    reachable: bool

    def as_row(self) -> tuple[str, str, bool, int | None]:
        return self.proxy, self.host, self.ok, self.latency_ms


class ProxyPool:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._transport = transport
        self._direct_failures: dict[str, int] = defaultdict(int)
        self._direct_blocked_until: dict[str, float] = {}
        self._check_state = CheckState()
        self._check_cancel: asyncio.Event | None = None
        self._candidates_cache: dict[str, tuple[float, list[str]]] = {}

    def _sources(self) -> list[ProxySource]:
        extra = [
            ProxySource(
                name=f"custom-{index}",
                url=url,
                fmt=SourceFormat.PLAIN,
            )
            for index, url in enumerate(self._settings.extra_proxy_sources, start=1)
        ]
        file_source = ProxySource(
            name="local-file",
            url=str(self._settings.proxy_file_path),
            fmt=SourceFormat.PLAIN,
        )
        return [*SOURCES, *extra, file_source]

    async def refresh(self, concurrency: int = 20) -> dict[str, int]:
        collected: list[ProxyRecord] = []
        report: dict[str, int] = {}
        sources = self._sources()
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": self._settings.fts_user_agent},
            follow_redirects=True,
        ) as client:

            async def load(source: ProxySource) -> tuple[str, list[ProxyRecord]]:
                async with semaphore:
                    if source.name == "local-file":
                        return source.name, self._read_local_file(source)
                    try:
                        response = await client.get(source.url)
                    except httpx.HTTPError:
                        return source.name, []
                    if response.status_code != 200:
                        return source.name, []
                    return source.name, parse(response.text, source)

            for name, records in await asyncio.gather(*(load(s) for s in sources)):
                report[name] = len(records)
                collected.extend(records)

        unique = dedupe(collected)
        usable = [record for record in unique if record.scheme in SUPPORTED_SCHEMES]
        self._db.upsert_proxies(usable)
        report["_unique"] = len(unique)
        report["_usable"] = len(usable)
        report["_dropped_unsupported"] = len(unique) - len(usable)
        report["_dropped_dead"] = self._db.drop_dead_proxies()
        return report

    def _read_local_file(self, source: ProxySource) -> list[ProxyRecord]:
        path = self._settings.proxy_file_path
        if not path.exists():
            return []
        return parse(path.read_text(encoding="utf-8", errors="replace"), source)

    def _candidates(self, limit: int, host: str | None = None) -> list[str]:
        cutoff = time.time() - PROXY_STALE_AFTER
        if host is None:
            stale = self._db.stale_proxies_any(limit, cutoff)
            fresh = self._db.untested_proxies_any(limit)
        else:
            stale = self._db.stale_proxies(host, limit, cutoff)
            fresh = self._db.untested_proxies(host, limit)
        stale = [key for key in stale if usable_proxy(key)][:limit]
        known = set(stale)
        rest = [key for key in fresh if usable_proxy(key) and key not in known]
        return stale + rest[: max(0, limit - len(stale))]

    async def _check(
        self,
        proxy: str,
        host: str,
        target: str,
        timeout: httpx.Timeout,
        semaphore: asyncio.Semaphore,
    ) -> CheckResult:
        async with semaphore:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    transport=self._transport,
                    proxy=socks_remote_dns(proxy) if self._transport is None else None,
                    timeout=timeout,
                    headers={"User-Agent": self._settings.fts_user_agent},
                    follow_redirects=True,
                ) as client:
                    response = await asyncio.wait_for(
                        client.get(target), timeout=self._settings.proxy_check_timeout
                    )
            except NETWORK_ERRORS:
                return CheckResult(proxy, host, False, None, False)
            latency = int((time.monotonic() - started) * 1000)
            ok = response.status_code == 200
            return CheckResult(proxy, host, ok, latency if ok else None, True)

    async def validate(
        self,
        host: str,
        *,
        limit: int = 200,
        concurrency: int | None = None,
    ) -> int:
        target = VALIDATION_TARGETS.get(host)
        if target is None:
            raise KeyError(f"нет адреса для проверки хоста {host}")

        concurrency = concurrency or self._settings.proxy_validate_concurrency
        candidates = self._candidates(limit, host)
        if not candidates:
            return len(self._db.best_proxies(host, self._settings.proxy_ready_per_host))

        timeout = httpx.Timeout(self._settings.proxy_connect_timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        results = await asyncio.gather(
            *(self._check(proxy, host, target, timeout, semaphore) for proxy in candidates)
        )
        self._db.record_proxy_results(item.as_row() for item in results)
        self._candidates_cache.pop(host, None)
        working = sum(1 for item in results if item.ok)

        return len(self._db.best_proxies(host, self._settings.proxy_ready_per_host)) or working

    def starved(self) -> list[str]:
        ready = self._settings.proxy_ready_per_host
        return [
            host
            for host in VALIDATION_TARGETS
            if host not in VALIDATION_SKIP and len(self._db.best_proxies(host, ready)) < ready
        ]

    async def validate_extra(
        self,
        proxies: Sequence[str],
        hosts: Iterable[str],
        *,
        concurrency: int | None = None,
    ) -> int:
        targets = {
            host: VALIDATION_TARGETS[host]
            for host in hosts
            if host in VALIDATION_TARGETS and host not in VALIDATION_SKIP
        }
        if not targets or not proxies:
            return 0
        timeout = httpx.Timeout(self._settings.proxy_connect_timeout)
        semaphore = asyncio.Semaphore(
            max(1, concurrency or self._settings.proxy_validate_concurrency)
        )
        batches = await asyncio.gather(
            *(self._check_proxy(proxy, targets, timeout, semaphore) for proxy in proxies)
        )
        results = [item for batch in batches for item in batch]
        self._db.record_proxy_results(item.as_row() for item in results)
        for host in targets:
            self._candidates_cache.pop(host, None)
        return sum(1 for item in results if item.ok)

    async def top_up(self, host: str, *, limit: int | None = None) -> bool:
        ready = self._settings.proxy_ready_per_host
        before = set(self._db.best_proxies(host, ready))
        await self.validate(host, limit=limit or self._settings.fts_topup_batch)
        fresh = sorted(set(self._db.best_proxies(host, ready)) - before)
        if not fresh:
            return False
        hungry = [item for item in self.starved() if item != host]
        found = await self.validate_extra(fresh, hungry)
        logger.info(
            "добор под %s: новых адресов %d; проверено ещё под %d хостов — рабочих %d",
            host,
            len(fresh),
            len(hungry),
            found,
        )
        return True

    async def _check_proxy(
        self,
        proxy: str,
        targets: dict[str, str],
        timeout: httpx.Timeout,
        semaphore: asyncio.Semaphore,
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        for position, (host, target) in enumerate(targets.items()):
            item = await self._check(proxy, host, target, timeout, semaphore)
            results.append(item)
            if position == 0 and not item.reachable:
                break
        return results

    async def _run_checks(
        self,
        tasks: list[asyncio.Task[list[CheckResult]]],
        cancel: asyncio.Event | None,
    ) -> list[CheckResult]:
        if cancel is None:
            batches = await asyncio.gather(*tasks)
            return [item for batch in batches for item in batch]
        gathered = asyncio.gather(*tasks)
        stopper = asyncio.ensure_future(cancel.wait())
        done, _pending = await asyncio.wait(
            {gathered, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        stopper.cancel()
        if stopper in done:
            gathered.cancel()
            return []
        batches = gathered.result()
        return [item for batch in batches for item in batch]

    async def validate_all(
        self,
        *,
        limit: int = 200,
        concurrency: int | None = None,
        cancel: asyncio.Event | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, int]:
        concurrency = concurrency or self._settings.proxy_validate_concurrency
        candidates = self._candidates(limit)
        if not candidates:
            return self._db.proxy_summary()

        targets = {
            host: target
            for host, target in VALIDATION_TARGETS.items()
            if host not in VALIDATION_SKIP
        }
        timeout = httpx.Timeout(self._settings.proxy_connect_timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        finished = 0

        async def check(proxy: str) -> list[CheckResult]:
            nonlocal finished
            batch = await self._check_proxy(proxy, targets, timeout, semaphore)
            finished += 1
            if on_progress is not None:
                on_progress(finished, len(candidates))
            return batch

        tasks = [asyncio.ensure_future(check(proxy)) for proxy in candidates]
        try:
            results = await self._run_checks(tasks, cancel)
        finally:
            for task in tasks:
                task.cancel()
        if results:
            self._db.record_proxy_results(item.as_row() for item in results)

        summary = self._db.proxy_summary()
        summary["_checked"] = finished
        summary["_dropped_dead"] = self._db.drop_dead_proxies()
        return summary

    @property
    def check_state(self) -> CheckState:
        return self._check_state

    async def start_check(
        self, limit: int = 200, concurrency: int | None = None
    ) -> CheckState:
        if self._check_state.running:
            raise RuntimeError("проверка уже идёт")
        self._check_state = CheckState(running=True, total=limit)
        self._check_cancel = asyncio.Event()
        asyncio.create_task(self._run_check(limit, concurrency))
        return self._check_state

    async def _run_check(self, limit: int, concurrency: int | None) -> None:
        try:
            report = await self.validate_all(
                limit=limit,
                concurrency=concurrency,
                cancel=self._check_cancel,
                on_progress=self._note_progress,
            )
            self._check_state.dropped = int(report.get("_dropped_dead", 0))
        except Exception as error:  # noqa: BLE001
            self._check_state.error = f"{type(error).__name__}: {error}"
        finally:
            self._check_state.running = False

    def _note_progress(self, checked: int, total: int) -> None:
        self._check_state.checked = checked
        self._check_state.total = total

    def stop_check(self) -> bool:
        if not self._check_state.running or self._check_cancel is None:
            return False
        self._check_cancel.set()
        return True

    async def warmup(self, hosts: list[str], *, limit: int = 200) -> dict[str, str]:
        report: dict[str, str] = {}
        for host in hosts:
            ready = self._db.best_proxies(host, self._settings.proxy_ready_per_host)
            if len(ready) >= self._settings.proxy_min_ready:
                report[host] = f"рабочих прокси: {len(ready)}"
                continue

            if not self.direct_allowed(host):
                report[host] = "напрямую недоступен, рабочих прокси нет — хост пропущен"
                continue

            if await self._direct_probe(host):
                report[host] = "напрямую доступен"
                continue

            for _ in range(self._settings.direct_fail_threshold):
                self.note_direct(host, ok=False)
            working = await self.validate(host, limit=limit)
            report[host] = (
                f"напрямую недоступен; рабочих прокси: {working}"
                if working
                else "напрямую недоступен, рабочих прокси не найдено — хост пропущен"
            )
        return report

    async def _direct_probe(self, host: str) -> bool:
        target = VALIDATION_TARGETS.get(host)
        if target is None:
            return True
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self._settings.fts_connect_timeout),
                headers={"User-Agent": self._settings.fts_user_agent},
                follow_redirects=True,
            ) as client:
                response = await client.get(target)
        except httpx.HTTPError:
            return False
        return response.status_code == 200
    async def harvest(
        self,
        host: str,
        target: int,
        *,
        batch: int = 1500,
        concurrency: int | None = None,
    ) -> int:
        ready = len(self._db.best_proxies(host, self._settings.proxy_ready_per_host))
        while ready < target:
            tested = await self.validate(host, limit=batch, concurrency=concurrency)
            if tested <= ready:
                break
            ready = tested
        return ready

    def direct_allowed(self, host: str) -> bool:
        return time.monotonic() >= self._direct_blocked_until.get(host, 0.0)

    def note_direct(self, host: str, ok: bool) -> None:
        if ok:
            self._direct_failures[host] = 0
            self._direct_blocked_until.pop(host, None)
            return
        self._direct_failures[host] += 1
        if self._direct_failures[host] >= self._settings.direct_fail_threshold:
            self._direct_blocked_until[host] = (
                time.monotonic() + self._settings.direct_retry_after
            )

    def candidates(self, host: str) -> list[str]:
        now = time.monotonic()
        cached = self._candidates_cache.get(host)
        if cached is not None and now - cached[0] < CANDIDATES_TTL:
            return cached[1]
        found = [
            proxy
            for proxy in self._db.best_proxies(host, self._settings.proxy_ready_per_host)
            if usable_proxy(proxy)
        ]
        self._candidates_cache[host] = (now, found)
        return found

    def routes_for(self, host: str) -> list[str | None]:
        routes: list[str | None] = []
        if self.direct_allowed(host):
            routes.append(None)
        routes.extend(self.candidates(host))
        return routes

    def has_route(self, host: str) -> bool:
        if not host:
            return False
        return self.direct_allowed(host) or bool(self.candidates(host))

    def note_proxy(
        self, proxy: str, host: str, ok: bool, latency_ms: int | None = None
    ) -> None:
        self._db.record_proxy_result(proxy, host, ok, latency_ms if ok else None)

    def summary(self) -> dict[str, int]:
        return self._db.proxy_summary()
