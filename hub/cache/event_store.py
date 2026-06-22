"""SQLite-backed event store for persisting dashboard events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

# Default location for the database
DEFAULT_DB_PATH = Path.home() / ".moolmesh" / "events.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    project TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    summary TEXT NOT NULL,
    session_id TEXT,
    tokens_json TEXT,
    tool_name TEXT,
    file_path TEXT,
    model TEXT,
    cwd TEXT,
    fingerprint TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_provider ON events(provider);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_fingerprint
    ON events(fingerprint) WHERE fingerprint IS NOT NULL;
"""


def file_fingerprint(path: Path) -> str:
    """Generate a content-based fingerprint from the first 1KB of a file.

    Uses SHA-256 of the first 1024 bytes. This identifies files regardless
    of path or inode, surviving renames and inode recycling (common on APFS).
    """
    try:
        with open(path, "rb") as f:
            header = f.read(1024)
        return hashlib.sha256(header).hexdigest()[:32]  # 32 hex chars = 128 bits
    except OSError:
        return ""


def _compute_fingerprint(event_dict: dict[str, Any]) -> str:
    """Compute a unique fingerprint for deduplication.

    Uses provider + session_id + timestamp + event_type + summary
    to uniquely identify an event.
    """
    key = "|".join([
        event_dict.get("provider", ""),
        event_dict.get("session_id") or "",
        event_dict.get("timestamp", ""),
        event_dict.get("event_type", ""),
        event_dict.get("summary", ""),
    ])
    return hashlib.md5(key.encode()).hexdigest()


class EventStore:
    """Thread-safe SQLite event persistence."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA mmap_size=268435456")   # 256MB — zero-copy reads
        self._conn.execute("PRAGMA temp_store=MEMORY")       # sorts in RAM
        self._conn.execute("PRAGMA cache_size=-65536")        # 64MB page cache
        self._conn.execute("PRAGMA busy_timeout=5000")        # 5s wait on lock contention
        self._lock = threading.Lock()

        # Check if migration is needed (table exists but no fingerprint column)
        if self._needs_migration():
            self._migrate_schema()
        else:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

        self._ensure_registry()

    def _needs_migration(self) -> bool:
        """Check if the existing table needs the fingerprint column migration."""
        try:
            columns = [row[1] for row in self._conn.execute("PRAGMA table_info(events)")]
            if not columns:
                return False  # Table doesn't exist yet — no migration needed
            return "fingerprint" not in columns
        except sqlite3.OperationalError:
            return False  # Table doesn't exist — no migration needed

    def _migrate_schema(self) -> None:
        """Recreate the events table with fingerprint column, preserving existing data."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS events_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                project TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                summary TEXT NOT NULL,
                session_id TEXT,
                tokens_json TEXT,
                tool_name TEXT,
                file_path TEXT,
                model TEXT,
                cwd TEXT,
                fingerprint TEXT,
                created_at REAL NOT NULL
            );

            INSERT OR IGNORE INTO events_new
                (id, provider, project, event_type, timestamp, summary,
                 session_id, tokens_json, tool_name, file_path, model, cwd,
                 fingerprint, created_at)
            SELECT
                id, provider, project, event_type, timestamp, summary,
                session_id, tokens_json, tool_name, file_path, model, cwd,
                NULL,
                created_at
            FROM events;

            DROP TABLE IF EXISTS events;
            ALTER TABLE events_new RENAME TO events;
        """)
        # Recreate indexes
        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_provider ON events(provider);
            CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_fingerprint
                ON events(fingerprint) WHERE fingerprint IS NOT NULL;
        """)
        self._conn.commit()

    def _ensure_registry(self) -> None:
        """Create the file_registry table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS file_registry (
                fingerprint TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                file_path TEXT NOT NULL,
                last_offset INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared connection. Callers MUST hold self._lock."""
        return self._conn

    def store(self, event_dict: dict[str, Any]) -> None:
        """Store a single event."""
        import time
        tokens = event_dict.get("tokens")
        tokens_json = json.dumps(tokens) if tokens else None
        fingerprint = _compute_fingerprint(event_dict)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR IGNORE INTO events
                   (provider, project, event_type, timestamp, summary,
                    session_id, tokens_json, tool_name, file_path, model, cwd,
                    fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_dict.get("provider", ""),
                    event_dict.get("project", ""),
                    event_dict.get("event_type", ""),
                    event_dict.get("timestamp", ""),
                    event_dict.get("summary", ""),
                    event_dict.get("session_id"),
                    tokens_json,
                    event_dict.get("tool_name"),
                    event_dict.get("file_path"),
                    event_dict.get("model"),
                    event_dict.get("cwd"),
                    fingerprint,
                    time.time(),
                ),
            )
            conn.commit()

    def store_batch(self, events: list[dict[str, Any]]) -> None:
        """Store multiple events in a single transaction.

        Uses INSERT OR IGNORE with fingerprint-based deduplication,
        so running backfill --full multiple times is safe.
        """
        if not events:
            return
        import time
        now = time.time()
        rows = []
        for e in events:
            tokens = e.get("tokens")
            rows.append((
                e.get("provider", ""),
                e.get("project", ""),
                e.get("event_type", ""),
                e.get("timestamp", ""),
                e.get("summary", ""),
                e.get("session_id"),
                json.dumps(tokens) if tokens else None,
                e.get("tool_name"),
                e.get("file_path"),
                e.get("model"),
                e.get("cwd"),
                _compute_fingerprint(e),
                now,
            ))
        with self._lock:
            conn = self._get_conn()
            conn.executemany(
                """INSERT OR IGNORE INTO events
                   (provider, project, event_type, timestamp, summary,
                    session_id, tokens_json, tool_name, file_path, model, cwd,
                    fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()

    def load_recent(self, limit: int = 500) -> list[dict[str, Any]]:
        """Load the most recent N events, including their SQLite IDs."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id, provider, project, event_type, timestamp, summary,
                          session_id, tokens_json, tool_name, file_path, model, cwd
                   FROM events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        events = []
        for row in reversed(rows):
            e: dict[str, Any] = {
                "id": row[0],
                "provider": row[1],
                "project": row[2],
                "event_type": row[3],
                "timestamp": row[4],
                "summary": row[5],
            }
            if row[6]:
                e["session_id"] = row[6]
            if row[7]:
                e["tokens"] = json.loads(row[7])
            if row[8]:
                e["tool_name"] = row[8]
            if row[9]:
                e["file_path"] = row[9]
            if row[10]:
                e["model"] = row[10]
            if row[11]:
                e["cwd"] = row[11]
            events.append(e)
        return events

    def get_project_summary(self) -> list[dict[str, Any]]:
        """Get per-project aggregated stats directly from SQLite."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT
                    provider,
                    project,
                    MAX(cwd) AS cwd,
                    COUNT(*) AS events,
                    COUNT(DISTINCT session_id) AS sessions,
                    SUM(CASE WHEN tokens_json IS NOT NULL
                        THEN COALESCE(json_extract(tokens_json, '$.input'), 0) ELSE 0 END) AS input_tokens,
                    SUM(CASE WHEN tokens_json IS NOT NULL
                        THEN COALESCE(json_extract(tokens_json, '$.output'), 0) ELSE 0 END) AS output_tokens,
                    SUM(CASE WHEN tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls,
                    MAX(timestamp) AS last_event,
                    MIN(timestamp) AS first_event
                FROM events
                GROUP BY provider, project
                ORDER BY last_event DESC
            """).fetchall()

            results = []
            for row in rows:
                provider, project, cwd, events, sessions, in_tok, out_tok, tools, last_ev, first_ev = row
                models_rows = conn.execute(
                    "SELECT DISTINCT model FROM events WHERE provider=? AND project=? AND model IS NOT NULL",
                    (provider, project),
                ).fetchall()
                results.append({
                    "provider": provider or "",
                    "project": project or "unknown",
                    "cwd": cwd or "",
                    "sessions": sessions or 0,
                    "events": events or 0,
                    "input_tokens": in_tok or 0,
                    "output_tokens": out_tok or 0,
                    "tool_calls": tools or 0,
                    "models": sorted(m[0] for m in models_rows),
                    "last_event": last_ev or "",
                    "last_event_type": "",
                })
            return results

    def load_since_id(self, last_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Load events with id > last_id, ordered ascending.

        Used for SSE replay after client reconnection. The id is the
        SQLite autoincrement — monotonically increasing, gap-free.

        Returns list of dicts with an extra 'id' field for the SSE event ID.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id, provider, project, event_type, timestamp, summary,
                          session_id, tokens_json, tool_name, file_path, model, cwd
                   FROM events WHERE id > ? ORDER BY id ASC LIMIT ?""",
                (last_id, limit),
            ).fetchall()

        events = []
        for row in rows:
            e: dict[str, Any] = {
                "id": row[0],
                "provider": row[1],
                "project": row[2],
                "event_type": row[3],
                "timestamp": row[4],
                "summary": row[5],
            }
            if row[6]:
                e["session_id"] = row[6]
            if row[7]:
                e["tokens"] = json.loads(row[7])
            if row[8]:
                e["tool_name"] = row[8]
            if row[9]:
                e["file_path"] = row[9]
            if row[10]:
                e["model"] = row[10]
            if row[11]:
                e["cwd"] = row[11]
            events.append(e)
        return events

    def get_max_id(self) -> int:
        """Return the highest event ID in the store, or 0 if empty."""
        with self._lock:
            row = self._get_conn().execute("SELECT MAX(id) FROM events").fetchone()
        return row[0] or 0

    def query(
        self,
        provider: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Query events with filters."""
        where_parts: list[str] = []
        params: list[Any] = []
        if provider:
            where_parts.append("provider = ?")
            params.append(provider)
        if project:
            where_parts.append("project LIKE ?")
            params.append(f"%{project}%")
        if session_id:
            where_parts.append("session_id = ?")
            params.append(session_id)
        if event_type:
            where_parts.append("event_type = ?")
            params.append(event_type)
        if since:
            where_parts.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(where_parts) if where_parts else "1=1"
        params.append(limit)

        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                f"""SELECT provider, project, event_type, timestamp, summary,
                           session_id, tokens_json, tool_name, file_path, model, cwd
                    FROM events WHERE {where}
                    ORDER BY timestamp DESC LIMIT ?""",
                params,
            ).fetchall()

        events = []
        for row in reversed(rows):
            e: dict[str, Any] = {
                "provider": row[0],
                "project": row[1],
                "event_type": row[2],
                "timestamp": row[3],
                "summary": row[4],
            }
            if row[5]:
                e["session_id"] = row[5]
            if row[6]:
                e["tokens"] = json.loads(row[6])
            if row[7]:
                e["tool_name"] = row[7]
            if row[8]:
                e["file_path"] = row[8]
            if row[9]:
                e["model"] = row[9]
            if row[10]:
                e["cwd"] = row[10]
            events.append(e)
        return events

    def count(self) -> int:
        with self._lock:
            return self._get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def stats_summary(self) -> dict[str, Any]:
        """Get aggregate stats from stored events."""
        with self._lock:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            providers = conn.execute(
                "SELECT provider, COUNT(*) FROM events GROUP BY provider"
            ).fetchall()
            projects = conn.execute(
                "SELECT project, COUNT(*) FROM events GROUP BY project ORDER BY COUNT(*) DESC LIMIT 20"
            ).fetchall()
        return {
            "total_events": total,
            "by_provider": {r[0]: r[1] for r in providers},
            "top_projects": [{"project": r[0], "events": r[1]} for r in projects],
        }

    def analytics(self, since: str | None = None) -> dict[str, Any]:
        """Aggregated analytics for the charts dashboard.

        If *since* is given (ISO timestamp), only events after that point
        are included.  Otherwise all events are analysed.
        """
        if since:
            where = "timestamp >= ?"
            params: list = [since]
        else:
            where = "1=1"
            params = []

        with self._lock:
            conn = self._get_conn()

            total = conn.execute(
                f"SELECT COUNT(*) FROM events WHERE {where}", params
            ).fetchone()[0]

            # By provider
            by_provider = conn.execute(
                f"""SELECT provider, COUNT(*),
                           SUM(CASE WHEN tokens_json IS NOT NULL
                               THEN json_extract(tokens_json, '$.input') ELSE 0 END),
                           SUM(CASE WHEN tokens_json IS NOT NULL
                               THEN json_extract(tokens_json, '$.output') ELSE 0 END)
                    FROM events WHERE {where} GROUP BY provider""", params
            ).fetchall()

            # By event type
            by_type = conn.execute(
                f"SELECT event_type, COUNT(*) FROM events WHERE {where} GROUP BY event_type ORDER BY COUNT(*) DESC",
                params
            ).fetchall()

            # By tool
            by_tool = conn.execute(
                f"""SELECT tool_name, COUNT(*) FROM events
                    WHERE {where} AND tool_name IS NOT NULL
                    GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 20""", params
            ).fetchall()

            # By project
            by_project = conn.execute(
                f"""SELECT project, provider, COUNT(*),
                           SUM(CASE WHEN tokens_json IS NOT NULL
                               THEN COALESCE(json_extract(tokens_json, '$.input'),0)
                                    + COALESCE(json_extract(tokens_json, '$.output'),0)
                               ELSE 0 END)
                    FROM events WHERE {where}
                    GROUP BY project, provider ORDER BY COUNT(*) DESC LIMIT 20""", params
            ).fetchall()

            # By model
            by_model = conn.execute(
                f"""SELECT model, COUNT(*) FROM events
                    WHERE {where} AND model IS NOT NULL
                    GROUP BY model ORDER BY COUNT(*) DESC""", params
            ).fetchall()

            # Hourly activity (for area chart) — group by truncated hour
            hourly = conn.execute(
                f"""SELECT SUBSTR(timestamp, 1, 13) AS hour, provider, COUNT(*)
                    FROM events WHERE {where} AND LENGTH(timestamp) >= 13
                    GROUP BY hour, provider ORDER BY hour""", params
            ).fetchall()

            # Total tokens
            tok = conn.execute(
                f"""SELECT
                      SUM(CASE WHEN tokens_json IS NOT NULL THEN json_extract(tokens_json, '$.input') ELSE 0 END),
                      SUM(CASE WHEN tokens_json IS NOT NULL THEN json_extract(tokens_json, '$.output') ELSE 0 END),
                      SUM(CASE WHEN tokens_json IS NOT NULL THEN COALESCE(json_extract(tokens_json, '$.cached_input'),0)
                           + COALESCE(json_extract(tokens_json, '$.cache_read'),0) ELSE 0 END)
                    FROM events WHERE {where}""", params
            ).fetchone()

            # Sessions count
            sessions = conn.execute(
                f"SELECT COUNT(DISTINCT session_id) FROM events WHERE {where} AND session_id IS NOT NULL",
                params
            ).fetchone()[0]

            # Projects count
            projects_count = conn.execute(
                f"SELECT COUNT(DISTINCT project) FROM events WHERE {where}", params
            ).fetchone()[0]

        return {
            "total_events": total,
            "total_sessions": sessions,
            "total_projects": projects_count,
            "tokens": {
                "input": tok[0] or 0,
                "output": tok[1] or 0,
                "cached": tok[2] or 0,
            },
            "by_provider": [
                {"provider": r[0], "events": r[1], "input_tokens": r[2] or 0, "output_tokens": r[3] or 0}
                for r in by_provider
            ],
            "by_type": [{"type": r[0], "count": r[1]} for r in by_type],
            "by_tool": [{"name": r[0], "count": r[1]} for r in by_tool],
            "by_project": [
                {"project": r[0], "provider": r[1], "events": r[2], "tokens": r[3] or 0}
                for r in by_project
            ],
            "by_model": [{"model": r[0], "count": r[1]} for r in by_model],
            "hourly": self._pack_hourly(hourly),
        }

    @staticmethod
    def _pack_hourly(rows: list) -> list[dict]:
        """Pack hourly rows into [{hour, claude, codex, qwen, total}]."""
        hours: dict[str, dict] = {}
        for hour, provider, count in rows:
            if hour not in hours:
                hours[hour] = {"hour": hour, "claude": 0, "codex": 0, "qwen": 0, "total": 0}
            hours[hour][provider] = count
            hours[hour]["total"] += count
        return list(hours.values())

    def get_offset(self, fingerprint: str) -> int | None:
        """Get the last known byte offset for a file by its content fingerprint.

        Returns None if the file has never been registered.
        """
        if not fingerprint:
            return None
        with self._lock:
            row = self._get_conn().execute(
                "SELECT last_offset FROM file_registry WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return row[0] if row else None

    def save_offset(self, fingerprint: str, provider: str, file_path: str, offset: int) -> None:
        """Save or update the byte offset for a file."""
        import time
        with self._lock:
            self._get_conn().execute(
                """INSERT INTO file_registry (fingerprint, provider, file_path, last_offset, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       last_offset = excluded.last_offset,
                       file_path = excluded.file_path,
                       updated_at = excluded.updated_at""",
                (fingerprint, provider, str(file_path), offset, time.time()),
            )
            self._get_conn().commit()

    def store_with_offset(
        self,
        events: list[dict],
        fingerprint: str,
        provider: str,
        file_path: str,
        new_offset: int,
    ) -> list[dict]:
        """Store events and update file offset in a single atomic transaction.

        Returns the events with their assigned SQLite IDs (for SSE broadcast).
        Duplicates (INSERT OR IGNORE that don't insert) are NOT returned.
        If the process crashes mid-write, both the events AND the offset
        roll back, so the next read resumes from the correct position.
        """
        if not events and not fingerprint:
            return []
        import time
        now = time.time()
        rows = []
        for e in events:
            tokens = e.get("tokens")
            rows.append((
                e.get("provider", ""),
                e.get("project", ""),
                e.get("event_type", ""),
                e.get("timestamp", ""),
                e.get("summary", ""),
                e.get("session_id"),
                json.dumps(tokens) if tokens else None,
                e.get("tool_name"),
                e.get("file_path"),
                e.get("model"),
                e.get("cwd"),
                _compute_fingerprint(e),
                now,
            ))

        result_events = []
        with self._lock:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for i, row in enumerate(rows):
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO events
                           (provider, project, event_type, timestamp, summary,
                            session_id, tokens_json, tool_name, file_path, model, cwd,
                            fingerprint, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        row,
                    )
                    if cursor.rowcount > 0:
                        # Event was inserted (not a duplicate)
                        ev = dict(events[i])
                        ev["id"] = cursor.lastrowid
                        result_events.append(ev)

                if fingerprint:
                    conn.execute(
                        """INSERT INTO file_registry (fingerprint, provider, file_path, last_offset, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(fingerprint) DO UPDATE SET
                               last_offset = excluded.last_offset,
                               file_path = excluded.file_path,
                               updated_at = excluded.updated_at""",
                        (fingerprint, provider, str(file_path), new_offset, now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return result_events

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_last_timestamp_per_provider(self) -> dict[str, str]:
        """DEPRECATED: Use file_registry offsets instead.
        Kept for backwards compatibility with external scripts.

        Returns dict like {"claude": "2026-04-08T22:28:29", "codex": "2026-04-08T16:00:00"}.
        Empty dict if no events exist.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT provider, MAX(timestamp) FROM events GROUP BY provider"
            ).fetchall()
        return {r[0]: r[1] for r in rows if r[1]}

    def get_last_timestamp_per_session(self, provider: str) -> dict[str, str]:
        """DEPRECATED: Use file_registry offsets instead.
        Kept for backwards compatibility with external scripts.

        Returns dict like {"session-abc": "2026-04-10T08:11:10", "session-xyz": "2026-04-10T08:12:59"}.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT session_id, MAX(timestamp) FROM events "
                "WHERE provider = ? AND session_id IS NOT NULL "
                "GROUP BY session_id",
                (provider,),
            ).fetchall()
        return {r[0]: r[1] for r in rows if r[0] and r[1]}

    def has_events(self) -> bool:
        """Check if there are any stored events."""
        return self.count() > 0
