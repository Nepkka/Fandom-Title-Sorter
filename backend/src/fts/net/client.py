from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..config import Settings
from .browser import BrowserTransport
from .cache import HttpCache
from .errors import NETWORK_ERRORS
from .solver import CloudflareSolver, looks_like_challenge

NO_RETRY_STATUSES = frozenset({400, 401, 403, 404, 429})

MAX_CLIENTS = 32

ANY_ROUTE = object()


class ProxyStrategy(Protocol):
    def direct_allowed(self, host: str) -> bool: ...

    def candidates(self, host: str) -> list[str]: ...

    def routes_for(self, host: str) -> list[str | None]: ...

    def note_direct(self, host: str, ok: bool) -> None: ...

    def note_proxy(
        self, proxy: str, host: str, ok: bool, latency_ms: int | None = None
    ) -> None: ...


class NoProxy:
    def direct_allowed(self, host: str) -> bool:
        return True

    def candidates(self, host: str) -> list[str]:
        return []

    def routes_for(self, host: str) -> list[str | None]:
        return [None]

    def note_direct(self, host: str, ok: bool) -> None:
        return None

    def note_proxy(
        self, proxy: str, host: str, ok: bool, latency_ms: int | None = None
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    content: bytes
    url: str
    proxy: str | None = None
    from_cache: bool = False
    elapsed: float = 0.0
    error: str | None = None
    attempts: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError:
            return None


def socks_remote_dns(proxy: str | None) -> str | None:
    if proxy and proxy.startswith("socks5://"):
        return f"socks5h://{proxy[len('socks5://'):]}"
    return proxy


def default_client_factory(settings: Settings) -> Callable[[str | None], httpx.AsyncClient]:
    def factory(proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=socks_remote_dns(proxy),
            timeout=httpx.Timeout(
                connect=settings.fts_connect_timeout,
                read=settings.fts_read_timeout,
                write=settings.fts_read_timeout,
                pool=settings.fts_connect_timeout,
            ),
            headers={"User-Agent": settings.fts_user_agent},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )

    return factory


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: HttpCache | None = None,
        proxies: ProxyStrategy | None = None,
        client_factory: Callable[[str | None], httpx.AsyncClient] | None = None,
        solver: CloudflareSolver | None = None,
        browser: BrowserTransport | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._proxies: ProxyStrategy = proxies or NoProxy()
        self._clients: OrderedDict[str | None, httpx.AsyncClient] = OrderedDict()
        self._client_refs: dict[str | None, int] = {}
        self._factory = client_factory or default_client_factory(settings)
        self._solver = solver
        self._browser = browser
        self._sleep = sleep or asyncio.sleep
        self._global = asyncio.Semaphore(max(1, settings.fts_max_concurrency))
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    def drop_cache(self, key: str | None) -> bool:
        if not key or self._cache is None:
            return False
        return self._cache.drop(key)

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @staticmethod
    def _is_challenge(fetched: Fetched) -> bool:
        return looks_like_challenge(fetched.status, fetched.headers, fetched.content)

    async def _try_browser(
        self,
        method: str,
        url: str,
        original: Fetched,
        started: float,
        attempts: int,
    ) -> Fetched:
        if self._browser is None or method.upper() != "GET":
            return original
        outcome = await self._browser.get_text(url)
        if outcome is None:
            return original
        status, payload = outcome
        return Fetched(
            status=status,
            content=payload.encode("utf-8"),
            url=url,
            proxy=None,
            elapsed=time.monotonic() - started,
            attempts=attempts,
            headers={"x-fts-transport": "browser"},
        )

    async def clearance(self, url: str) -> dict[str, str] | None:
        if self._browser is None:
            return None
        outcome = await self._browser.clearance(url)
        if outcome is None:
            return None
        cookies, agent = outcome
        return {"Cookie": cookies, "User-Agent": agent}

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._client_refs.clear()
        if self._browser is not None:
            await self._browser.aclose()

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        self._client_refs[proxy] = self._client_refs.get(proxy, 0) + 1
        if proxy in self._clients:
            self._clients.move_to_end(proxy)
            return self._clients[proxy]
        client = self._factory(proxy)
        self._clients[proxy] = client
        return client

    def _release_client(self, proxy: str | None) -> None:
        remaining = self._client_refs.get(proxy, 1) - 1
        if remaining > 0:
            self._client_refs[proxy] = remaining
            return
        self._client_refs.pop(proxy, None)
        while len(self._clients) > MAX_CLIENTS:
            idle = next(
                (key for key in self._clients if self._client_refs.get(key, 0) <= 0),
                None,
            )
            if idle is None:
                return
            evicted = self._clients.pop(idle)
            asyncio.ensure_future(evicted.aclose())

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_semaphores:
            self._host_semaphores[host] = asyncio.Semaphore(self.host_concurrency(host))
        return self._host_semaphores[host]

    def host_concurrency(self, host: str) -> int:
        configured = max(1, self._settings.fts_per_host_concurrency)
        available = len(self._proxies.candidates(host))
        if available == 0:
            return configured
        return max(configured, min(available, self._settings.fts_per_host_cap))

    def routes_for(self, host: str) -> list[str | None]:
        return self._proxies.routes_for(host)

    async def _route_once(
        self,
        proxy: str | None,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        files: dict[str, Any] | None,
        headers: dict[str, str] | None,
        host: str,
    ) -> Fetched | None:
        client = self._client(proxy)
        started = time.monotonic()
        try:
            async with self._global, self._semaphore(host):
                response = await client.request(
                    method, url, params=params, data=data, files=files, headers=headers
                )
            self._note(
                host, proxy, ok=True, latency_ms=int((time.monotonic() - started) * 1000)
            )
            return Fetched(
                status=response.status_code,
                content=response.content,
                url=str(response.url),
                proxy=proxy,
                elapsed=time.monotonic() - started,
                headers=dict(response.headers),
            )
        except NETWORK_ERRORS as error:
            self._note(host, proxy, ok=False)
            self._last_error = f"{type(error).__name__}: {error}"
            return None
        finally:
            self._release_client(proxy)

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        batch: int | None = None,
        route: Any = ANY_ROUTE,
    ) -> Fetched:
        host = urlparse(url).netloc.lower()

        if cache_key and self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                status, body = hit
                return Fetched(status=status, content=body, url=url, from_cache=True)

        started = time.monotonic()
        if route is ANY_ROUTE:
            routes = self.routes_for(host)[: self._settings.fts_proxy_attempts]
        else:
            routes = [route]
        if not routes:
            return Fetched(
                status=0,
                content=b"",
                url=url,
                error=f"нет доступного маршрута для {host}: напрямую недоступен, прокси не набраны",
                attempts=0,
            )

        size = max(1, batch or self._settings.fts_proxy_batch)
        proxies = [route for route in routes if route is not None]
        groups: list[list[str | None]] = []
        if None in routes:
            groups.append([None])
        groups.extend(
            proxies[start : start + size] for start in range(0, len(proxies), size)
        )

        attempts = 0
        last_error: str | None = None
        last_response: Fetched | None = None

        for chunk in groups:
            attempts += len(chunk)
            pending = {
                asyncio.create_task(
                    self._route_once(
                        proxy,
                        method=method,
                        url=url,
                        params=params,
                        data=data,
                        files=files,
                        headers=headers,
                        host=host,
                    )
                )
                for proxy in chunk
            }
            chosen: Fetched | None = None
            try:
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        item = task.result()
                        if item is None:
                            continue
                        if item.ok or item.status in NO_RETRY_STATUSES:
                            chosen = item
                            break
                        if item.status >= 500:
                            last_error = f"HTTP {item.status}"
                            last_response = item
                    if chosen is not None:
                        break
            finally:
                for task in pending:
                    task.cancel()

            if chosen is not None:
                result = Fetched(
                    status=chosen.status,
                    content=chosen.content,
                    url=chosen.url,
                    proxy=chosen.proxy,
                    elapsed=time.monotonic() - started,
                    attempts=attempts,
                    headers=chosen.headers,
                )
                if looks_like_challenge(chosen.status, dict(chosen.headers), chosen.content):
                    full_url = str(httpx.URL(url, params=params))
                    result = await self._try_browser(method, full_url, result, started, attempts)
                    if result.status == 0 or not self._is_challenge(result):
                        if cache_key and self._cache is not None and result.ok:
                            self._cache.put(
                                cache_key, url, result.status, result.content, ttl=cache_ttl
                            )
                        return result
                    if self._solver is not None:
                        solved = await self._solver.solve_get(full_url)
                        if solved is not None:
                            status, body = solved
                            result = Fetched(
                                status=status,
                                content=body,
                                url=full_url,
                                proxy=chosen.proxy,
                                elapsed=time.monotonic() - started,
                                attempts=attempts,
                            )
                if cache_key and self._cache is not None and result.ok:
                    self._cache.put(
                        cache_key, url, result.status, result.content, ttl=cache_ttl
                    )
                return result

        if last_response is not None:
            return Fetched(
                status=last_response.status,
                content=last_response.content,
                url=last_response.url,
                proxy=last_response.proxy,
                elapsed=time.monotonic() - started,
                error=last_error,
                attempts=attempts,
                headers=last_response.headers,
            )

        return Fetched(
            status=0,
            content=b"",
            url=url,
            elapsed=time.monotonic() - started,
            error=last_error or getattr(self, "_last_error", None) or "все попытки исчерпаны",
            attempts=attempts,
        )

    def _note(
        self, host: str, proxy: str | None, ok: bool, latency_ms: int | None = None
    ) -> None:
        if proxy is None:
            self._proxies.note_direct(host, ok)
        else:
            self._proxies.note_proxy(proxy, host, ok, latency_ms)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, Fetched]:
        fetched = await self.fetch(
            "GET",
            url,
            params=params,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
            headers=headers,
        )
        return fetched.json(), fetched


async def fetch_routed(
    fetcher: Fetcher,
    limiter: Any,
    host: str,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    routes: Iterable[str | None] | None = None,
    **kwargs: Any,
) -> Fetched:
    if limiter is None:
        return await fetcher.fetch(method, url, **kwargs)

    available = list(routes) if routes is not None else fetcher.routes_for(host)
    if not available:
        return Fetched(
            status=0,
            content=b"",
            url=url,
            error=f"нет ни прямого адреса, ни прокси для {host}",
        )
    route = await limiter.acquire_route(available)
    response = await fetcher.fetch(method, url, route=route, **kwargs)
    if response.proxy is None and route is not None:
        response = replace(response, proxy=route)
    for _ in range(max(0, attempts - 1)):
        if response.status != 0:
            break
        available = [item for item in available if item != route]
        if not available:
            break
        route = await limiter.acquire_route(available)
        response = await fetcher.fetch(method, url, route=route, **kwargs)
        if response.proxy is None and route is not None:
            response = replace(response, proxy=route)
    return response
