from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


BATCH = "batch"
FILE_STARTED = "file_started"
FILE_DECIDED = "file_decided"
FILE_MOVED = "file_moved"
FILE_FAILED = "file_failed"
FILE_DROPPED = "file_dropped"
DEFERRED = "deferred"
DONE = "done"
