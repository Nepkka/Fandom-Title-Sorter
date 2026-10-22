from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from ..jobs import events as ev
from ..jobs.runner import RunOptions, RunStats

EventHandler = Callable[[ev.RunEvent], Awaitable[None]]
Runner = Callable[[RunOptions, EventHandler, asyncio.Event], Awaitable[RunStats]]


@dataclass
class JobState:
    job_id: str
    status: str = "running"
    stats: dict = field(default_factory=dict)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    events_seen: int = 0
    input_root: str = ""
    output_root: str = ""
    move: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class JobManager:
    def __init__(self, runner: Runner, *, history: int = 20, replay: int = 500) -> None:
        self._runner = runner
        self._subscribers: set[asyncio.Queue] = set()
        self._current: JobState | None = None
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._history: list[JobState] = []
        self._history_limit = max(1, history)
        self._seq = 0
        self._log: deque[tuple[int, ev.RunEvent]] = deque(maxlen=max(1, replay))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def replay(self, since: int | None) -> list[tuple[int, ev.RunEvent]]:
        if since is None:
            return []
        return [item for item in self._log if item[0] > since]

    def snapshot(self) -> dict:
        return {
            "job": self._current.to_dict() if self._current else None,
            "history": [job.to_dict() for job in self._history],
            "seq": self._seq,
        }

    def job(self, job_id: str) -> JobState | None:
        if self._current is not None and self._current.job_id == job_id:
            return self._current
        for state in reversed(self._history):
            if state.job_id == job_id:
                return state
        return None

    async def _publish(self, event: ev.RunEvent) -> None:
        self._seq += 1
        self._log.append((self._seq, event))
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((self._seq, event))

    @property
    def current(self) -> JobState | None:
        return self._current

    def history(self) -> list[JobState]:
        return list(self._history)

    async def start(self, options: RunOptions) -> JobState:
        if self._current is not None and self._current.status == "running":
            raise RuntimeError("сортировка уже идёт")

        state = JobState(
            job_id=uuid.uuid4().hex[:12],
            input_root=options.input_root,
            output_root=options.output_root,
            move=options.move,
        )
        self._current = state
        self._cancel = asyncio.Event()

        async def on_event(event: ev.RunEvent) -> None:
            state.events_seen += 1
            if event.kind == ev.BATCH:
                state.stats = {"total": int(event.payload.get("total") or 0)}
            await self._publish(event)

        async def runner() -> None:
            try:
                stats = await self._runner(options, on_event, self._cancel)
                state.status = "cancelled" if self._cancel.is_set() else "done"
                state.stats = _stats_payload(stats)
            except Exception as error:  # noqa: BLE001
                state.status = "failed"
                state.error = f"{type(error).__name__}: {error}"
            finally:
                state.finished_at = time.time()
                if self._current is state:
                    self._current = None
                self._history.append(state)
                if len(self._history) > self._history_limit:
                    self._history.pop(0)
                await self._publish(
                    ev.RunEvent(kind=ev.DONE, run_id=state.job_id, payload=state.to_dict())
                )

        self._task = asyncio.create_task(runner())
        return state

    async def cancel(self) -> bool:
        if self._current is None or self._current.status != "running":
            return False
        self._cancel.set()
        return True


def _stats_payload(stats: RunStats) -> dict:
    return {
        "run_id": stats.run_id,
        "total": stats.total,
        "moved": stats.moved,
        "review": stats.review,
        "unknown": stats.unknown,
        "failed": stats.failed,
        "reused": stats.reused,
        "deferred": stats.deferred,
        "deleted": stats.deleted,
        "elapsed": round(stats.elapsed, 1),
    }


__all__ = ["JobManager", "JobState", "Runner"]
