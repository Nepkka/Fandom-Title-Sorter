from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from urllib.parse import urlparse

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

CHALLENGE_TIMEOUT = 20.0
LOAD_TIMEOUT = 15.0
POLL_INTERVAL = 0.5

CHALLENGE_MARKERS = ("just a moment", "один момент", "checking your browser", "cf-mitigated")


def find_browser() -> str | None:
    for candidate in CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def extract_payload(body: str) -> str | None:
    if not body:
        return None
    text = body.strip()
    if text.startswith(("[", "{")):
        return text
    start = text.find("<pre")
    if start == -1:
        return None
    open_end = text.find(">", start)
    close = text.find("</pre>", open_end)
    if open_end == -1 or close == -1:
        return None
    candidate = text[open_end + 1 : close].strip()
    if candidate.startswith(("[", "{")):
        return candidate
    return None


def looks_like_challenge_body(body: str) -> bool:
    head = (body or "")[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def hide_process_windows(pid: int) -> None:
    if os.name != "nt" or not pid:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles: list[int] = []
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            handles.append(hwnd)
        return True

    user32.EnumWindows(callback(collect), 0)
    for hwnd in handles:
        user32.ShowWindow(hwnd, 0)


def build_browser(settings) -> BrowserTransport | None:
    if not settings.fts_browser_bypass:
        return None
    transport = BrowserTransport(
        settings.fts_browser_path or None,
        tabs=settings.fts_browser_tabs,
        hidden=settings.fts_browser_hidden,
    )
    return transport if transport.available else None


@dataclass(slots=True)
class BrowserStats:
    requests: int = 0
    challenges_passed: int = 0
    failures: int = 0


class BrowserTransport:
    def __init__(
        self,
        executable: str | None = None,
        *,
        headless: bool = False,
        timeout: float = CHALLENGE_TIMEOUT,
        tabs: int = 4,
        poll_interval: float = POLL_INTERVAL,
        load_timeout: float = LOAD_TIMEOUT,
        hidden: bool = True,
    ) -> None:
        self._executable = executable or find_browser()
        self._headless = headless
        self._timeout = timeout
        self._load_timeout = load_timeout
        self._poll = poll_interval
        self._hidden = hidden
        self._tabs_wanted = max(1, tabs)
        self._browser_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        self._browser = None
        self._tabs: asyncio.Queue = asyncio.Queue()
        self._all_tabs: list[object] = []
        self._start_lock = asyncio.Lock()
        self._hopeless: set[str] = set()
        self._attempting: set[str] = set()
        self.stats = BrowserStats()

    @property
    def available(self) -> bool:
        return self._executable is not None

    @property
    def tabs(self) -> int:
        return len(self._all_tabs) or self._tabs_wanted

    async def _ensure_started(self) -> bool:
        if self._browser is not None:
            return True
        if not self.available:
            return False
        async with self._start_lock:
            if self._browser is not None:
                return True
            return await self._start_tabs()

    async def _start_tabs(self) -> bool:
        try:
            import nodriver as uc
        except ImportError:
            return False
        try:
            self._browser = await uc.start(
                headless=self._headless,
                browser_executable_path=self._executable,
                browser_args=list(self._browser_args),
            )
            if self._hidden:
                hide_process_windows(getattr(self._browser, "_process_pid", 0))
            for _ in range(self._tabs_wanted):
                tab = await self._browser.get("about:blank")
                self._all_tabs.append(tab)
                self._tabs.put_nowait(tab)
        except Exception:
            self._browser = None
            self._all_tabs.clear()
            self._tabs = asyncio.Queue()
            return False
        return True

    async def get_text(self, url: str) -> tuple[int, str] | None:
        host = urlparse(url).netloc.lower()
        if host in self._hopeless or host in self._attempting:
            return None
        self._attempting.add(host)
        try:
            if not await self._ensure_started():
                return None
            tab = await self._tabs.get()
            try:
                outcome, tab = await self._load(tab, url)
            except BaseException:
                self._tabs.put_nowait(tab)
                raise
            self._tabs.put_nowait(tab)
            if outcome is None:
                self._hopeless.add(host)
            return outcome
        finally:
            self._attempting.discard(host)

    async def clearance(self, url: str) -> tuple[str, str] | None:
        if not await self._ensure_started():
            return None
        tab = None
        try:
            tab = await asyncio.wait_for(self._browser.get(url), timeout=self._load_timeout)
            return await self._solve_challenge(tab, url)
        except Exception:
            self.stats.failures += 1
            return None
        finally:
            if tab is not None:
                with contextlib.suppress(Exception):
                    await tab.close()

    async def _solve_challenge(self, tab, url: str) -> tuple[str, str] | None:
        waited = 0.0
        while waited < self._timeout:
            await asyncio.sleep(self._poll)
            waited += self._poll
            try:
                body = await tab.get_content()
            except Exception:
                continue
            if not body.strip() or looks_like_challenge_body(body):
                continue
            cookies = await self._cookie_header(tab, url)
            agent = await self._user_agent(tab)
            if cookies is None or agent is None:
                self.stats.failures += 1
                return None
            self.stats.challenges_passed += 1
            return cookies, agent

        self.stats.failures += 1
        return None

    @staticmethod
    async def _cookie_header(tab, url: str) -> str | None:
        try:
            from nodriver.cdp import network as cdp_network

            items = await tab.send(cdp_network.get_cookies(urls=[url]))
        except Exception:
            return None
        pairs = [f"{item.name}={item.value}" for item in items if item.name]
        return "; ".join(pairs) or None

    @staticmethod
    async def _user_agent(tab) -> str | None:
        try:
            agent = await tab.evaluate("navigator.userAgent", return_by_value=True)
        except Exception:
            return None
        return agent if isinstance(agent, str) else None

    async def _load(self, tab, url: str) -> tuple[tuple[int, str] | None, object]:
        try:
            await asyncio.wait_for(tab.get(url), timeout=self._load_timeout)
        except Exception:
            self.stats.failures += 1
            return None, await self._replace_tab(tab)

        waited = 0.0
        challenged = False
        while waited < self._timeout:
            try:
                body = await tab.get_content()
            except Exception:
                body = ""
            if body.strip():
                if looks_like_challenge_body(body) and extract_payload(body) is None:
                    challenged = True
                else:
                    payload = extract_payload(body)
                    if payload is not None:
                        if challenged:
                            self.stats.challenges_passed += 1
                        self.stats.requests += 1
                        return (200, payload), tab
            await asyncio.sleep(self._poll)
            waited += self._poll
        self.stats.failures += 1
        return None, tab

    async def _replace_tab(self, dead):
        if self._browser is None:
            return dead
        with contextlib.suppress(Exception):
            fresh = await self._browser.get("about:blank")
            if dead in self._all_tabs:
                self._all_tabs[self._all_tabs.index(dead)] = fresh
            return fresh
        return dead

    async def aclose(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.stop()
            self._browser = None
            self._all_tabs.clear()
            self._tabs = asyncio.Queue()

    def summary(self) -> dict[str, int]:
        return {
            "requests": self.stats.requests,
            "challenges_passed": self.stats.challenges_passed,
            "failures": self.stats.failures,
            "available": int(self.available),
            "tabs": self.tabs,
        }
