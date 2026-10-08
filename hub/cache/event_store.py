"""SQLite-backed event store for persisting dashboard events."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

_log = logging.getLogger("moolmesh.event_store")

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


def _mig_1_session_lifecycle(conn: sqlite3.Connection) -> None:
    """Add additive session-lifecycle columns to pre-existing DBs.

    Fresh DBs already get these from ``_ensure_sessions_table``; the guard on
    ``PRAGMA table_info`` makes this a no-op there, avoiding a duplicate-column
    error while still backfilling databases created before this column existed.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "ended_at" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at TEXT")
    if "ended_reason" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_reason TEXT")


def _mig_2_watcher_state(conn: sqlite3.Connection) -> None:
    """Per-provider watcher heartbeat for the startup catch-up (issue #45).

    One row per file-based provider: the wall-clock time of the watcher's last
    completed rescan. On startup a gap longer than the live window means the
    daemon was down, so the first pass widens its cutoff to this time. A
    dedicated table (not ``file_registry``, which is keyed by file fingerprint
    and read as "one row per session file") keeps both shapes clean.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watcher_state (
            provider TEXT PRIMARY KEY,
            last_cycle_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)


def _mig_3_historical_flag(conn: sqlite3.Connection) -> None:
    """Mark events ingested by a non-live path (issue #45).

    ``mool backfill``, the daemon's startup catch-up and ``--reparse codex``
    insert OLD events with NEW ids, so every "highest id = most recent" reader
    (the dashboard's recent feed, the startup stats tracker, SSE replay, MCP
    ``get_recent_events``) would surface March history as current activity.
    Those paths write ``historical = 1``; the live watcher keeps the default 0.
    Rows that already exist stay 0: they were ingested live. The flag is NOT
    part of the event fingerprint, so a re-harvest still dedupes.

    The partial index keeps ``load_recent`` a bounded index walk even when the
    newest ids are hundreds of thousands of historical rows.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "historical" not in columns:
        conn.execute(
            "ALTER TABLE events ADD COLUMN historical INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_live ON events(id) WHERE historical = 0"
    )


# ``file_registry`` identifies a file by (key, path): ``fingerprint`` is the
# content fingerprint of the first KB (or a provider's stable key, see
# ``BaseHarvester.registry_key``) and ``file_path`` is ``registry_path()``.
# ``legacy = 1`` marks offsets written while the table was keyed by the
# fingerprint alone (#50): two files sharing their first KB shared that offset,
# so ``mool backfill`` re-reads such files from 0 once and clears the flag.
_REGISTRY_DDL = """
    CREATE TABLE IF NOT EXISTS {name} (
        fingerprint TEXT NOT NULL,
        provider TEXT NOT NULL,
        file_path TEXT NOT NULL,
        last_offset INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        legacy INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (fingerprint, file_path)
    )
"""


def registry_path(path: str | Path) -> str:
    """The one normalization of a path used as part of a ``file_registry`` key.

    Every read and write of the registry goes through it: a lookup that
    normalized differently from the write would miss its own row and re-read
    the file from 0 on every poll. ``normcase`` folds case on Windows only.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def _mig_4_registry_path_key(conn: sqlite3.Connection) -> None:
    """Key ``file_registry`` by (fingerprint, file_path) — issue #50.

    SQLite cannot change a PRIMARY KEY in place, and a new unique index on
    (fingerprint, file_path) would not help while the old PK still rejects a
    second path per fingerprint, so the table is rebuilt: new table, copy every
    row (paths through ``registry_path``, ``legacy = 1``), drop, rename — in
    one explicit transaction. Idempotent: a composite PK means it already ran
    (or the DB was created fresh by ``_ensure_registry``); a leftover
    ``file_registry_new`` from an interrupted run is dropped first.
    """
    info = conn.execute("PRAGMA table_info(file_registry)").fetchall()
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    if pk == ["fingerprint", "file_path"]:
        return
    rows = conn.execute(
        "SELECT fingerprint, provider, file_path, last_offset, updated_at FROM file_registry"
    ).fetchall()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS file_registry_new")
        conn.execute(_REGISTRY_DDL.format(name="file_registry_new"))
        conn.executemany(
            """INSERT OR IGNORE INTO file_registry_new
                   (fingerprint, provider, file_path, last_offset, updated_at, legacy)
               VALUES (?, ?, ?, ?, ?, 1)""",
            [(fp, prov, registry_path(path), off, upd) for fp, prov, path, off, upd in rows],
        )
        conn.execute("DROP TABLE file_registry")
        conn.execute("ALTER TABLE file_registry_new RENAME TO file_registry")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def resolve_registry_offset(
    rows: list[tuple[str, int, float]], path: str | Path
) -> tuple[int | None, str | None]:
    """Pick the offset for ``path`` among one key's registry rows (#50).

    ``rows`` are ``(file_path, last_offset, updated_at)`` sharing a key.
    Returns ``(offset, adopt_from)``:

    * the row for this exact path → its offset;
    * else a row whose file no longer exists on disk → a rename: its offset,
      and ``adopt_from`` names the row to move onto the new path (the reason
      the key is content-based in the first place);
    * else every other row points at a live file → a collision: a different
      file that starts with the same bytes, read from 0 (``None``).
    """
    want = registry_path(path)
    for row_path, offset, _updated in rows:
        if registry_path(row_path) == want:
            return offset, None
    gone = [r for r in rows if not os.path.exists(r[0])]
    if gone:
        row_path, offset, _updated = max(gone, key=lambda r: r[2])
        return offset, row_path
    return None, None


def _mig_5_opencode_first_ingest_day(conn: sqlite3.Connection) -> None:
    """Mark pre-#45 live OpenCode rows that predate the provider's first day.

    OpenCode sessions first ingested LIVE (``historical = 0``) before #45 got
    their activity clock on the install day, even though their events are up to
    months older (issue #61): the live ingest epoch is the import moment, not
    the work. This one-off migration marks ``historical = 1`` on rows that are
    (a) OpenCode, (b) still live, (c) ingested on the provider's FIRST ingest
    day — the local day of ``MIN(created_at)`` for OpenCode — and (d) whose
    parsed event time is STRICTLY BEFORE that day. Bounded on purpose: only the
    first-day window is examined, and a row with an unreadable timestamp is
    left untouched (never guess). ``_historical_event_dt`` (the #53 definition,
    imported lazily to keep the two stores' import direction one-way) parses
    the mixed ISO/numeric timestamp formats.

    Idempotent: rows are updated only once and a second run finds no live
    first-day rows ahead of their own event time. Rows ingested later stay live.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "historical" not in columns:
        return  # pre-#45 schema; migration 3 adds the column first
    row = conn.execute(
        "SELECT MIN(created_at) FROM events WHERE provider = 'opencode'"
    ).fetchone()
    if not row or row[0] is None:
        return
    from datetime import date, datetime, time as dtime, timedelta

    first_day = date.fromtimestamp(float(row[0]))  # local install day
    start = datetime.combine(first_day, dtime.min).timestamp()
    end = datetime.combine(first_day + timedelta(days=1), dtime.min).timestamp()
    rows = conn.execute(
        """SELECT id, timestamp FROM events
           WHERE provider = 'opencode' AND historical = 0
             AND created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchall()
    if not rows:
        return
    from hub.cache.workspace_store import _historical_event_dt

    ids = [(eid,) for eid, ts in rows
           if (dt := _historical_event_dt(ts, None)) is not None
           and dt.timestamp() < start]
    if ids:
        conn.executemany("UPDATE events SET historical = 1 WHERE id = ?", ids)


# Versioned, additive migrations for events.db — each runs exactly once.
_EVENT_STORE_MIGRATIONS = [
    (1, "session_lifecycle", _mig_1_session_lifecycle),
    (2, "watcher_state", _mig_2_watcher_state),
    (3, "historical_flag", _mig_3_historical_flag),
    (4, "registry_path_key", _mig_4_registry_path_key),
    (5, "opencode_first_ingest_day", _mig_5_opencode_first_ingest_day),
]


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


# Scalar columns of ``events`` / ``sessions``. Every writer funnels provider
# data through ``_scalar``/``_normalize_event`` first, so a format change (a
# dict where a string used to be) can never surface as
# ``sqlite3.ProgrammingError: type 'dict' is not supported`` and take down the
# watcher thread with it (issue #65).
_EVENT_SCALAR_KEYS = (
    "provider", "project", "event_type", "timestamp", "summary",
    "session_id", "tool_name", "file_path", "model", "cwd",
)
_SCALAR_MAX_LEN = 2000


def _scalar(value: Any) -> Any:
    """Coerce any JSON value for a scalar column.

    ``None`` and strings pass through; numbers/booleans stringify; dicts and
    lists become compact, key-sorted JSON (stable across re-parses) truncated
    to a bounded length. Never raises.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return ""
    return text[:_SCALAR_MAX_LEN]


def _tokens_json(value: Any) -> str | None:
    """``json.dumps`` of a tokens payload, or None when absent/unserializable."""
    if not value:
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``event`` whose scalar columns hold only scalars (#65).

    The fingerprint is computed from the normalized copy, so a dict that used
    to crash the write now both stores and dedupes deterministically.
    """
    normalized = dict(event)
    for key in _EVENT_SCALAR_KEYS:
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            normalized[key] = _scalar(value)
    return normalized


def _compute_fingerprint(event_dict: dict[str, Any]) -> str:
    """Compute a unique fingerprint for deduplication.

    Uses provider + session_id + timestamp + event_type + summary
    to uniquely identify an event. Callers pass an event normalized by
    ``_normalize_event`` so the fingerprint is stable regardless of value
    shape.
    """
    key = "|".join([
        str(event_dict.get("provider", "") or ""),
        str(event_dict.get("session_id") or ""),
        str(event_dict.get("timestamp", "") or ""),
        str(event_dict.get("event_type", "") or ""),
        str(event_dict.get("summary", "") or ""),
    ])
    return hashlib.md5(key.encode()).hexdigest()


def _insert_event_row(
    conn: sqlite3.Connection, e: dict[str, Any], now: float, historical: bool = False
) -> int:
    """INSERT OR IGNORE one event (+ its full text); 1 if inserted, else 0."""
    e = _normalize_event(e)
    cursor = conn.execute(
        """INSERT OR IGNORE INTO events
           (provider, project, event_type, timestamp, summary,
            session_id, tokens_json, tool_name, file_path, model, cwd,
            fingerprint, created_at, historical)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            e.get("provider", ""), e.get("project", ""), e.get("event_type", ""),
            e.get("timestamp", ""), e.get("summary", ""), e.get("session_id"),
            _tokens_json(e.get("tokens")), e.get("tool_name"),
            e.get("file_path"), e.get("model"), e.get("cwd"),
            _compute_fingerprint(e), now, 1 if historical else 0,
        ),
    )
    if cursor.rowcount <= 0:
        return 0
    full_text = e.get("full_text")
    if full_text and cursor.lastrowid:
        conn.execute(
            "INSERT OR IGNORE INTO event_content (event_id, full_text) VALUES (?, ?)",
            (cursor.lastrowid, full_text),
        )
    return 1


class EventStore:
    """Thread-safe SQLite event persistence."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass  # fallback to default journal mode (DELETE) on exotic filesystems
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
        self._ensure_sessions_table()
        self._ensure_event_content_table()
        self._ensure_session_links_table()
        self._apply_migrations()

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
        """Create the file_registry table if it doesn't exist.

        A fresh DB gets the (fingerprint, file_path) key directly; an existing
        one keeps its shape until migration 4 rebuilds it.
        """
        self._conn.execute(_REGISTRY_DDL.format(name="file_registry"))
        self._conn.commit()

    def _ensure_sessions_table(self) -> None:
        """Create the sessions table if it doesn't exist, backfill from events."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if exists:
            return
        self._conn.executescript("""
            CREATE TABLE sessions (
                id TEXT NOT NULL,
                provider TEXT NOT NULL,
                project TEXT NOT NULL,
                title TEXT DEFAULT '',
                cwd TEXT DEFAULT '',
                git_branch TEXT DEFAULT '',
                model TEXT DEFAULT '',
                cli_version TEXT DEFAULT '',
                source TEXT DEFAULT '',
                cost REAL DEFAULT 0.0,
                is_sidechain INTEGER DEFAULT 0,
                first_event_at TEXT,
                last_event_at TEXT,
                event_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                initial_prompt TEXT DEFAULT '',
                metadata_json TEXT,
                created_at REAL NOT NULL,
                ended_at TEXT,
                ended_reason TEXT,
                PRIMARY KEY (id, provider)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
            CREATE INDEX IF NOT EXISTS idx_sessions_is_active ON sessions(is_active);
            CREATE INDEX IF NOT EXISTS idx_sessions_git_branch ON sessions(git_branch);
        """)
        # Backfill from existing events
        import time
        now = time.time()
        self._conn.execute(f"""
            INSERT OR IGNORE INTO sessions
                (id, provider, project, cwd, model,
                 first_event_at, last_event_at, event_count,
                 is_active, created_at)
            SELECT
                session_id, provider, project,
                MAX(cwd), MAX(model),
                MIN(timestamp), MAX(timestamp), COUNT(*),
                1, {now}
            FROM events
            WHERE session_id IS NOT NULL
            GROUP BY session_id, provider
        """)
        self._conn.commit()

    def _ensure_event_content_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS event_content (
                event_id INTEGER PRIMARY KEY REFERENCES events(id),
                full_text TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def _ensure_session_links_table(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_session TEXT NOT NULL,
                source_provider TEXT NOT NULL,
                target_session TEXT NOT NULL,
                target_provider TEXT NOT NULL,
                link_type TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                metadata_json TEXT,
                created_at REAL NOT NULL,
                UNIQUE(source_session, target_session, link_type)
            );
            CREATE INDEX IF NOT EXISTS idx_links_source ON session_links(source_session);
            CREATE INDEX IF NOT EXISTS idx_links_target ON session_links(target_session);
        """)
        self._conn.commit()

    def _apply_migrations(self) -> None:
        """Apply additive, versioned schema migrations exactly once.

        Mirrors ``git_store._apply_migrations``: a ``schema_migrations`` control
        table records which migrations have run, so each runs once and never on
        every startup. Migrations are additive only (see AGENTS.md §4).
        """
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
        """)
        self._conn.commit()

        applied = {
            r[0] for r in self._conn.execute("SELECT version FROM schema_migrations")
        }
        for version, name, fn in _EVENT_STORE_MIGRATIONS:
            if version in applied:
                continue
            fn(self._conn)
            import time
            self._conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (version, name, time.time()),
            )
            self._conn.commit()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run one write unit on the shared connection, leaving it clean (#65).

        Acquires the store lock, discards any transaction a previous failure
        left open, commits on success and rolls back on ANY exception — even
        KeyboardInterrupt — so ``conn.in_transaction`` is always False
        afterwards and the next writer never hits "cannot start a transaction
        within a transaction".
        """
        with self._lock:
            conn = self._get_conn()
            if conn.in_transaction:
                self._rollback_quiet(conn)
            try:
                yield conn
                conn.commit()
            except BaseException:
                self._rollback_quiet(conn)
                raise

    @staticmethod
    def _rollback_quiet(conn: sqlite3.Connection) -> None:
        """Roll back ignoring an already-broken connection."""
        try:
            conn.rollback()
        except sqlite3.Error:
            pass

    def upsert_session(self, meta: dict[str, Any], timestamp: str) -> None:
        """Insert or update session metadata.

        Uses INSERT ON CONFLICT DO UPDATE to merge new info without losing
        previously-stored fields (e.g. git_branch from an earlier event).
        Scalar columns are defensively coerced (#65): a provider sending a
        dict where a string used to be must not become a binding error.
        """
        import time
        now = time.time()
        sid = _scalar(meta.get("id", "")) or ""
        if not sid:
            return
        # Never persist an empty string as a timestamp: an empty first entry
        # (common on Claude summary/meta lines) must not freeze first_event_at
        # at "" forever — store NULL so a later valid timestamp can fill it.
        ts = _scalar(timestamp) or None
        metadata = meta.get("metadata")
        metadata_json = None
        if metadata:
            try:
                metadata_json = json.dumps(metadata, default=str)
            except (TypeError, ValueError):
                metadata_json = None
        try:
            cost = float(meta.get("cost", 0.0))
        except (TypeError, ValueError):
            cost = 0.0
        with self._write() as conn:
            conn.execute("""
                INSERT INTO sessions
                    (id, provider, project, title, cwd, git_branch, model,
                     cli_version, source, cost, is_sidechain,
                     first_event_at, last_event_at, event_count,
                     is_active, initial_prompt, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                ON CONFLICT(id, provider) DO UPDATE SET
                    project = COALESCE(NULLIF(excluded.project, ''), sessions.project),
                    title = COALESCE(NULLIF(excluded.title, ''), sessions.title),
                    cwd = COALESCE(NULLIF(excluded.cwd, ''), sessions.cwd),
                    git_branch = COALESCE(NULLIF(excluded.git_branch, ''), sessions.git_branch),
                    model = COALESCE(NULLIF(excluded.model, ''), sessions.model),
                    cli_version = COALESCE(NULLIF(excluded.cli_version, ''), sessions.cli_version),
                    source = COALESCE(NULLIF(excluded.source, ''), sessions.source),
                    cost = CASE WHEN excluded.cost > sessions.cost THEN excluded.cost ELSE sessions.cost END,
                    is_sidechain = excluded.is_sidechain,
                    first_event_at = CASE
                        WHEN NULLIF(sessions.first_event_at, '') IS NULL
                            THEN NULLIF(excluded.first_event_at, '')
                        WHEN NULLIF(excluded.first_event_at, '') IS NULL
                            THEN sessions.first_event_at
                        ELSE MIN(sessions.first_event_at, excluded.first_event_at)
                    END,
                    last_event_at = COALESCE(NULLIF(excluded.last_event_at, ''), sessions.last_event_at),
                    event_count = (SELECT COUNT(*) FROM events
                                   WHERE session_id = excluded.id AND provider = excluded.provider),
                    is_active = CASE WHEN sessions.ended_at IS NOT NULL THEN 0 ELSE 1 END,
                    initial_prompt = COALESCE(NULLIF(excluded.initial_prompt, ''), sessions.initial_prompt),
                    metadata_json = COALESCE(excluded.metadata_json, sessions.metadata_json)
            """, (
                sid,
                _scalar(meta.get("provider", "")) or "",
                _scalar(meta.get("project", "")) or "",
                _scalar(meta.get("title", "")) or "",
                _scalar(meta.get("cwd", "")) or "",
                _scalar(meta.get("git_branch", "")) or "",
                _scalar(meta.get("model", "")) or "",
                _scalar(meta.get("cli_version", "")) or "",
                _scalar(meta.get("source", "")) or "",
                cost,
                1 if meta.get("is_sidechain") else 0,
                ts,
                ts,
                _scalar(meta.get("initial_prompt", "")) or "",
                metadata_json,
                now,
            ))

    def refresh_session_stats(self, provider: str, session_ids: set[str] | list[str]) -> None:
        """Recompute event_count and first/last event time from ``events``.

        ``upsert_session`` runs before a batch's events are stored and dates the
        session with that batch's first entry, so a file ingested in ONE pass
        (history backfill / catch-up, #45) would keep ``event_count`` at the
        pre-insert count and ``last_event_at == first_event_at``. Widens the
        stored range with the events' range; never narrows it (session_meta
        lines can date a session without producing an event).
        """
        ids = [s for s in session_ids if s]
        if not ids or not provider:
            return
        with self._write() as conn:
            for sid in ids:
                conn.execute("""
                    UPDATE sessions SET
                        event_count = (SELECT COUNT(*) FROM events
                                       WHERE session_id = :sid AND provider = :p),
                        -- '~' sorts after any ISO timestamp: a sentinel for
                        -- "missing" so MIN() keeps whichever side exists.
                        first_event_at = NULLIF(MIN(
                            COALESCE(NULLIF(first_event_at, ''), '~'),
                            COALESCE((SELECT MIN(NULLIF(timestamp, '')) FROM events
                                      WHERE session_id = :sid AND provider = :p), '~')
                        ), '~'),
                        last_event_at = NULLIF(MAX(
                            COALESCE(last_event_at, ''),
                            COALESCE((SELECT MAX(timestamp) FROM events
                                      WHERE session_id = :sid AND provider = :p), '')
                        ), '')
                    WHERE id = :sid AND provider = :p
                """, {"sid": sid, "p": provider})

    def mark_session_ended(
        self, session_id: str, provider: str, ended_at: str, reason: str
    ) -> None:
        """Record an observed terminal signal for a session (Bug A, issue #16).

        Sets ``is_active = 0`` only from a signal observed in the session file
        (e.g. a Claude ``/exit`` local-command) — never inferred from recency
        (prohibited by #22↔#16). The first terminal signal wins: the guard on
        ``ended_at IS NULL`` makes repeat polls idempotent no-ops, and the
        ``ended_at`` column makes the ended state sticky against later upserts.
        """
        if not session_id or not provider:
            return
        with self._write() as conn:
            conn.execute("""
                UPDATE sessions
                SET is_active = 0, ended_at = ?, ended_reason = ?
                WHERE id = ? AND provider = ? AND ended_at IS NULL
            """, (_scalar(ended_at) or None, _scalar(reason) or "", session_id, provider))

    def get_sessions(
        self,
        hours: int = 24,
        provider: str | None = None,
        branch: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query sessions with optional filters."""
        where_parts = ["1=1"]
        params: list[Any] = []
        if hours:
            where_parts.append("s.last_event_at >= datetime('now', '-' || ? || ' hours')")
            params.append(hours)
        if provider:
            where_parts.append("s.provider = ?")
            params.append(provider)
        if branch:
            where_parts.append("s.git_branch = ?")
            params.append(branch)
        where = " AND ".join(where_parts)
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(f"""
                    SELECT s.id, s.provider, s.project, s.title, s.cwd,
                           s.git_branch, s.model, s.cli_version, s.source,
                           s.cost, s.is_sidechain, s.first_event_at, s.last_event_at,
                           (SELECT COUNT(*) FROM events e
                            WHERE e.session_id = s.id AND e.provider = s.provider) AS event_count,
                           s.is_active, s.initial_prompt, s.metadata_json
                    FROM sessions s
                    WHERE {where}
                    ORDER BY s.last_event_at DESC
                """, params).fetchall()
            except sqlite3.OperationalError:
                return []
        results = []
        for r in rows:
            d: dict[str, Any] = {
                "id": r[0], "provider": r[1], "project": r[2],
                "title": r[3] or "", "cwd": r[4] or "",
                "git_branch": r[5] or "", "model": r[6] or "",
                "cli_version": r[7] or "", "source": r[8] or "",
                "cost": r[9] or 0.0, "is_sidechain": bool(r[10]),
                "first_event_at": r[11] or "", "last_event_at": r[12] or "",
                "event_count": r[13] or 0, "is_active": bool(r[14]),
            }
            if r[15]:
                d["initial_prompt"] = r[15]
            if r[16]:
                try:
                    d["metadata"] = json.loads(r[16])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def get_session_detail(self, session_id: str) -> dict[str, Any] | None:
        """Get detailed info for a single session."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("""
                    SELECT s.id, s.provider, s.project, s.title, s.cwd,
                           s.git_branch, s.model, s.cli_version, s.source,
                           s.cost, s.is_sidechain, s.first_event_at, s.last_event_at,
                           (SELECT COUNT(*) FROM events e
                            WHERE e.session_id = s.id AND e.provider = s.provider) AS event_count,
                           s.is_active, s.initial_prompt, s.metadata_json,
                           s.ended_at,
                           s.ended_reason
                    FROM sessions s WHERE s.id = ?
                """, (session_id,)).fetchone()
                if row:
                    from hub.cache.workspace_store import read_session_activity
                    activity = read_session_activity(conn, session_id).get(
                        (session_id, row[1] or ""))
                else:
                    activity = None
            except sqlite3.OperationalError:
                return None
        if not row:
            return None
        d: dict[str, Any] = {
            "id": row[0], "provider": row[1], "project": row[2],
            "title": row[3] or "", "cwd": row[4] or "",
            "git_branch": row[5] or "", "model": row[6] or "",
            "cli_version": row[7] or "", "source": row[8] or "",
            "cost": row[9] or 0.0, "is_sidechain": bool(row[10]),
            "first_event_at": row[11] or "", "last_event_at": row[12] or "",
            "event_count": row[13] or 0, "is_active": bool(row[14]),
            "ended_at": row[17] or "", "ended_reason": row[18] or "",
        }
        if row[15]:
            d["initial_prompt"] = row[15]
        if row[16]:
            try:
                d["metadata"] = json.loads(row[16])
            except (json.JSONDecodeError, TypeError):
                pass
        # Ingest-based last activity (MAX(events.created_at)) for live rows;
        # monotonic and reliable, unlike last_event_at which carries the
        # original (possibly days-old) message timestamp in resumed sessions.
        # Imported history (backfill / re-parse) is dated by its event time
        # instead (#53, read_session_activity). Always populated for any
        # session that has at least one event.
        d["last_activity_at"] = (
            activity.strftime("%Y-%m-%dT%H:%M:%SZ") if activity else ""
        )
        return d

    def get_session_events(
        self, session_id: str, include_full_text: bool = False, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._get_conn()
            if include_full_text:
                rows = conn.execute("""
                    SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                           e.summary, e.session_id, e.tokens_json, e.tool_name,
                           e.file_path, e.model, e.cwd, ec.full_text, e.created_at
                    FROM events e
                    LEFT JOIN event_content ec ON e.id = ec.event_id
                    WHERE e.session_id = ?
                    ORDER BY e.timestamp ASC
                    LIMIT ?
                """, (session_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                           e.summary, e.session_id, e.tokens_json, e.tool_name,
                           e.file_path, e.model, e.cwd, NULL as full_text, e.created_at
                    FROM events e
                    WHERE e.session_id = ?
                    ORDER BY e.timestamp ASC
                    LIMIT ?
                """, (session_id, limit)).fetchall()
        results = []
        for r in rows:
            d: dict[str, Any] = {
                "id": r[0], "provider": r[1], "project": r[2],
                "event_type": r[3], "timestamp": r[4], "summary": r[5],
                "session_id": r[6], "tool_name": r[8],
                "file_path": r[9], "model": r[10], "cwd": r[11],
                # Ingest epoch (events.created_at, REAL NOT NULL): reliable and
                # monotonic, exposed alongside the original message timestamp so
                # age-based logic can distinguish resumed sessions.
                "created_at": r[13],
            }
            if r[7]:
                try:
                    d["tokens"] = json.loads(r[7])
                except (json.JSONDecodeError, TypeError):
                    pass
            if r[12]:
                d["full_text"] = r[12]
            results.append(d)
        return results

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared connection. Callers MUST hold self._lock."""
        return self._conn

    def store(self, event_dict: dict[str, Any]) -> None:
        """Store a single event."""
        import time
        event_dict = _normalize_event(event_dict)
        tokens_json = _tokens_json(event_dict.get("tokens"))
        fingerprint = _compute_fingerprint(event_dict)
        with self._write() as conn:
            cursor = conn.execute(
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
            full_text = event_dict.get("full_text")
            if cursor.rowcount > 0 and full_text and cursor.lastrowid:
                conn.execute(
                    "INSERT OR IGNORE INTO event_content (event_id, full_text) VALUES (?, ?)",
                    (cursor.lastrowid, full_text),
                )

    def store_batch(self, events: list[dict[str, Any]]) -> None:
        """Store multiple events in a single transaction.

        Uses INSERT OR IGNORE with fingerprint-based deduplication,
        so running backfill --full multiple times is safe.
        """
        if not events:
            return
        import time
        now = time.time()
        with self._write() as conn:
            for raw in events:
                e = _normalize_event(raw)
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO events
                       (provider, project, event_type, timestamp, summary,
                        session_id, tokens_json, tool_name, file_path, model, cwd,
                        fingerprint, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        e.get("provider", ""),
                        e.get("project", ""),
                        e.get("event_type", ""),
                        e.get("timestamp", ""),
                        e.get("summary", ""),
                        e.get("session_id"),
                        _tokens_json(e.get("tokens")),
                        e.get("tool_name"),
                        e.get("file_path"),
                        e.get("model"),
                        e.get("cwd"),
                        _compute_fingerprint(e),
                        now,
                    ),
                )
                full_text = e.get("full_text")
                if cursor.rowcount > 0 and full_text and cursor.lastrowid:
                    conn.execute(
                        "INSERT OR IGNORE INTO event_content (event_id, full_text) VALUES (?, ?)",
                        (cursor.lastrowid, full_text),
                    )

    def load_recent(self, limit: int = 500) -> list[dict[str, Any]]:
        """Load the most recent N live events, including their SQLite IDs.

        Historical rows (backfill / catch-up / re-parse, #45) carry new ids but
        old timestamps; they are excluded so they never pose as recent.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id, provider, project, event_type, timestamp, summary,
                          session_id, tokens_json, tool_name, file_path, model, cwd
                   FROM events WHERE historical = 0 ORDER BY id DESC LIMIT ?""",
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
        Historical rows (#45) are skipped: the id sequence stays monotonic for
        the client, it just jumps over ids that were never live.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id, provider, project, event_type, timestamp, summary,
                          session_id, tokens_json, tool_name, file_path, model, cwd
                   FROM events WHERE id > ? AND historical = 0
                   ORDER BY id ASC LIMIT ?""",
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

    def get_offset(self, fingerprint: str, file_path: str | Path) -> int | None:
        """Last known offset of a file, keyed by (fingerprint, path) — #50.

        See ``resolve_registry_offset``: exact row → its offset; a row whose
        file is gone → a rename, adopted onto ``file_path`` (keeping its
        offset and ``legacy`` flag); a row whose file still exists → a
        different file with the same first KB → None (read from 0).
        """
        if not fingerprint:
            return None
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT file_path, last_offset, updated_at FROM file_registry"
                " WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchall()
            offset, adopt_from = resolve_registry_offset(rows, file_path)
        if adopt_from is not None:
            import time
            with self._write() as conn:
                conn.execute(
                    """UPDATE file_registry SET file_path = ?, updated_at = ?
                       WHERE fingerprint = ? AND file_path = ?""",
                    (registry_path(file_path), time.time(), fingerprint, adopt_from),
                )
        return offset

    def is_legacy_offset(self, fingerprint: str, file_path: str | Path) -> bool | None:
        """``legacy`` flag of the exact (fingerprint, path) row; None if no row."""
        with self._lock:
            row = self._get_conn().execute(
                "SELECT legacy FROM file_registry WHERE fingerprint = ? AND file_path = ?",
                (fingerprint, registry_path(file_path)),
            ).fetchone()
        return None if row is None else bool(row[0])

    def clear_legacy_offset(self, fingerprint: str, file_path: str | Path) -> None:
        """Mark a row as re-read from 0 under the (fingerprint, path) key (#50)."""
        with self._write() as conn:
            conn.execute(
                "UPDATE file_registry SET legacy = 0 WHERE fingerprint = ? AND file_path = ?",
                (fingerprint, registry_path(file_path)),
            )

    def latest_offset_for_path(self, provider: str, file_path: str | Path) -> int | None:
        """Most recently written offset of ``file_path`` under ANY key.

        Seeds a provider's stable key (``BaseHarvester.STABLE_KEY``) from the
        content-fingerprint rows written before it existed, so the first
        restart after the upgrade resumes instead of re-reading from 0.
        """
        with self._lock:
            row = self._get_conn().execute(
                """SELECT last_offset FROM file_registry
                   WHERE provider = ? AND file_path = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (provider, registry_path(file_path)),
            ).fetchone()
        return row[0] if row else None

    def save_offset(self, fingerprint: str, provider: str, file_path: str, offset: int) -> None:
        """Save or update the byte offset for a file."""
        import time
        with self._write() as conn:
            conn.execute(
                """INSERT INTO file_registry (fingerprint, provider, file_path, last_offset, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint, file_path) DO UPDATE SET
                       last_offset = excluded.last_offset,
                       updated_at = excluded.updated_at""",
                (fingerprint, provider, registry_path(file_path), offset, time.time()),
            )

    def get_watcher_cycle(self, provider: str) -> float | None:
        """Wall-clock time of the provider watcher's last completed cycle (#45)."""
        with self._lock:
            row = self._get_conn().execute(
                "SELECT last_cycle_at FROM watcher_state WHERE provider = ?", (provider,)
            ).fetchone()
        return row[0] if row else None

    def set_watcher_cycle(self, provider: str, at: float) -> None:
        """Persist the provider watcher's last completed cycle (#45)."""
        import time
        with self._write() as conn:
            conn.execute(
                """INSERT INTO watcher_state (provider, last_cycle_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(provider) DO UPDATE SET
                       last_cycle_at = excluded.last_cycle_at,
                       updated_at = excluded.updated_at""",
                (provider, at, time.time()),
            )

    def store_with_offset(
        self,
        events: list[dict],
        fingerprint: str,
        provider: str,
        file_path: str,
        new_offset: int,
        historical: bool = False,
    ) -> list[dict]:
        """Store events and update file offset in a single atomic transaction.

        ``historical=True`` marks rows ingested by a non-live path (backfill /
        catch-up, #45) so the "recent" readers skip them.

        Returns the events with their assigned SQLite IDs (for SSE broadcast).
        Duplicates (INSERT OR IGNORE that don't insert) are NOT returned.
        If the process crashes mid-write, both the events AND the offset
        roll back, so the next read resumes from the correct position.
        """
        if not events and not fingerprint:
            return []
        import time
        now = time.time()
        normalized = [_normalize_event(e) for e in events]
        rows = []
        for e in normalized:
            rows.append((
                e.get("provider", ""),
                e.get("project", ""),
                e.get("event_type", ""),
                e.get("timestamp", ""),
                e.get("summary", ""),
                e.get("session_id"),
                _tokens_json(e.get("tokens")),
                e.get("tool_name"),
                e.get("file_path"),
                e.get("model"),
                e.get("cwd"),
                _compute_fingerprint(e),
                now,
                1 if historical else 0,
            ))

        result_events = []
        with self._lock:
            conn = self._get_conn()
            self._begin_immediate(conn)
            try:
                for i, row in enumerate(rows):
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO events
                           (provider, project, event_type, timestamp, summary,
                            session_id, tokens_json, tool_name, file_path, model, cwd,
                            fingerprint, created_at, historical)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        row,
                    )
                    if cursor.rowcount > 0:
                        ev = dict(normalized[i])
                        ev["id"] = cursor.lastrowid
                        full_text = ev.pop("full_text", None)
                        if full_text and cursor.lastrowid:
                            conn.execute(
                                "INSERT OR IGNORE INTO event_content (event_id, full_text) VALUES (?, ?)",
                                (cursor.lastrowid, full_text),
                            )
                        result_events.append(ev)

                if fingerprint:
                    # ``legacy`` is left alone: only a from-0 re-read clears it.
                    conn.execute(
                        """INSERT INTO file_registry (fingerprint, provider, file_path, last_offset, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(fingerprint, file_path) DO UPDATE SET
                               last_offset = excluded.last_offset,
                               updated_at = excluded.updated_at""",
                        (fingerprint, provider, registry_path(file_path), new_offset, now),
                    )
                conn.commit()
            except BaseException:
                self._rollback_quiet(conn)
                raise
        return result_events

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        """Start an explicit write transaction, clearing any leaked one first.

        A previous failure could have left the shared connection inside a
        transaction; ``BEGIN IMMEDIATE`` would then raise "cannot start a
        transaction within a transaction" and the watcher would die with it
        (#65). Rolling back first makes the next write always recoverable.
        """
        if conn.in_transaction:
            EventStore._rollback_quiet(conn)
        conn.execute("BEGIN IMMEDIATE")

    def replace_session_events(
        self,
        provider: str,
        session_ids: list[str],
        events: list[dict],
        file_offsets: list[tuple[str, str, int]],
    ) -> int:
        """Atomically swap a group of sessions' events for a fresh parse (#45).

        In ONE transaction: delete the sessions' events and their dependent
        ``event_content`` rows, reset the offsets of their files, insert the
        re-parsed events (``INSERT OR IGNORE``, first fingerprint wins) and
        store the new offsets ``(fingerprint, file_path, offset)``. Any failure
        — including KeyboardInterrupt — rolls the whole group back. Returns the
        number of events inserted. ``sessions`` metadata is untouched here (the
        caller refreshes its stats after commit).
        """
        import time
        now = time.time()
        marks = ",".join("?" * len(session_ids))
        inserted = 0
        with self._lock:
            conn = self._get_conn()
            self._begin_immediate(conn)
            try:
                conn.execute(
                    f"""DELETE FROM event_content WHERE event_id IN (
                            SELECT id FROM events
                            WHERE provider = ? AND session_id IN ({marks}))""",
                    [provider, *session_ids],
                )
                conn.execute(
                    f"DELETE FROM events WHERE provider = ? AND session_id IN ({marks})",
                    [provider, *session_ids],
                )
                for fp, path, _off in file_offsets:
                    conn.execute(
                        "DELETE FROM file_registry WHERE fingerprint = ? AND file_path = ?",
                        (fp, registry_path(path)),
                    )
                for e in events:
                    # Re-parsed history, not live activity (#45).
                    inserted += _insert_event_row(conn, e, now, historical=True)
                for fp, path, off in file_offsets:
                    conn.execute(
                        """INSERT INTO file_registry
                               (fingerprint, provider, file_path, last_offset, updated_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (fp, provider, registry_path(path), off, now),
                    )
                conn.commit()
            except BaseException:
                self._rollback_quiet(conn)
                raise
        return inserted

    def link_sessions(
        self,
        source_session: str,
        source_provider: str,
        target_session: str,
        target_provider: str,
        link_type: str = "references",
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Create a link between two sessions. Returns True if created, False if duplicate.

        Any failure rolls the shared connection back (``_write``) and returns
        False with a logged warning — never a leaked transaction (#65).
        """
        import time
        try:
            with self._write() as conn:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO session_links
                        (source_session, source_provider, target_session, target_provider,
                         link_type, confidence, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    source_session, source_provider,
                    target_session, target_provider,
                    link_type, confidence,
                    json.dumps(metadata) if metadata else None,
                    time.time(),
                ))
                return cursor.rowcount > 0
        except Exception:
            _log.warning(
                "link_sessions failed (%s -> %s)", source_session, target_session,
                exc_info=True,
            )
            return False

    def get_session_chain(self, session_id: str) -> list[dict[str, Any]]:
        """Get all sessions linked to the given session (predecessors and successors)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT
                        sl.source_session, sl.source_provider,
                        sl.target_session, sl.target_provider,
                        sl.link_type, sl.confidence, sl.metadata_json, sl.created_at,
                        CASE WHEN sl.source_session = ? THEN 'successor' ELSE 'predecessor' END AS direction,
                        s.title, s.model, s.project, s.first_event_at, s.last_event_at, s.event_count
                    FROM session_links sl
                    LEFT JOIN sessions s ON (
                        CASE WHEN sl.source_session = ?
                            THEN s.id = sl.target_session AND s.provider = sl.target_provider
                            ELSE s.id = sl.source_session AND s.provider = sl.source_provider
                        END
                    )
                    WHERE sl.source_session = ? OR sl.target_session = ?
                    ORDER BY sl.created_at ASC
                """, (session_id, session_id, session_id, session_id)).fetchall()
            except sqlite3.OperationalError:
                return []
        results = []
        for r in rows:
            linked_id = r[2] if r[0] == session_id else r[0]
            linked_provider = r[3] if r[0] == session_id else r[1]
            d: dict[str, Any] = {
                "session_id": linked_id,
                "provider": linked_provider,
                "direction": r[8],
                "link_type": r[4],
                "confidence": r[5],
                "title": r[9] or "",
                "model": r[10] or "",
                "project": r[11] or "",
                "first_event_at": r[12] or "",
                "last_event_at": r[13] or "",
                "event_count": r[14] or 0,
            }
            if r[6]:
                try:
                    d["metadata"] = json.loads(r[6])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def detect_temporal_links(
        self, session_id: str, hours: float = 4.0, min_shared_files: int = 1
    ) -> list[dict[str, Any]]:
        """Find sessions related by temporal and file proximity.

        Returns candidate links (not yet stored) with confidence scores.
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT
                        e2.session_id,
                        e2.provider,
                        s2.title,
                        s2.model,
                        s2.project,
                        COUNT(DISTINCT e2.file_path) AS shared_files,
                        MIN(e2.timestamp) AS first_event,
                        MAX(e2.timestamp) AS last_event,
                        (SELECT COUNT(*) FROM events WHERE session_id = e2.session_id) AS event_count
                    FROM events e1
                    JOIN events e2 ON e1.project = e2.project
                        AND e1.file_path IS NOT NULL
                        AND e2.file_path IS NOT NULL
                        AND e1.file_path = e2.file_path
                        AND e1.session_id != e2.session_id
                        AND abs(julianday(e1.timestamp) - julianday(e2.timestamp)) < ?
                    LEFT JOIN sessions s2 ON e2.session_id = s2.id AND e2.provider = s2.provider
                    WHERE e1.session_id = ?
                    GROUP BY e2.session_id, e2.provider
                    HAVING shared_files >= ?
                    ORDER BY shared_files DESC, first_event ASC
                """, (hours / 24.0, session_id, min_shared_files)).fetchall()
            except sqlite3.OperationalError:
                return []
        results = []
        for r in rows:
            confidence = min(0.5 + (r[5] * 0.1), 0.9)
            results.append({
                "session_id": r[0],
                "provider": r[1],
                "title": r[2] or "",
                "model": r[3] or "",
                "project": r[4] or "",
                "shared_files": r[5],
                "first_event_at": r[6] or "",
                "last_event_at": r[7] or "",
                "event_count": r[8] or 0,
                "confidence": confidence,
                "link_type": "temporal",
            })
        return results

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
