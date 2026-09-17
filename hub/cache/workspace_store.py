"""SQLite store for path→workspace attribution (MoolMesh #20 — Workspace axis).

Lives in its own database, ``~/.moolmesh/workspace.db``, kept separate from
``events.db`` (sessions) and ``github.db`` (git/GitHub) per invariant §2.4. The
backfill pass reads ``events.db`` strictly read-only and never writes to it.

Two tables:

  ``workspaces``        — stable workspace identities (one row per project),
                          keyed by ``workspace_key`` from the resolver ladder.
  ``path_attributions`` — the M:N edge: a session touched a file that belongs to
                          a workspace. One session → many files → many
                          workspaces; one workspace → many sessions.

Mirrors ``git_store.py``: shared thread-safe connection, WAL, and versioned
run-once migrations via ``schema_migrations`` (additive only, AGENTS.md §4).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hub.log import get as get_logger
from hub.correlation.workspace_resolver import (
    WorkspaceIdentity,
    resolve_dir,
    _containing_dir,
)

_log = get_logger("WorkspaceStore")


_SCHEMA = """
-- Stable workspace identities (the "project" a path belongs to).
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL UNIQUE,   -- git_remote:host/owner/repo | git_root:<path> | path_hash:<hash>
    kind TEXT NOT NULL,                    -- git_remote | git_root | path_hash
    remote_url TEXT,                       -- canonical host/owner/repo (git_remote)
    root_path TEXT,                        -- git root / worktree path (git_remote, git_root)
    dir_path TEXT,                         -- containing directory (path_hash)
    first_seen TEXT NOT NULL
);

-- M:N: session ↔ workspace, one edge per (session, provider, file_path).
CREATE TABLE IF NOT EXISTS path_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resolved_via TEXT NOT NULL,            -- the ladder rung used at resolution time
    first_seen TEXT NOT NULL,
    UNIQUE(session_id, provider, file_path)
);
CREATE INDEX IF NOT EXISTS idx_attr_workspace ON path_attributions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_attr_session ON path_attributions(session_id, provider);

-- Filesystem path-touches (issue #21 — Workspace axis, Phase B).
-- A file under a marked root changed on disk, attributed to its owning
-- workspace via the same resolver. Has NO session — kept separate from
-- path_attributions (whose session_id is NOT NULL) so neither read query
-- is polluted by the other's rows. Additive: created via IF NOT EXISTS on
-- every init, so it lands on existing v1.10.0 databases without a migration.
CREATE TABLE IF NOT EXISTS path_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,             -- one row per path; conflict target
    mtime REAL NOT NULL,                    -- last observed st_mtime
    source TEXT NOT NULL,                   -- "filesystem"
    resolved_via TEXT NOT NULL,             -- ladder rung used at resolution time
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_touch_workspace ON path_touches(workspace_id);

-- Per-root mtime cursor: the high-water mark advanced each scan so already
-- observed files are not re-emitted (incremental). See WorkspaceStore.set_cursor.
CREATE TABLE IF NOT EXISTS fs_cursors (
    root_path TEXT PRIMARY KEY,
    last_mtime REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Versioned, additive migrations — each runs exactly once (mirrors git_store).
_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], int]]] = []


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, name, fn in _MIGRATIONS:
        if version not in applied:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            _log.info("Migration %d applied: %s", version, name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceStore:
    """Thread-safe SQLite persistence for workspace attribution."""

    DEFAULT_DB_PATH = Path.home() / ".moolmesh" / "workspace.db"

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or self.DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()

        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        try:
            _apply_migrations(self._conn)
        except Exception:
            _log.error(
                "Error applying migrations — DB still works but may be stale",
                exc_info=True,
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Write primitives (caller holds the lock) ---

    def _upsert_workspace_locked(self, ident: WorkspaceIdentity, now: str) -> int:
        """Insert or refresh a workspace; return its id. ``first_seen`` is pinned.

        ON CONFLICT DO UPDATE keeps the attribution self-healing: if a checkout
        later gains a remote, a re-run promotes the row's evidence while the id
        and first_seen stay stable.
        """
        conn = self._conn
        conn.execute(
            """INSERT INTO workspaces
                   (workspace_key, kind, remote_url, root_path, dir_path, first_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace_key) DO UPDATE SET
                   kind = excluded.kind,
                   remote_url = excluded.remote_url,
                   root_path = excluded.root_path,
                   dir_path = excluded.dir_path""",
            (ident.key, ident.kind, ident.remote_url, ident.root_path, ident.dir_path, now),
        )
        row = conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key = ?", (ident.key,)
        ).fetchone()
        return row[0]

    def _record_attribution_locked(
        self,
        session_id: str,
        provider: str,
        file_path: str,
        workspace_id: int,
        resolved_via: str,
        now: str,
    ) -> None:
        """Record one session↔workspace edge. Idempotent and self-healing."""
        self._conn.execute(
            """INSERT INTO path_attributions
                   (session_id, provider, file_path, workspace_id, resolved_via, first_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, provider, file_path) DO UPDATE SET
                   workspace_id = excluded.workspace_id,
                   resolved_via = excluded.resolved_via""",
            (session_id, provider, file_path, workspace_id, resolved_via, now),
        )

    # --- Public write API ---

    def upsert_workspace(self, ident: WorkspaceIdentity) -> int:
        with self._lock:
            wid = self._upsert_workspace_locked(ident, _now())
            self._conn.commit()
            return wid

    def record_attribution(
        self,
        session_id: str,
        provider: str,
        file_path: str,
        ident: WorkspaceIdentity,
    ) -> int:
        """Resolve+persist one attribution. Returns the workspace id."""
        now = _now()
        with self._lock:
            wid = self._upsert_workspace_locked(ident, now)
            self._record_attribution_locked(
                session_id, provider or "", file_path, wid, ident.kind, now
            )
            self._conn.commit()
            return wid

    # --- Filesystem touches (issue #21 — Phase B watcher) ---

    def get_cursor(self, root_path: str) -> float:
        """Return the mtime high-water mark for a root (0.0 if never scanned).

        ``0.0`` makes the first cycle emit every file under the root — the
        "first cycle IS the backfill" convention from ``watchers/base.py``.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT last_mtime FROM fs_cursors WHERE root_path = ?", (root_path,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set_cursor(self, root_path: str, last_mtime: float) -> None:
        """Persist the mtime cursor for a root.

        The watcher advances this to ``scan_start - 1s`` (NOT ``max(mtime seen)``):
        a file written mid-scan can have an mtime below the newest file already
        visited, and a ``max``-based cursor would jump past it and drop the touch
        forever. Anchoring to the scan start re-emits a few paths on overlap
        (harmless — the touch upsert is idempotent) rather than losing any.
        """
        with self._lock:
            self._conn.execute(
                """INSERT INTO fs_cursors (root_path, last_mtime, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(root_path) DO UPDATE SET
                       last_mtime = excluded.last_mtime,
                       updated_at = excluded.updated_at""",
                (root_path, float(last_mtime), _now()),
            )
            self._conn.commit()

    def record_touch(
        self,
        path: str,
        mtime: float,
        ident: WorkspaceIdentity,
        source: str = "filesystem",
    ) -> int:
        """Resolve+persist one filesystem path-touch. Returns the workspace id.

        Idempotent and self-healing on ``path``: if a directory later gains a
        ``.git`` the resolver returns a different workspace and the row's
        ``workspace_id`` is updated in place (one path → one workspace).
        """
        now = _now()
        with self._lock:
            wid = self._upsert_workspace_locked(ident, now)
            self._conn.execute(
                """INSERT INTO path_touches
                       (workspace_id, path, mtime, source, resolved_via, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       workspace_id = excluded.workspace_id,
                       mtime = excluded.mtime,
                       resolved_via = excluded.resolved_via,
                       last_seen = excluded.last_seen""",
                (wid, path, float(mtime), source, ident.kind, now, now),
            )
            self._conn.commit()
            return wid

    def get_workspace_touches(self, workspace_key: str) -> list[dict[str, Any]]:
        """Filesystem touches attributed to a workspace, newest mtime first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.path, t.mtime, t.source, t.resolved_via, t.last_seen
                   FROM path_touches t
                   JOIN workspaces w ON w.id = t.workspace_id
                   WHERE w.workspace_key = ?
                   ORDER BY t.mtime DESC""",
                (workspace_key,),
            ).fetchall()
        return [
            {"path": r[0], "mtime": r[1], "source": r[2],
             "resolved_via": r[3], "last_seen": r[4]}
            for r in rows
        ]

    # --- Backfill (populate from already-persisted events, read-only) ---

    def backfill_from_events(self, events_db_path: str | Path) -> dict[str, int]:
        """Populate attributions from ``events.db`` — a read-only batch pass.

        Reads only absolute file paths (``file_path LIKE '/%'``): non-absolute
        values in ``events.file_path`` are overwhelmingly Bash command strings,
        not paths, so attributing them would invent bogus workspaces. The count
        of skipped non-absolute rows is reported (no silent truncation).

        ``events.db`` is opened ``mode=ro``; this method never writes to it.
        """
        uri = f"file:{events_db_path}?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = src.execute(
                """SELECT DISTINCT session_id, provider, file_path, cwd
                   FROM events
                   WHERE file_path LIKE '/%' AND session_id IS NOT NULL AND session_id != ''"""
            ).fetchall()
            skipped = src.execute(
                """SELECT COUNT(*) FROM (
                       SELECT DISTINCT session_id, provider, file_path FROM events
                       WHERE file_path IS NOT NULL AND file_path != ''
                         AND file_path NOT LIKE '/%'
                         AND session_id IS NOT NULL AND session_id != ''
                   )"""
            ).fetchone()[0]
        finally:
            src.close()

        now = _now()
        dir_cache: dict[str, WorkspaceIdentity] = {}
        attributed = 0
        with self._lock:
            for session_id, provider, file_path, cwd in rows:
                container = _containing_dir(file_path, cwd)
                ident = dir_cache.get(container)
                if ident is None:
                    ident = resolve_dir(container)
                    dir_cache[container] = ident
                wid = self._upsert_workspace_locked(ident, now)
                self._record_attribution_locked(
                    session_id, provider or "", file_path, wid, ident.kind, now
                )
                attributed += 1
            self._conn.commit()
            workspace_count = self._conn.execute(
                "SELECT COUNT(*) FROM workspaces"
            ).fetchone()[0]

        return {
            "attributed": attributed,
            "workspaces": workspace_count,
            "directories": len(dir_cache),
            "skipped_non_absolute": skipped,
        }

    # --- Read surface ---

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                          w.first_seen, COUNT(a.id) AS attributions,
                          COUNT(DISTINCT a.session_id || '/' || a.provider) AS sessions,
                          (SELECT COUNT(*) FROM path_touches t
                           WHERE t.workspace_id = w.id) AS touches
                   FROM workspaces w
                   LEFT JOIN path_attributions a ON a.workspace_id = w.id
                   GROUP BY w.id
                   ORDER BY sessions DESC, attributions DESC, touches DESC"""
            ).fetchall()
        return [
            {
                "workspace_key": r[0], "kind": r[1], "remote_url": r[2],
                "root_path": r[3], "dir_path": r[4], "first_seen": r[5],
                "attributions": r[6], "sessions": r[7], "touches": r[8],
            }
            for r in rows
        ]

    def get_session_workspaces(
        self, session_id: str, provider: str | None = None
    ) -> list[dict[str, Any]]:
        """Which workspaces a session touched, with per-workspace file counts."""
        where = "a.session_id = ?"
        params: list[Any] = [session_id]
        if provider:
            where += " AND a.provider = ?"
            params.append(provider)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                           a.provider, COUNT(DISTINCT a.file_path) AS files
                    FROM path_attributions a
                    JOIN workspaces w ON w.id = a.workspace_id
                    WHERE {where}
                    GROUP BY w.id, a.provider
                    ORDER BY files DESC""",
                params,
            ).fetchall()
        return [
            {
                "workspace_key": r[0], "kind": r[1], "remote_url": r[2],
                "root_path": r[3], "dir_path": r[4], "provider": r[5], "files": r[6],
            }
            for r in rows
        ]

    def get_workspace_sessions(self, workspace_key: str) -> list[dict[str, Any]]:
        """Which sessions touched a workspace, with per-session file counts."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT a.session_id, a.provider, COUNT(DISTINCT a.file_path) AS files
                   FROM path_attributions a
                   JOIN workspaces w ON w.id = a.workspace_id
                   WHERE w.workspace_key = ?
                   GROUP BY a.session_id, a.provider
                   ORDER BY files DESC""",
                (workspace_key,),
            ).fetchall()
        return [
            {"session_id": r[0], "provider": r[1], "files": r[2]}
            for r in rows
        ]
