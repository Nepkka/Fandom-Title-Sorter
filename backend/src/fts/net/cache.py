from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from ..storage.paths import long_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    status     INTEGER NOT NULL,
    body       BLOB NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_http_cache_expires ON http_cache(expires_at);
"""


class HttpCache:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            connection.executescript(SCHEMA)

    def get(self, key: str) -> tuple[int, bytes] | None:
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            row = connection.execute(
                "SELECT status, body, expires_at FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        status, body, expires_at = row
        if expires_at is not None and float(expires_at) < time.time():
            return None
        return int(status), bytes(body)

    def put(
        self,
        key: str,
        url: str,
        status: int,
        body: bytes,
        ttl: float | None = None,
    ) -> None:
        expires_at = None if ttl is None else time.time() + ttl
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            connection.execute(
                "INSERT INTO http_cache (key, url, status, body, stored_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "status=excluded.status, body=excluded.body, "
                "stored_at=excluded.stored_at, expires_at=excluded.expires_at",
                (key, url, status, body, time.time(), expires_at),
            )

    def size(self) -> int:
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            row = connection.execute("SELECT COUNT(*) FROM http_cache").fetchone()
        return int(row[0])

    def drop(self, key: str) -> bool:
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            cursor = connection.execute("DELETE FROM http_cache WHERE key = ?", (key,))
        return bool(cursor.rowcount)

    def purge_expired(self, max_age_seconds: float | None = None) -> int:
        with sqlite3.connect(long_path(self.path), timeout=30.0) as connection:
            cursor = connection.execute(
                "DELETE FROM http_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (time.time(),),
            )
            removed = cursor.rowcount
            if max_age_seconds is not None:
                cursor = connection.execute(
                    "DELETE FROM http_cache WHERE stored_at < ?",
                    (time.time() - max_age_seconds,),
                )
                removed += cursor.rowcount
        return removed
