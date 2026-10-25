from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import date

from ..storage.db import Database


class BudgetExhausted(RuntimeError):
    pass


class SearchUnavailable(RuntimeError):
    pass


class RollingWindow:
    def __init__(
        self,
        limit: int,
        window: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(1, limit)
        self._window = window
        self._clock = clock
        self._events: deque[float] = deque()
        self._cooldown_until = 0.0

    @property
    def limit(self) -> int:
        return self._limit

    def fork(self) -> RollingWindow:
        return RollingWindow(self._limit, self._window, self._clock)

    def set_limit(self, limit: int) -> None:
        self._limit = max(1, limit)

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] >= self._window:
            self._events.popleft()

    def try_acquire(self) -> bool:
        now = self._clock()
        if now < self._cooldown_until:
            return False
        self._prune(now)
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True

    def wait_seconds(self) -> float:
        now = self._clock()
        if now < self._cooldown_until:
            return self._cooldown_until - now
        self._prune(now)
        if len(self._events) < self._limit:
            return 0.0
        return max(0.0, self._window - (now - self._events[0]))

    async def acquire(self) -> None:
        while not self.try_acquire():
            await asyncio.sleep(max(self.wait_seconds(), 0.01))

    def on_rate_limited(self) -> None:
        now = self._clock()
        self._limit = max(1, min(self._limit, max(1, len(self._events))))
        self._cooldown_until = now + self._window


class DailyBudget:
    def __init__(
        self,
        db: Database,
        name: str,
        limit: int,
        today: Callable[[], str] | None = None,
    ) -> None:
        self._db = db
        self._name = name
        self._limit = max(0, limit)
        self._today = today or (lambda: date.today().isoformat())

    @property
    def limit(self) -> int:
        return self._limit

    def used(self, scope: str = "") -> int:
        return self._db.get_counter(self._name, self._today(), scope)

    def remaining(self, scope: str = "") -> int:
        return max(0, self._limit - self.used(scope))

    def spend(self, scope: str = "", amount: int = 1) -> bool:
        if self.remaining(scope) < amount:
            return False
        self._db.bump_counter(self._name, self._today(), amount, scope)
        return True

    def refund(self, scope: str = "", amount: int = 1) -> None:
        day = self._today()
        used = self._db.get_counter(self._name, day, scope)
        if used <= 0:
            return
        self._db.bump_counter(self._name, day, -min(used, amount), scope)

    def used_total(self) -> int:
        return self._db.counters_sum(self._name, self._today())


def route_key(route: str | None) -> str:
    return route or "direct"


class AdaptiveLimiter:
    def __init__(
        self,
        window: RollingWindow,
        budget: DailyBudget,
        max_limit: int,
        probe_after: int = 3,
    ) -> None:
        self._window = window
        self._budget = budget
        self._max_limit = max(window.limit, max_limit)
        self._probe_after = max(1, probe_after)
        self._windows: dict[str, RollingWindow] = {}
        self._clean: dict[str, int] = {}

    @property
    def window(self) -> RollingWindow:
        return self._window

    @property
    def budget(self) -> DailyBudget:
        return self._budget

    def window_for(self, route: str | None) -> RollingWindow:
        key = route_key(route)
        window = self._windows.get(key)
        if window is None:
            window = self._window.fork()
            self._windows[key] = window
        return window

    def allow_route(self, route: str | None) -> bool:
        if self._budget.remaining(route_key(route)) <= 0:
            return False
        return self.window_for(route).try_acquire()

    def exhaust_route(self, route: str | None) -> None:
        key = route_key(route)
        self._budget.spend(key, self._budget.remaining(key))

    async def acquire_route(self, routes: Iterable[str | None]) -> str | None:
        candidates = list(routes)
        if not candidates:
            raise BudgetExhausted("нет доступного маршрута")
        while True:
            for route in candidates:
                if self.allow_route(route):
                    return route
            if not self.any_budget_left(candidates):
                raise BudgetExhausted("суточный запас исчерпан на всех маршрутах")
            await asyncio.sleep(max(self.wait_seconds(candidates), 0.01))

    def any_budget_left(self, routes: Iterable[str | None]) -> bool:
        return any(self._budget.remaining(route_key(route)) > 0 for route in routes)

    def wait_seconds(self, routes: Iterable[str | None]) -> float:
        waits = [
            self.window_for(route).wait_seconds()
            for route in routes
            if self._budget.remaining(route_key(route)) > 0
        ]
        return min(waits) if waits else 0.0

    def remaining_total(self, routes: Iterable[str | None]) -> int:
        return sum(self._budget.remaining(route_key(route)) for route in routes)

    def note_success(self, route: str | None) -> None:
        key = route_key(route)
        self._budget.spend(key)
        window = self.window_for(route)
        self._clean[key] = self._clean.get(key, 0) + 1
        if self._clean[key] >= self._probe_after and window.limit < self._max_limit:
            window.set_limit(window.limit + 1)
            self._clean[key] = 0

    def note_failure(self, route: str | None) -> None:
        self._clean[route_key(route)] = 0

    def note_rate_limited(self, route: str | None) -> None:
        key = route_key(route)
        self._clean[key] = 0
        self._budget.spend(key)
        self.window_for(route).on_rate_limited()
