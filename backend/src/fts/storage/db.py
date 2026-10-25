from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..domain.models import (
    Action,
    Alternative,
    Decision,
    Fandom,
    ImageMeta,
    SearchResult,
    TargetKind,
)
from ..domain.naming import normalize_key
from ..domain.rules import rules_revision
from ..domain.similar import pick_tag_fandom
from . import paths as fspaths

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    ext       TEXT NOT NULL DEFAULT '',
    size      INTEGER NOT NULL DEFAULT 0,
    width     INTEGER NOT NULL DEFAULT 0,
    height    INTEGER NOT NULL DEFAULT 0,
    mtime     REAL NOT NULL DEFAULT 0,
    md5       TEXT,
    phash     INTEGER,
    seen_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    post_id     TEXT,
    post_url    TEXT,
    similarity  REAL NOT NULL DEFAULT 0,
    payload     TEXT,
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_searches_unique
    ON searches(image_id, source, COALESCE(post_url, ''));

CREATE TABLE IF NOT EXISTS decisions (
    image_id        INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    run_id          TEXT,
    kind            TEXT NOT NULL,
    action          TEXT NOT NULL,
    fandom          TEXT,
    character       TEXT,
    artist          TEXT,
    multi_character INTEGER NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    alternatives    TEXT,
    manual          INTEGER NOT NULL DEFAULT 0,
    decided_at      REAL NOT NULL,
    rules_revision  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL,
    image_id   INTEGER REFERENCES images(id) ON DELETE SET NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_run ON journal(run_id);

CREATE TABLE IF NOT EXISTS proxies (
    key       TEXT PRIMARY KEY,
    scheme    TEXT NOT NULL,
    host      TEXT NOT NULL,
    port      INTEGER NOT NULL,
    source    TEXT NOT NULL,
    country   TEXT,
    anonymity TEXT,
    latency   INTEGER,
    uptime    REAL,
    added_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_stats (
    proxy_key   TEXT NOT NULL,
    host        TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 0,
    fail        INTEGER NOT NULL DEFAULT 0,
    last_ok_at  REAL,
    last_fail_at REAL,
    latency_ms  INTEGER,
    PRIMARY KEY (proxy_key, host)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT NOT NULL,
    day   TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    used  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, day, scope)
);

CREATE TABLE IF NOT EXISTS rules (
    position INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    aliases  TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    id         INTEGER PRIMARY KEY,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(image_id)
);

-- Тег фандома, которого нет в словаре: только запоминаем, решение принимает владелец.
CREATE TABLE IF NOT EXISTS unmatched_tags (
    tag         TEXT PRIMARY KEY,
    hits        INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    example     TEXT
);

CREATE TABLE IF NOT EXISTS fandom_suggestions (
    tag        TEXT PRIMARY KEY,
    fandom     TEXT NOT NULL,
    similar_to TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_fandoms (
    tag    TEXT NOT NULL,
    fandom TEXT NOT NULL,
    votes  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tag, fandom)
);
"""


class Database:
    _SIGN = 1 << 63
    _MOD = 1 << 64

    @classmethod
    def _to_signed(cls, value: int | None) -> int | None:
        return None if value is None else (value - cls._MOD if value >= cls._SIGN else value)

    @classmethod
    def _to_unsigned(cls, value: int | None) -> int | None:
        return None if value is None else (value + cls._MOD if value < 0 else value)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rules_revision: str | None = None
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(fspaths.long_path(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
            }
            if "manual" not in columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN manual INTEGER NOT NULL DEFAULT 0"
                )
            if "rules_revision" not in columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN rules_revision TEXT NOT NULL DEFAULT ''"
                )
            image_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(images)").fetchall()
            }
            if "sha256" in image_columns:
                connection.execute("DROP INDEX IF EXISTS idx_images_sha256")
                connection.execute("ALTER TABLE images DROP COLUMN sha256")
            counter_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(counters)").fetchall()
            }
            if "scope" not in counter_columns:
                connection.executescript(
                    """
                    CREATE TABLE counters_new (
                        name  TEXT NOT NULL,
                        day   TEXT NOT NULL,
                        scope TEXT NOT NULL DEFAULT '',
                        used  INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (name, day, scope)
                    );
                    INSERT INTO counters_new (name, day, scope, used)
                        SELECT name, day, '', used FROM counters;
                    DROP TABLE counters;
                    ALTER TABLE counters_new RENAME TO counters;
                    """
                )
            rule_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(rules)").fetchall()
            }
            if "children" in rule_columns:
                legacy = connection.execute("SELECT name, aliases, children FROM rules").fetchall()
                for row in legacy:
                    aliases = list(json.loads(row["aliases"] or "[]"))
                    for child in json.loads(row["children"] or "[]"):
                        if child not in aliases:
                            aliases.append(child)
                    connection.execute(
                        "UPDATE rules SET aliases = ? WHERE name = ?",
                        (json.dumps(aliases, ensure_ascii=False), row["name"]),
                    )
                connection.execute("ALTER TABLE rules DROP COLUMN children")

    def upsert_image(self, meta: ImageMeta) -> int:
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO images
                    (path, ext, size, width, height, mtime, md5, phash, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size, width=excluded.width, height=excluded.height,
                    mtime=excluded.mtime,
                    md5=COALESCE(excluded.md5, images.md5),
                    phash=COALESCE(excluded.phash, images.phash),
                    seen_at=excluded.seen_at
                """,
                (
                    meta.path,
                    meta.ext,
                    meta.size,
                    meta.width,
                    meta.height,
                    meta.mtime,
                    meta.md5,
                    self._to_signed(meta.phash),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM images WHERE path = ?", (meta.path,)
            ).fetchone()
        return int(row["id"])

    def image_id(self, path: str) -> int | None:
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM images WHERE path = ?", (path,)).fetchone()
        return int(row["id"]) if row else None

    def latest_run_id(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM decisions WHERE run_id IS NOT NULL "
                "ORDER BY decided_at DESC LIMIT 1"
            ).fetchone()
        return str(row["run_id"]) if row else None

    def image_row(self, image_id: int) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return dict(row) if row else None

    def recent_decisions(
        self,
        limit: int = 100,
        offset: int = 0,
        run_id: str | None = None,
        under: str | None = None,
    ) -> list[dict]:
        query = """
            SELECT d.*, i.path, i.width, i.height, i.size
            FROM decisions d
            JOIN images i ON i.id = d.image_id
            WHERE d.run_id = ?
        """
        params: list[object] = [run_id]
        if under:
            query += " AND i.path LIKE ? ESCAPE '|'"
            escaped = under.replace("|", "||").replace("%", "|%").replace("_", "|_")
            params.append(escaped + os.sep + "%")
        query += " ORDER BY d.decided_at DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def searches_for(self, image_id: int) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source, status, post_url, similarity, payload FROM searches "
                "WHERE image_id = ? ORDER BY similarity DESC",
                (image_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_by_phash(self, phash: int, max_distance: int = 6) -> list[tuple[int, str, int]]:
        candidates: list[tuple[int, str, int]] = []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, path, phash FROM images WHERE phash IS NOT NULL"
            ).fetchall()
        for row in rows:
            stored = self._to_unsigned(int(row["phash"]))
            if stored is None:
                continue
            distance = fspaths.hamming(stored, phash)
            if distance <= max_distance:
                candidates.append((int(row["id"]), str(row["path"]), distance))
        candidates.sort(key=lambda item: item[2])
        return candidates

    def decided_phashes(self) -> list[tuple[int, int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT i.id AS id, i.phash AS phash FROM images i "
                "JOIN decisions d ON d.image_id = i.id WHERE i.phash IS NOT NULL"
            ).fetchall()
        result: list[tuple[int, int]] = []
        for row in rows:
            stored = self._to_unsigned(int(row["phash"]))
            if stored is not None:
                result.append((stored, int(row["id"])))
        return result

    def decision_for_unchanged(
        self, path: str, size: int, mtime: float
    ) -> tuple[int, Decision] | None:
        revision = self.rules_revision()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT d.* FROM images i JOIN decisions d ON d.image_id = i.id "
                "WHERE i.path = ? AND i.size = ? AND i.mtime = ? "
                "AND (d.rules_revision = ? OR d.manual = 1)",
                (path, size, mtime, revision),
            ).fetchone()
        if row is None:
            return None
        return int(row["image_id"]), self._row_to_decision(row)

    def move_image_path(self, image_id: int, new_path: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE images SET path = ? WHERE id = ?", (new_path, image_id))

    def move_path(self, old_path: str, new_path: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE images SET path = ? WHERE path = ?", (new_path, old_path)
            )
            return int(cursor.rowcount or 0)

    def save_search(self, image_id: int, result: SearchResult) -> bool:
        payload = json.dumps(
            {
                "fandom": list(result.fandom_tags),
                "artist": list(result.artist_tags),
                "character": list(result.character_tags),
                "image_url": result.image_url,
                "error": result.error,
            },
            ensure_ascii=False,
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO searches
                    (image_id, source, status, post_id, post_url, similarity, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    str(result.source),
                    str(result.status),
                    result.post_id,
                    result.post_url,
                    result.similarity,
                    payload,
                    time.time(),
                ),
            )
            return cursor.rowcount > 0

    def forget_decision(self, image_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM decisions WHERE image_id = ?", (image_id,))

    def save_decision(self, image_id: int, decision: Decision, run_id: str | None = None) -> None:
        alternatives = json.dumps(
            [
                {"fandom": a.fandom, "evidence": a.evidence, "best_similarity": a.best_similarity}
                for a in decision.alternatives
            ],
            ensure_ascii=False,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO decisions
                    (image_id, run_id, kind, action, fandom, character, artist,
                     multi_character, confidence, reason, alternatives, manual, decided_at,
                     rules_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    run_id=excluded.run_id, kind=excluded.kind, action=excluded.action,
                    fandom=excluded.fandom, character=excluded.character, artist=excluded.artist,
                    multi_character=excluded.multi_character, confidence=excluded.confidence,
                    reason=excluded.reason, alternatives=excluded.alternatives,
                    manual=excluded.manual, decided_at=excluded.decided_at,
                    rules_revision=excluded.rules_revision
                """,
                (
                    image_id,
                    run_id,
                    str(decision.kind),
                    str(decision.action),
                    decision.fandom,
                    decision.character,
                    decision.artist,
                    int(decision.multi_character),
                    decision.confidence,
                    decision.reason,
                    alternatives,
                    int(decision.manual),
                    time.time(),
                    self.rules_revision(),
                ),
            )

    def decision_run_id(self, image_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM decisions WHERE image_id = ?", (image_id,)
            ).fetchone()
        return str(row["run_id"]) if row and row["run_id"] else None

    def decision_for(self, image_id: int, revision: str | None = None) -> Decision | None:
        sql = "SELECT * FROM decisions WHERE image_id = ?"
        params: list[object] = [image_id]
        if revision is not None:
            sql += " AND (rules_revision = ? OR manual = 1)"
            params.append(revision)
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return self._row_to_decision(row) if row else None

    @staticmethod
    def _row_to_decision(row) -> Decision:
        alternatives = tuple(
            Alternative(
                fandom=item["fandom"],
                evidence=item["evidence"],
                best_similarity=item["best_similarity"],
            )
            for item in json.loads(row["alternatives"] or "[]")
        )
        return Decision(
            kind=TargetKind(row["kind"]),
            action=Action(row["action"]),
            fandom=row["fandom"],
            character=row["character"],
            artist=row["artist"],
            multi_character=bool(row["multi_character"]),
            confidence=float(row["confidence"]),
            reason=row["reason"],
            alternatives=alternatives,
            manual=bool(row["manual"]) if "manual" in row.keys() else False,  # noqa: SIM118
        )

    def add_journal(
        self, run_id: str, image_id: int | None, source: str, target: str, size: int
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO journal (run_id, image_id, source, target, size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, image_id, source, target, size, time.time()),
            )
            return int(cursor.lastrowid or 0)

    def commit_move(
        self, entry_id: int, image_id: int | None, target: str, size: int
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE journal SET target = ?, size = ? WHERE id = ?",
                (target, size, entry_id),
            )
            if image_id is not None:
                connection.execute(
                    "UPDATE images SET path = ? WHERE id = ?", (target, image_id)
                )
                connection.execute("DELETE FROM queue WHERE image_id = ?", (image_id,))

    def journal_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, COUNT(*) AS moves, MIN(created_at) AS started_at, "
                "MAX(created_at) AS finished_at, SUM(size) AS bytes FROM journal "
                "GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def repair_moved_paths(self, exists=None) -> int:
        check = exists or fspaths.exists
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT i.id AS image_id, i.path AS path, j.target AS target "
                "FROM journal j "
                "JOIN images i ON i.id = j.image_id "
                "JOIN (SELECT image_id, MAX(id) AS last_id FROM journal "
                "      WHERE image_id IS NOT NULL GROUP BY image_id) recent "
                "  ON recent.last_id = j.id"
            ).fetchall()

        fixed = 0
        for row in rows:
            path, target = str(row["path"]), str(row["target"])
            if path == target:
                continue
            if check(target) and not check(path):
                self.move_image_path(int(row["image_id"]), target)
                fixed += 1
        return fixed

    def journal_entries_for_run(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, image_id, source, target FROM journal WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_undo(self, run_id: str, restored: Iterable[dict]) -> None:
        with self.connect() as connection:
            for entry in restored:
                image_id = entry.get("image_id")
                if image_id is not None:
                    connection.execute(
                        "UPDATE images SET path = ? WHERE id = ?",
                        (str(entry["source"]), int(image_id)),
                    )
            connection.execute("DELETE FROM journal WHERE run_id = ?", (run_id,))

    def was_moved_to(self, image_id: int, path: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM journal WHERE image_id = ? AND target = ? LIMIT 1",
                (image_id, path),
            ).fetchone()
        return row is not None

    def last_move_for_image(self, image_id: int) -> tuple[int, str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, source, target FROM journal WHERE image_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (image_id,),
            ).fetchone()
        return (int(row["id"]), str(row["source"]), str(row["target"])) if row else None

    def drop_journal_entry(self, entry_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM journal WHERE id = ?", (entry_id,))

    def bump_counter(self, name: str, day: str, amount: int = 1, scope: str = "") -> int:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO counters (name, day, scope, used) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name, day, scope) DO UPDATE SET used = used + excluded.used",
                (name, day, scope, amount),
            )
            row = connection.execute(
                "SELECT used FROM counters WHERE name = ? AND day = ? AND scope = ?",
                (name, day, scope),
            ).fetchone()
        return int(row["used"])

    def get_counter(self, name: str, day: str, scope: str = "") -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT used FROM counters WHERE name = ? AND day = ? AND scope = ?",
                (name, day, scope),
            ).fetchone()
        return int(row["used"]) if row else 0

    def counters_sum(self, name: str, day: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT SUM(used) FROM counters WHERE name = ? AND day = ?", (name, day)
            ).fetchone()
        return int(row[0] or 0)

    def upsert_proxies(self, records: Iterable[object]) -> int:
        now = time.time()
        rows = [
            (
                record.key,
                record.scheme,
                record.host,
                record.port,
                record.source,
                record.country,
                record.anonymity,
                record.latency_ms,
                record.uptime,
                now,
            )
            for record in records
        ]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO proxies
                    (key, scheme, host, port, source, country, anonymity, latency, uptime, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    latency=COALESCE(excluded.latency, proxies.latency),
                    uptime=COALESCE(excluded.uptime, proxies.uptime)
                """,
                rows,
            )
        return len(rows)

    def record_proxy_result(
        self, proxy_key: str, host: str, ok: bool, latency_ms: int | None = None
    ) -> None:
        self.record_proxy_results([(proxy_key, host, ok, latency_ms)])

    def record_proxy_results(
        self, results: Iterable[tuple[str, str, bool, int | None]]
    ) -> None:
        now = time.time()
        rows = [
            (
                proxy_key,
                host,
                int(ok),
                int(not ok),
                now if ok else None,
                None if ok else now,
                latency_ms,
            )
            for proxy_key, host, ok, latency_ms in results
        ]
        if not rows:
            return
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO proxy_stats
                    (proxy_key, host, ok, fail, last_ok_at, last_fail_at, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proxy_key, host) DO UPDATE SET
                    ok = ok + excluded.ok,
                    fail = fail + excluded.fail,
                    last_ok_at = COALESCE(excluded.last_ok_at, proxy_stats.last_ok_at),
                    last_fail_at = COALESCE(excluded.last_fail_at, proxy_stats.last_fail_at),
                    latency_ms = COALESCE(excluded.latency_ms, proxy_stats.latency_ms)
                """,
                rows,
            )

    def best_proxies(self, host: str, limit: int = 10) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.proxy_key,
                       CAST(s.ok AS REAL) / (s.ok + s.fail + 1.0) AS score,
                       s.latency_ms
                FROM proxy_stats s
                WHERE s.host = ? AND s.ok > 0 AND s.ok >= s.fail
                ORDER BY score DESC, COALESCE(s.latency_ms, 99999) ASC
                LIMIT ?
                """,
                (host, limit),
            ).fetchall()
        return [row["proxy_key"] for row in rows]

    def untested_proxies(self, host: str, limit: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.key
                FROM proxies p
                LEFT JOIN proxy_stats s ON s.proxy_key = p.key AND s.host = ?
                WHERE s.proxy_key IS NULL
                ORDER BY COALESCE(p.latency, 99999) ASC, RANDOM()
                LIMIT ?
                """,
                (host, limit),
            ).fetchall()
        return [row["key"] for row in rows]

    def stale_proxies(self, host: str, limit: int, older_than: float) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.key
                FROM proxies p
                JOIN proxy_stats s ON s.proxy_key = p.key AND s.host = ?
                WHERE s.last_ok_at IS NOT NULL AND s.last_ok_at < ?
                ORDER BY s.last_ok_at ASC
                LIMIT ?
                """,
                (host, older_than, limit),
            ).fetchall()
        return [row["key"] for row in rows]

    def untested_proxies_any(self, limit: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.key
                FROM proxies p
                LEFT JOIN proxy_stats s ON s.proxy_key = p.key
                WHERE s.proxy_key IS NULL
                ORDER BY COALESCE(p.latency, 99999) ASC, RANDOM()
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row["key"] for row in rows]

    def stale_proxies_any(self, limit: int, older_than: float) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.proxy_key AS key, MIN(s.last_ok_at) AS oldest
                FROM proxy_stats s
                JOIN proxies p ON p.key = s.proxy_key
                WHERE s.last_ok_at IS NOT NULL AND s.last_ok_at < ?
                GROUP BY s.proxy_key
                ORDER BY oldest ASC
                LIMIT ?
                """,
                (older_than, limit),
            ).fetchall()
        return [row["key"] for row in rows]

    def proxy_summary(self) -> dict[str, int]:
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS n FROM proxies").fetchone()["n"]
            stats = connection.execute(
                "SELECT host, COUNT(*) AS n FROM proxy_stats WHERE ok > 0 AND ok >= fail "
                "GROUP BY host"
            ).fetchall()
        summary = {"total": int(total)}
        for row in stats:
            summary[str(row["host"])] = int(row["n"])
        return summary

    def proxy_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM proxies").fetchone()
        return int(row["n"])

    def drop_dead_proxies(self) -> int:
        with self.connect() as connection:
            dead = (
                "SELECT proxy_key FROM proxy_stats GROUP BY proxy_key HAVING SUM(ok) = 0"
            )
            removed = connection.execute(
                f"DELETE FROM proxies WHERE key IN ({dead})"
            ).rowcount
            connection.execute(f"DELETE FROM proxy_stats WHERE proxy_key IN ({dead})")
        return max(0, removed)

    def replace_rules(self, fandoms: Iterable[Fandom]) -> int:
        items = list(fandoms)
        with self.connect() as connection:
            connection.execute("DELETE FROM rules")
            connection.executemany(
                "INSERT INTO rules (position, name, aliases, priority) VALUES (?, ?, ?, ?)",
                [
                    (
                        position,
                        fandom.name,
                        json.dumps(list(fandom.aliases), ensure_ascii=False),
                        fandom.priority,
                    )
                    for position, fandom in enumerate(items)
                ],
            )
        self._rules_revision = None
        return len(items)

    def add_fandom(self, fandom: Fandom) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO rules (position, name, aliases, priority) "
                "VALUES ((SELECT COALESCE(MAX(position), -1) + 1 FROM rules), ?, ?, ?)",
                (
                    fandom.name,
                    json.dumps(list(fandom.aliases), ensure_ascii=False),
                    fandom.priority,
                ),
            )
            added = cursor.rowcount > 0
        self._rules_revision = None
        return added

    def add_alias(self, name: str, alias: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT aliases FROM rules WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return False
            aliases = json.loads(row["aliases"] or "[]")
            if alias in aliases:
                return False
            aliases.append(alias)
            connection.execute(
                "UPDATE rules SET aliases = ? WHERE name = ?",
                (json.dumps(aliases, ensure_ascii=False), name),
            )
        self._rules_revision = None
        return True

    def rules_revision(self) -> str:
        if self._rules_revision is None:
            self._rules_revision = rules_revision(self.load_rules())
        return self._rules_revision

    def load_rules(self) -> list[Fandom]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM rules ORDER BY position").fetchall()
        return [
            Fandom(
                name=row["name"],
                aliases=tuple(json.loads(row["aliases"] or "[]")),
                priority=int(row["priority"]),
            )
            for row in rows
        ]

    def rule_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM rules").fetchone()
        return int(row["n"])

    def learn_tags(self, pairs: Iterable[tuple[str, str]]) -> int:
        rows: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for tag, fandom in pairs:
            key = normalize_key(tag)
            name = str(fandom).strip()
            if not key or not name or (key, name) in seen:
                continue
            seen.add((key, name))
            rows.append((key, name))
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO tag_fandoms (tag, fandom, votes) VALUES (?, ?, 1)
                ON CONFLICT(tag, fandom) DO UPDATE SET votes = votes + 1
                """,
                rows,
            )
        return len(rows)

    def tag_votes(self) -> dict[str, dict[str, int]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT tag, fandom, votes FROM tag_fandoms").fetchall()
        votes: dict[str, dict[str, int]] = {}
        for row in rows:
            votes.setdefault(str(row["tag"]), {})[str(row["fandom"])] = int(row["votes"])
        return votes

    def tag_lookup(self) -> Callable[[str], str | None]:
        votes = self.tag_votes()
        return lambda tag: pick_tag_fandom(votes.get(normalize_key(tag), {}))

    def tag_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(DISTINCT tag) AS n FROM tag_fandoms").fetchone()
        return int(row["n"])

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def enqueue(self, image_id: int, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO queue (image_id, reason, created_at) VALUES (?, ?, ?)",
                (image_id, reason, time.time()),
            )

    def queued(self, limit: int = 500) -> list[tuple[int, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT q.image_id AS image_id, i.path AS path FROM queue q "
                "JOIN images i ON i.id = q.image_id ORDER BY q.created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [(int(row["image_id"]), str(row["path"])) for row in rows]

    def dequeue(self, image_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM queue WHERE image_id = ?", (image_id,))

    def queue_size(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM queue").fetchone()
        return int(row["n"])

    def record_unmatched(self, tags: Iterable[str], example: str = "") -> int:
        now = time.time()
        rows = [(tag, 1, now, now, example) for tag in {tag for tag in tags if tag}]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO unmatched_tags (tag, hits, first_seen, last_seen, example)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    hits = hits + excluded.hits,
                    last_seen = excluded.last_seen,
                    example = COALESCE(unmatched_tags.example, excluded.example)
                """,
                rows,
            )
        return len(rows)

    def unmatched_tags(self, limit: int = 200) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tag, hits, first_seen, last_seen, example FROM unmatched_tags "
                "ORDER BY hits DESC, tag ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unmatched_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM unmatched_tags").fetchone()
        return int(row["n"])

    def forget_unmatched(self, tags: Iterable[str]) -> int:
        items = [tag for tag in tags if tag]
        if not items:
            return 0
        with self.connect() as connection:
            connection.executemany(
                "DELETE FROM unmatched_tags WHERE tag = ?", [(tag,) for tag in items]
            )
        return len(items)

    def record_suggestion(self, tag: str, fandom: str, similar_to: str, score: float) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO fandom_suggestions (tag, fandom, similar_to, score, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tag) DO UPDATE SET "
                "fandom = excluded.fandom, similar_to = excluded.similar_to, "
                "score = MAX(fandom_suggestions.score, excluded.score)",
                (tag, fandom, similar_to, score, time.time()),
            )

    def suggestions(self, limit: int = 200) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tag, fandom, similar_to, score, created_at FROM fandom_suggestions "
                "ORDER BY score DESC, created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def suggestion_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM fandom_suggestions").fetchone()
        return int(row["n"])

    def forget_suggestion(self, tag: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM fandom_suggestions WHERE tag = ?", (tag,))
            return int(cursor.rowcount or 0)
