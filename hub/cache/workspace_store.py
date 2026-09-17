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

import os
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

-- Machine-wide portfolio rollup (issue #22 — Workspace axis, Phase C).
-- A materialized projection over the workspace tree, keyed (workspace, day),
-- SIGNAL-AGNOSTIC: a node lights up whether the activity came from a session
-- (path_attributions), the filesystem (path_touches), or git (github.db
-- commits). One row folds all three per day so the MCP/dashboard read stays a
-- single-table, single-DB query (git lives in a separate DB, resolved in only
-- at build time). Additive via IF NOT EXISTS — lands on existing v1.11.0 DBs.
--
-- Keyed INSERT OR REPLACE, NEVER wipe-and-rebuild (the daily_digests molde):
-- path_touches holds one row per path, latest state only (its UNIQUE(path)
-- upsert re-dates last_seen on every re-touch), so a day-keyed aggregate is not
-- reproducible from the base table — a file touched again tomorrow vanishes
-- from today's count. This rollup is the ONLY durable per-day fs record; a
-- DELETE + reinsert would erase history it alone holds.
CREATE TABLE IF NOT EXISTS workspace_rollup (
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    day TEXT NOT NULL,                     -- YYYY-MM-DD (substr of the ISO ts)
    session_touches INTEGER NOT NULL DEFAULT 0,   -- path_attributions edges
    fs_touches INTEGER NOT NULL DEFAULT 0,        -- path_touches (filesystem)
    git_touches INTEGER NOT NULL DEFAULT 0,       -- github.db commits
    last_activity TEXT,                    -- max ISO ts seen that day
    built_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, day)
);
CREATE INDEX IF NOT EXISTS idx_rollup_day ON workspace_rollup(day);
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


def _lit_sources(session_n: int, fs_n: int, git_n: int) -> list[str]:
    """Which signals lit a node — honest per-signal presence, not a summed total.

    Session edges, filesystem touches and git commits are incommensurable units;
    the portfolio answers *whether a node lit up and from which signal*, so we
    report the set of lit sources rather than fold them into one meaningless int.
    """
    lit = []
    if session_n:
        lit.append("session")
    if fs_n:
        lit.append("filesystem")
    if git_n:
        lit.append("git")
    return lit


def _portfolio_row(r: Any) -> dict[str, Any]:
    """Shape one aggregated portfolio row (shared by the store and MCP reads)."""
    session_n, fs_n, git_n = r[5] or 0, r[6] or 0, r[7] or 0
    return {
        "workspace_key": r[0], "kind": r[1], "remote_url": r[2],
        "root_path": r[3], "dir_path": r[4],
        "session_touches": session_n, "fs_touches": fs_n, "git_touches": git_n,
        "sources": _lit_sources(session_n, fs_n, git_n),
        "active_days": r[8], "last_activity": r[9],
    }


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

    # --- Portfolio rollup (issue #22 — Phase C, signal-agnostic projection) ---

    def build_rollup(self, github_db_path: str | Path | None = None) -> dict[str, int]:
        """Materialize the machine-wide portfolio rollup, signal-agnostic.

        Folds three independent signals per ``(workspace, day)`` — sessions
        (``path_attributions``), the filesystem (``path_touches``), and git
        (``github.db`` commits) — into ``workspace_rollup``. The two workspace.db
        tables have *different shapes* and different timestamp columns; git lives
        in a separate DB entirely and is resolved in here (read-only) so the read
        surface stays a single-table query.

        Day grouping avoids the ISO/epoch trap: ``path_touches.last_seen`` and
        ``path_attributions.first_seen`` are ISO-8601 TEXT (``substr(col,1,10)``
        yields the date and lexicographically orders correctly), while
        ``path_touches.mtime`` is a REAL epoch — never mixed. We group on the ISO
        columns, NOT ``datetime(col,'unixepoch')`` (which on an ISO string
        returns empty). ``last_seen`` (not ``first_seen``) is used for fs so a
        re-touched file re-dates to its latest activity, not its discovery day.

        Keyed ``INSERT OR REPLACE`` per ``(workspace_id, day)`` — NEVER a
        wipe-and-rebuild: ``path_touches`` keeps only the latest row per path, so
        this rollup is the only durable per-day fs record and a DELETE would
        erase history it alone holds (see the ``workspace_rollup`` schema note).

        Returns build stats, including ``multi_source_nodes`` (workspaces lit by
        ≥2 distinct signals — the signal-agnostic UNION is only meaningful if
        this is non-zero) and how many git repos matched an existing workspace
        vs. minted a new one.
        """
        github_db_path = github_db_path or (self.db_path.parent / "github.db")
        now = _now()

        # (workspace_id, day) -> {"session": n, "fs": n, "git": n, "last": iso}
        agg: dict[tuple[int, str], dict[str, Any]] = {}

        def _bump(wid: int, day: str, source: str, n: int, last: str | None) -> None:
            if not day:
                return
            cell = agg.setdefault(
                (wid, day),
                {"session": 0, "fs": 0, "git": 0, "last": ""},
            )
            cell[source] += n
            if last and last > cell["last"]:
                cell["last"] = last

        git_repos_matched = 0
        git_repos_new = 0

        with self._lock:
            conn = self._conn

            # 1. Session signal — one edge per (session, provider, file).
            for wid, day, n, last in conn.execute(
                """SELECT workspace_id, substr(first_seen, 1, 10) AS day,
                          COUNT(*) AS n, MAX(first_seen) AS last
                   FROM path_attributions GROUP BY workspace_id, day"""
            ).fetchall():
                _bump(wid, day, "session", n, last)

            # 2. Filesystem signal — group on the ISO last_seen (not mtime epoch).
            for wid, day, n, last in conn.execute(
                """SELECT workspace_id, substr(last_seen, 1, 10) AS day,
                          COUNT(*) AS n, MAX(last_seen) AS last
                   FROM path_touches GROUP BY workspace_id, day"""
            ).fetchall():
                _bump(wid, day, "fs", n, last)

            # 3. Git signal — resolve each repo root to a workspace via the SAME
            #    ladder (resolve_dir walks up to the repo's .git, so a repo root
            #    and a deep session/fs path on that repo collide on one key →
            #    one node, three signals). github.db is opened read-only; absent
            #    or unreadable → git simply contributes nothing.
            for repo_path, day, n, last in self._read_git_commits(github_db_path):
                ident = resolve_dir(repo_path)
                existed = conn.execute(
                    "SELECT 1 FROM workspaces WHERE workspace_key = ?", (ident.key,)
                ).fetchone()
                wid = self._upsert_workspace_locked(ident, now)
                if day is None:  # repo-seen marker only (no commits) — count match
                    if existed:
                        git_repos_matched += 1
                    else:
                        git_repos_new += 1
                    continue
                _bump(wid, day, "git", n, last)

            for (wid, day), cell in agg.items():
                conn.execute(
                    """INSERT OR REPLACE INTO workspace_rollup
                           (workspace_id, day, session_touches, fs_touches,
                            git_touches, last_activity, built_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (wid, day, cell["session"], cell["fs"], cell["git"],
                     cell["last"] or None, now),
                )
            conn.commit()

            multi_source_nodes = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT workspace_id FROM workspace_rollup
                       GROUP BY workspace_id
                       HAVING (SUM(session_touches) > 0)
                            + (SUM(fs_touches) > 0)
                            + (SUM(git_touches) > 0) >= 2
                   )"""
            ).fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM workspace_rollup").fetchone()[0]
            nodes = conn.execute(
                "SELECT COUNT(DISTINCT workspace_id) FROM workspace_rollup"
            ).fetchone()[0]

        return {
            "rows": rows,
            "workspaces": nodes,
            "multi_source_nodes": multi_source_nodes,
            "git_repos_matched": git_repos_matched,
            "git_repos_new": git_repos_new,
        }

    @staticmethod
    def _read_git_commits(
        github_db_path: str | Path,
    ) -> list[tuple[str, str | None, int, str | None]]:
        """Read git commit day-counts per repo from ``github.db`` (read-only).

        Yields ``(repo_path, day, count, last_ts)`` rows plus, for every repo, a
        sentinel ``(repo_path, None, 0, None)`` so the caller can tell repos it
        matched to a workspace apart from ones it minted (build stats). Returns
        ``[]`` if github.db is absent or has no git tables — git is optional.
        """
        if not os.path.exists(str(github_db_path)):
            return []
        out: list[tuple[str, str | None, int, str | None]] = []
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return []
        try:
            repos = src.execute("SELECT id, path FROM repos").fetchall()
            for repo_id, repo_path in repos:
                if not repo_path:
                    continue
                out.append((repo_path, None, 0, None))  # repo-seen sentinel
                for day, n, last in src.execute(
                    """SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS n,
                              MAX(timestamp) AS last
                       FROM git_commits WHERE repo_id = ? GROUP BY day""",
                    (repo_id,),
                ).fetchall():
                    out.append((repo_path, day, n, last))
        except sqlite3.OperationalError:
            return []
        finally:
            src.close()
        return out

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

    def get_portfolio(self, since: str | None = None) -> list[dict[str, Any]]:
        """The machine-wide portfolio: hot workspaces from the rollup.

        One row per workspace, aggregating every day it lit up, from any signal.
        ``since`` is compared as a plain string against the ISO ``day`` (NOT via
        ``date()`` — that would reintroduce the ISO/epoch hazard at read time).
        """
        where = ""
        params: list[Any] = []
        if since:
            where = "WHERE r.day >= ?"
            params.append(since[:10])
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT w.workspace_key, w.kind, w.remote_url, w.root_path,
                           w.dir_path,
                           SUM(r.session_touches) AS session_touches,
                           SUM(r.fs_touches) AS fs_touches,
                           SUM(r.git_touches) AS git_touches,
                           COUNT(*) AS active_days,
                           MAX(r.last_activity) AS last_activity
                    FROM workspace_rollup r
                    JOIN workspaces w ON w.id = r.workspace_id
                    {where}
                    GROUP BY w.id
                    ORDER BY last_activity DESC""",
                params,
            ).fetchall()
        return [_portfolio_row(r) for r in rows]

    def get_workspace_activity(
        self, workspace_key: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-day activity for one workspace from the rollup, newest day first."""
        where = "WHERE w.workspace_key = ?"
        params: list[Any] = [workspace_key]
        if since:
            where += " AND r.day >= ?"
            params.append(since[:10])
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT r.day, r.session_touches, r.fs_touches, r.git_touches,
                           r.last_activity
                    FROM workspace_rollup r
                    JOIN workspaces w ON w.id = r.workspace_id
                    {where}
                    ORDER BY r.day DESC""",
                params,
            ).fetchall()
        return [
            {
                "day": r[0], "session_touches": r[1], "fs_touches": r[2],
                "git_touches": r[3], "sources": _lit_sources(r[1], r[2], r[3]),
                "last_activity": r[4],
            }
            for r in rows
        ]
