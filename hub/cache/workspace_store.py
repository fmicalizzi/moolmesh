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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hub.localtime import CONFIGURED, local_day, resolve_timezone, user_timezone, zone_key
from hub.log import get as get_logger
from hub.models.base import SHELL_TOOLS
from hub.correlation.workspace_resolver import (
    SQL_PORTABLE_ABS,
    WorkspaceIdentity,
    is_portable_abs,
    path_module,
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
-- ``via`` (issue #40): 'file' = a file the session touched; 'cwd' = fallback
-- for a session with NO file edges at all — one edge per distinct absolute cwd,
-- file_path = the cwd itself. A session's cwd edges are deleted the moment it
-- gains a file edge. Pre-#40 DBs get the column via migration 1.
-- ``event_ts`` (issue #42): the time of the EARLIEST underlying event of the
-- edge, UTC ISO in one fixed format (``_event_ts``) so string order is
-- chronological. ``first_seen`` is the attribution pass's clock (a backfill
-- artifact); the rollup dates session activity on ``event_ts`` and falls back
-- to ``first_seen`` only where no event clock was readable (NULL).
-- ``activity_ts`` (issue #56): THE EDGE'S OWN clock — the time of the LATEST
-- underlying event of the edge, per the #53 rule (live rows: ``created_at``;
-- historical rows: parsed event time), fixed-format UTC ISO so the NULL-safe
-- MAX upsert only ever moves it later. State and production read it instead of
-- applying the session's max clock to every project the session touched.
CREATE TABLE IF NOT EXISTS path_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resolved_via TEXT NOT NULL,            -- the ladder rung used at resolution time
    first_seen TEXT NOT NULL,
    via TEXT NOT NULL DEFAULT 'file',      -- 'file' | 'cwd' (#40)
    event_ts TEXT,                         -- earliest event time, UTC ISO (#42)
    activity_ts TEXT,                      -- latest event clock, UTC ISO (#56)
    UNIQUE(session_id, provider, file_path)
);
CREATE INDEX IF NOT EXISTS idx_attr_workspace ON path_attributions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_attr_session ON path_attributions(session_id, provider);

-- Local days each edge worked (issue #114). ``activity_ts`` is ONE clock per
-- edge (its latest event), so a session that worked a project on several days
-- would count only on its last one. One row per (session, provider, workspace,
-- edge kind, LOCAL day) with at least one event of that edge — keyed by the
-- workspace, not the file, so a session editing 50 files of one repo on one day
-- is one row. ``via`` mirrors ``path_attributions.via`` so the rows of a
-- session's cwd-fallback edges leave with them (#40). Days are local days in
-- the zone recorded under ``rollup_meta.active_days_zone``; a changed
-- ``[user] timezone`` re-derives the table (after a backup). Readers count a
-- row only while an edge with the same (session, provider, workspace, via)
-- still exists, so a re-resolved or purged edge drops its days without a
-- rewrite. ``INSERT OR IGNORE`` makes every pass idempotent.
CREATE TABLE IF NOT EXISTS edge_active_days (
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    via TEXT NOT NULL,                     -- 'file' | 'cwd' (as path_attributions)
    day TEXT NOT NULL,                     -- YYYY-MM-DD, LOCAL day (#81, #114)
    PRIMARY KEY (session_id, provider, workspace_id, via, day)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_active_days_workspace ON edge_active_days(workspace_id, day);

-- Small key/value markers (rollup zone #81, re-derivations #101/#114). Also
-- created by migrations 4–6; here so a pass never meets a DB without it.
CREATE TABLE IF NOT EXISTS rollup_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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

-- Session-attribution cursor (issue #39 — daemon-scheduled attribution).
-- High-water mark over events.db ``events.id`` (INTEGER AUTOINCREMENT,
-- monotonic; the events_new migration preserves ids): the incremental pass
-- attributes only ``id > last_event_id``. Advanced ONLY after the attribution
-- commit succeeded, so a failed pass re-reads its range next cycle. One row per
-- source (``'events'``). Additive via IF NOT EXISTS — lands on existing DBs.
CREATE TABLE IF NOT EXISTS attribution_cursors (
    source TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL,
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
--
-- Consequence — this is a DURABLE STORE, not a cache. build_rollup only ADDS and
-- OVERWRITES the (workspace, day) keys it recomputes; it NEVER retracts a key it
-- no longer produces. So if the mapping from a source timestamp to `day` ever
-- CHANGES (e.g. #23 moved git from local-day to UTC-day, or any future portfolio
-- reclassification/regrouping), the commits move to their new day and their OLD
-- day rows are left behind — stale, double-counting on SUM/active_days until
-- reconciled. Recovery is a SURGICAL reconcile of the shifted signal (zero it,
-- prune fully-empty rows, re-run the rollup), NOT a blind full rebuild — a blind
-- DELETE FROM would destroy the fs-per-day history that lives only here. Do not
-- build a retraction mechanism to "fix" this; the durability is the point.
--
-- The ONE exception is session_touches (#40): it is a pure projection of
-- path_attributions, whose edges can now disappear (a session's cwd-fallback
-- edges are deleted when it gains file edges, and re-resolution can move an
-- edge to another workspace). build_rollup therefore recomputes that column
-- for EVERY row — zeroing it where no edge remains — and prunes rows left
-- all-zero. fs_touches / git_touches are never retracted.
CREATE TABLE IF NOT EXISTS workspace_rollup (
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    day TEXT NOT NULL,                     -- YYYY-MM-DD, LOCAL day (#81; rollup_meta.day_zone)
    session_touches INTEGER NOT NULL DEFAULT 0,   -- path_attributions edges
    fs_touches INTEGER NOT NULL DEFAULT 0,        -- path_touches (filesystem)
    git_touches INTEGER NOT NULL DEFAULT 0,       -- github.db commits
    last_activity TEXT,                    -- max ISO ts seen that day
    built_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, day)
);
CREATE INDEX IF NOT EXISTS idx_rollup_day ON workspace_rollup(day);

-- delivery_candidate (issue #22 — Phase C, correlate). A LOCAL, structural
-- guess that a workspace's work was likely delivered — surfaced as a candidate
-- WITH CONFIDENCE, never as a bare "done" flag. Two disciplines are baked into
-- the shape:
--   * Quiescence alone is indistinguishable from a break, so it is only a
--     PRECONDITION: a row exists only when a workspace went quiet AND a second,
--     co-occurring signal closed the burst.
--   * The firing signal is RECORDED per row (``signal`` + ``signal_detail``) so
--     the confidence is auditable — one of exactly three admissible signals:
--     ``session_close`` (sessions.ended_at, #16, read-only), ``git_commit``
--     (a commit closing the burst), ``root_artifact`` (a new file at the
--     workspace root whose extension is outside the working set).
-- Keyed per (workspace, signal); a re-detect that finds the workspace hot again
-- evicts the row (never a stale candidate over a now-active workspace).
CREATE TABLE IF NOT EXISTS delivery_candidates (
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    signal TEXT NOT NULL,          -- session_close | git_commit | root_artifact
    signal_detail TEXT,            -- session_id | sha | artifact path (auditable)
    quiescent_since TEXT NOT NULL, -- real last activity (UTC ISO) before quiet
    confidence REAL NOT NULL,      -- [0, 0.9] heuristic — candidate, never fact
    detected_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, signal)
);

-- Portfolio classification (issue #24 — Stage 1). Maps each workspace into the
-- 4-category taxonomy (collapse harness A → project, nest subdirs/materials
-- B/C, de-prioritize config D1, orphan degenerate/home-config D2) so the
-- portfolio read can render real projects instead of 376 flat folders.
--
-- Unlike workspace_rollup (a DURABLE store that must never be blindly rebuilt),
-- this table is DERIVED and fully REBUILDABLE: it is a projection over
-- workspaces + events.db (session cwds) + the filesystem, holding no history of
-- its own. classify_workspaces therefore DELETEs and re-inserts every row on
-- each pass — a workspace that stops being harness (or whose project moves)
-- MUST lose its stale row, which a keyed upsert could not guarantee. Additive
-- via IF NOT EXISTS; lands on existing v1.12.x DBs without a migration.
--
-- project_key is a JOIN HANDLE (the canonical project a workspace belongs to);
-- project_label is a display NAME (masked under hide_project_names, like the
-- workspace name fields). role drives rendering: 'project' (a real root),
-- 'collapse' (harness folded INTO project_key at read time — never re-keyed in
-- the rollup), 'nest' (shown nested under project_key), 'orphan' (unclassified).
CREATE TABLE IF NOT EXISTS workspace_classification (
    workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    category TEXT NOT NULL,        -- root | A | B | C | D
    subtype TEXT NOT NULL,         -- project | harness | subdir | materials | config | home_config
                                   -- | degenerate | temporary | app_bundle | container (#64)
                                   -- | reference (#36, decisión 4c)
    role TEXT NOT NULL,            -- project | collapse | nest | orphan
    project_key TEXT,              -- canonical project group (join handle; NULL for orphan)
    project_label TEXT,            -- project group display name (masked at read)
    resolved_via TEXT NOT NULL,    -- self | session_cwd | encode_match | fs_decode | subdir | materials | dotchild | home_dot | degenerate | tool | unresolved
    classified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classification_project ON workspace_classification(project_key);
CREATE INDEX IF NOT EXISTS idx_classification_role ON workspace_classification(role);
"""


def _mig_1_attribution_via(conn: sqlite3.Connection) -> int:
    """Add ``path_attributions.via`` (#40) to pre-existing DBs.

    Fresh DBs get the column from ``_SCHEMA``; the ``PRAGMA table_info`` guard
    makes this a no-op there. Existing rows are all file edges — the DEFAULT.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(path_attributions)")}
    if "via" in columns:
        return 0
    conn.execute(
        "ALTER TABLE path_attributions ADD COLUMN via TEXT NOT NULL DEFAULT 'file'"
    )
    return 1


def _mig_2_attribution_event_ts(conn: sqlite3.Connection) -> int:
    """Add ``path_attributions.event_ts`` (#42) and re-derive it for history.

    Fresh DBs get the column from ``_SCHEMA`` (the ``PRAGMA table_info`` guard
    skips the ALTER). Existing edges start NULL — the rollup falls back to
    ``first_seen`` for them — so this also resets the ``'events'`` attribution
    cursor to 0: the daemon's next incremental pass is then a full pass that
    fills ``event_ts`` for every edge (upserts are idempotent and the value
    only ever moves earlier). It runs exactly once, via ``schema_migrations``:
    a later open never resets the cursor again. ``mool workspace backfill``
    stays the manual reset. A fresh DB has no cursor row — the UPDATE is a no-op.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(path_attributions)")}
    added = 0
    if "event_ts" not in columns:
        conn.execute("ALTER TABLE path_attributions ADD COLUMN event_ts TEXT")
        added = 1
    conn.execute(
        "UPDATE attribution_cursors SET last_event_id = 0, updated_at = ?"
        " WHERE source = 'events'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    return added


def _mig_3_attribution_activity_ts(conn: sqlite3.Connection) -> int:
    """Add ``path_attributions.activity_ts`` (#56) and re-derive it for history.

    Fresh DBs get the column from ``_SCHEMA`` (the ``PRAGMA table_info`` guard
    skips the ALTER). Existing edges are seeded from ``event_ts`` — the closest
    edge clock already on file; the pass below then advances it to the real
    latest event. Like #42, this resets the ``'events'`` attribution cursor so
    the daemon's next incremental pass is a full pass that fills ``activity_ts``
    for every edge whose events are still readable. Runs exactly once via
    ``schema_migrations``: a later open never resets the cursor again. The
    reset also makes the next pass the full pass that purges shell-only edges
    (#58), since both ship together. A fresh DB has no cursor row — the UPDATE
    is a no-op.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(path_attributions)")}
    added = 0
    if "activity_ts" not in columns:
        conn.execute("ALTER TABLE path_attributions ADD COLUMN activity_ts TEXT")
        added = 1
    conn.execute(
        "UPDATE path_attributions SET activity_ts = event_ts"
        " WHERE activity_ts IS NULL AND event_ts IS NOT NULL"
    )
    conn.execute(
        "UPDATE attribution_cursors SET last_event_id = 0, updated_at = ?"
        " WHERE source = 'events'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    return added


def _mig_4_rollup_day_zone(conn: sqlite3.Connection) -> int:
    """Record which zone ``workspace_rollup.day`` is bucketed in (#81).

    Until v1.27.x every rollup day was a UTC day. From #81 the rollup buckets
    on the shared LOCAL day (``hub.localtime``), so ``build_rollup`` must know
    the zone the existing rows were built in to move them once (see
    ``_rollup_zone_transition_locked``). An existing rollup is marked ``UTC``;
    a fresh one gets no marker and ``build_rollup`` writes the current zone.
    Additive (a new table) and idempotent (``INSERT OR IGNORE``).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rollup_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    has_rows = conn.execute("SELECT 1 FROM workspace_rollup LIMIT 1").fetchone()
    if not has_rows:
        return 0
    conn.execute(
        "INSERT OR IGNORE INTO rollup_meta (key, value) VALUES (?, 'UTC')",
        (_ROLLUP_ZONE_KEY,),
    )
    return 1


# rollup_meta key holding the zone ``workspace_rollup.day`` is bucketed in.
_ROLLUP_ZONE_KEY = "day_zone"

# rollup_meta key set while the #101 edge-clock re-derivation is pending.
_ACTIVITY_REDERIVE_KEY = "activity_rederive"


def _mig_5_activity_rederive(conn: sqlite3.Connection) -> int:
    """Schedule a one-time re-derivation of ``activity_ts`` (#101).

    events.db migration 8 re-marks as historical the live rows a pre-#62
    outage catch-up stored, so their #53 clock becomes the event time instead
    of the catch-up day. ``activity_ts`` only ever advances
    (``_ACTIVITY_TS_UPSERT``), so a plain cursor reset could never move an edge
    back to its real day: this marks the re-derivation pending and
    ``attribute_incremental`` runs it — a full pass that REPLACES each seen
    edge's clock — once events.db has migration 8 recorded, after a backup of
    workspace.db. Nothing is rewritten here. A DB with no edges has nothing to
    re-derive (no marker). Idempotent (``INSERT OR IGNORE``).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rollup_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    if not conn.execute("SELECT 1 FROM path_attributions LIMIT 1").fetchone():
        return 0
    conn.execute(
        "INSERT OR IGNORE INTO rollup_meta (key, value) VALUES (?, 'pending')",
        (_ACTIVITY_REDERIVE_KEY,),
    )
    return 1


# rollup_meta keys for the per-day edge activity (#114): the zone
# ``edge_active_days.day`` is bucketed in, and the pending (re-)derivation.
_ACTIVE_DAYS_ZONE_KEY = "active_days_zone"
_ACTIVE_DAYS_REDERIVE_KEY = "active_days_rederive"
# Its value while a full pass is under way (#130): set, with the day table
# cleared, right after the backup; a retry that finds it resumes without a new
# backup. ``pending`` means "not started yet".
_REDERIVE_RUNNING = "running"


def _mig_6_edge_active_days(conn: sqlite3.Connection) -> int:
    """Create ``edge_active_days`` (#114) and schedule its first derivation.

    Additive: a new table (also in ``_SCHEMA``, so ``IF NOT EXISTS`` makes this
    a no-op on a fresh DB). Existing edges have no days yet, so a DB with edges
    gets the ``active_days_rederive`` marker: ``attribute_incremental`` then
    runs one full read-only pass over events.db that fills the table, after a
    backup of workspace.db (see ``_rederive_full``). Nothing existing is
    rewritten here. Idempotent (``INSERT OR IGNORE``).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rollup_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS edge_active_days (
               session_id TEXT NOT NULL,
               provider TEXT NOT NULL,
               workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
               via TEXT NOT NULL,
               day TEXT NOT NULL,
               PRIMARY KEY (session_id, provider, workspace_id, via, day)
           ) WITHOUT ROWID"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_days_workspace"
        " ON edge_active_days(workspace_id, day)"
    )
    if not conn.execute("SELECT 1 FROM path_attributions LIMIT 1").fetchone():
        return 0
    conn.execute(
        "INSERT OR IGNORE INTO rollup_meta (key, value) VALUES (?, 'pending')",
        (_ACTIVE_DAYS_REDERIVE_KEY,),
    )
    return 1


def _mig_7_live_catchup_day(conn: sqlite3.Connection) -> int:
    """Schedule re-dating the live rows of an outage catch-up (#131).

    ``_edge_activity_dt`` now clocks a live row by its own time when that lies
    within the live window before its ingest, so the evening before a
    just-past-midnight restart counts on its own day. Existing edges were
    bucketed on the ingest day: this marks BOTH one-time re-derivations
    pending — the edge clocks (#101 marker; it runs only once events.db has
    migration 8, as before) and the days worked (#114 marker) — and
    ``attribute_incremental`` runs them as one full pass after a backup of
    workspace.db (``_rederive_full``). Nothing is rewritten here. A DB with no
    edges has nothing to re-derive. Idempotent (``INSERT OR REPLACE`` of the
    same values).
    """
    if not conn.execute("SELECT 1 FROM path_attributions LIMIT 1").fetchone():
        return 0
    for key in (_ACTIVITY_REDERIVE_KEY, _ACTIVE_DAYS_REDERIVE_KEY):
        conn.execute(
            "INSERT OR REPLACE INTO rollup_meta (key, value) VALUES (?, 'pending')",
            (key,),
        )
    return 1


# Versioned, additive migrations — each runs exactly once (mirrors git_store).
_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], int]]] = [
    (1, "attribution_via", _mig_1_attribution_via),
    (2, "attribution_event_ts", _mig_2_attribution_event_ts),
    (3, "attribution_activity_ts", _mig_3_attribution_activity_ts),
    (4, "rollup_day_zone", _mig_4_rollup_day_zone),
    (5, "activity_rederive", _mig_5_activity_rederive),
    (6, "edge_active_days", _mig_6_edge_active_days),
    (7, "live_catchup_day", _mig_7_live_catchup_day),
]


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
    if all(version in applied for version, _n, _f in _MIGRATIONS):
        return
    from hub.log import migration_logging
    with migration_logging(conn):
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


def backup_workspace_db(conn: sqlite3.Connection, db_path: Path,
                        backup_dir: Path | None = None) -> Path:
    """Consistent copy of workspace.db via sqlite3's online backup API.

    Same pattern as ``hub.backfill.backup_events_db``: taken before any pass
    that rewrites existing rows (the rollup zone transition, #81). Copies from
    the store's own connection, so it sees exactly what the pass will rewrite.
    """
    import time as _time
    backup_dir = backup_dir or Path(db_path).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"workspace-{stamp}.db"
    n = 1
    while dest.exists():
        dest = backup_dir / f"workspace-{stamp}-{n}.db"
        n += 1
    dst = sqlite3.connect(str(dest))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    from hub.backups import prune_backups
    prune_backups(backup_dir, "workspace", protect=dest)  # keep the newest N (#52)
    return dest


def _parse_ts(s: str | None) -> datetime | None:
    """Parse a timestamp from ANY of the three real clocks into aware UTC.

    The three delivery-signal sources store time in three incompatible formats
    (verified on real DBs) and string comparison across them is wrong by hours:

      * ``path_touches.last_seen`` — ``2026-09-17T08:17:16.149924+00:00`` (UTC offset)
      * ``sessions.ended_at``      — ``2026-09-17T15:16:49.014Z`` (UTC, Z form)
      * ``git_commits.timestamp``  — ``2026-09-16T00:18:20`` (NAIVE local — git_store
        migration 3 deliberately stores commit time in local, not UTC)

    Lexicographically ``Z`` > ``+`` and naive-vs-UTC is a whole-timezone skew, so
    quiescence MUST be computed on parsed ``datetime`` objects, never on the raw
    strings. Empty/None/garbage → ``None`` so it drops out of a ``max`` instead of
    poisoning it (one real ended session has ``last_event_at = ''``).
    """
    if not s or not s.strip():
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → interpret as system local, then to UTC
    return dt.astimezone(timezone.utc)


def _event_ts(timestamp: str | None, created_at: float | None) -> str | None:
    """One event's clock as fixed-format UTC ISO (#42), or ``None``.

    ``events.timestamp`` mixes formats (Claude/Codex ``...Z``; OpenCode a local
    ``-06:00`` offset, where ``substr(ts,1,10)`` would give the LOCAL day), so
    it is parsed via :func:`_parse_ts`. Empty/garbage falls back to the ingest
    epoch ``created_at``. ``timespec='microseconds'`` keeps the width fixed
    (plain ``isoformat`` drops ``.ffffff`` at 0 µs), so string comparison — the
    upsert's MIN, the rollup's MAX — is chronological.
    """
    dt = _parse_ts(timestamp)
    if dt is None and created_at is not None:
        try:
            dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            dt = None
    return dt.isoformat(timespec="microseconds") if dt is not None else None


def _historical_event_dt(timestamp: str | None, created_at: float | None) -> datetime | None:
    """Activity time of one HISTORICAL event row (#53), as aware UTC, or ``None``.

    Backfill / catch-up / re-parse insert old events with ``created_at = now``,
    so for those rows the event's own ``timestamp`` is the only honest clock.
    Parsed via :func:`_parse_ts` (mixed ISO formats); a bare numeric epoch —
    seconds, or milliseconds (OpenCode) when > 1e11 — is accepted too. An
    unparseable/empty timestamp falls back to ``created_at`` (the ingest epoch):
    a documented, visible degradation rather than a silently dropped session.
    """
    dt = _parse_ts(timestamp)
    if dt is None and timestamp:
        try:
            n = float(timestamp.strip())
            dt = datetime.fromtimestamp(n / 1000.0 if n > 1e11 else n, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            dt = None
    if dt is None and created_at is not None:
        try:
            dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            dt = None
    return dt


def read_session_activity(
    conn: sqlite3.Connection,
    session_id: str | None = None,
) -> dict[tuple[str, str], datetime]:
    """THE per-session activity clock over an open events.db connection (#53).

    ``{(session_id, provider or ''): aware-UTC datetime}``, the max of:

      * ``MAX(created_at)`` over LIVE rows (``historical = 0``) — the ingest
        epoch, kept for live rows because a resumed session replays original
        timestamps spanning months (#18); the ingest clock does not;
      * the parsed event time (:func:`_historical_event_dt`) over HISTORICAL
        rows (``historical = 1``: backfill / catch-up / re-parse, #45), whose
        ``created_at`` is the import time, not when the work happened.

    Timestamps are parsed in Python (formats are mixed), never ``MAX()``-ed as
    strings in SQL. A pre-#45 events.db without the ``historical`` column keeps
    the old all-rows ``MAX(created_at)`` behavior. ``session_id`` narrows the
    scan to one session (indexed). Shared by derived state, the production view
    and session detail — one definition of "when did this session happen".
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    has_hist = "historical" in cols
    where = "session_id IS NOT NULL AND session_id != ''"
    params: tuple[Any, ...] = ()
    if session_id is not None:
        where += " AND session_id = ?"
        params = (session_id,)
    out: dict[tuple[str, str], datetime] = {}

    def _bump(key: tuple[str, str], dt: datetime | None) -> None:
        if dt is not None and (key not in out or dt > out[key]):
            out[key] = dt

    live_where = where + (" AND historical = 0" if has_hist else "")
    for sid, prov, mx in conn.execute(
        f"SELECT session_id, provider, MAX(created_at) FROM events "
        f"WHERE {live_where} GROUP BY session_id, provider",
        params,
    ):
        if mx is None:
            continue
        try:
            _bump((sid, prov or ""), datetime.fromtimestamp(float(mx), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            continue
    if has_hist:
        for sid, prov, ts, created_at in conn.execute(
            f"SELECT session_id, provider, timestamp, created_at FROM events "
            f"WHERE {where} AND historical = 1",
            params,
        ):
            _bump((sid, prov or ""), _historical_event_dt(ts, created_at))
    return out


def _min_ts(a: str | None, b: str | None) -> str | None:
    """NULL-safe minimum of two ``_event_ts`` strings."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_ts(a: str | None, b: str | None) -> str | None:
    """NULL-safe maximum of two fixed-format UTC ISO strings."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# The live window of events.db (``event_store.LIVE_WINDOW_HOURS``, #62): an
# event ingested live within it of its own time is a catch-up, not a replay.
_LIVE_WINDOW = timedelta(hours=12)


def _edge_activity_dt(
    timestamp: str | None, created_at: float | None, historical: bool
) -> datetime | None:
    """One event's #53 activity clock: the honest time of THAT event.

    Live rows (``historical = 0``) are clocked by the ingest epoch
    ``created_at`` — a resumed session replays original timestamps spanning
    months (#18) — EXCEPT when the event's own parsed time lies inside the
    live window before that ingest (``[created_at - LIVE_WINDOW, created_at]``):
    then the event time is honest and wins (#131). That is the catch-up after
    an outage: the evening before a just-past-midnight restart is ingested
    live, and must count on its own local day, not on the restart day. A
    replay older than the window, or a timestamp in the future of its ingest,
    keeps the ingest clock. Historical rows (#45: backfill / catch-up /
    re-parse) stored ``created_at = now`` at import, so their parsed event
    time is the only honest clock (:func:`_historical_event_dt`, which also
    falls back to ``created_at``). A live row with no usable ``created_at``
    falls back to the parsed timestamp; an unreadable row yields ``None``.
    """
    if historical:
        return _historical_event_dt(timestamp, created_at)
    dt: datetime | None = None
    if created_at is not None:
        try:
            dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            dt = None
    if dt is None:
        return _parse_ts(timestamp)
    if timestamp:
        own = _historical_event_dt(str(timestamp), None)
        if own is not None and timedelta(0) <= dt - own <= _LIVE_WINDOW:
            return own
    return dt


# NULL-safe "keep the earliest" for the event_ts upsert (#42). SQLite's scalar
# MIN() is NULL if either side is NULL, so each side falls back to the other: a
# later incremental pass never moves an old edge later, and a NULL (unreadable
# clock) never clobbers a known one.
_EVENT_TS_UPSERT = (
    "event_ts = MIN(COALESCE(path_attributions.event_ts, excluded.event_ts),"
    " COALESCE(excluded.event_ts, path_attributions.event_ts))"
)

# NULL-safe "keep the latest" for the activity_ts upsert (#56). Same COALESCE
# discipline as event_ts, but with MAX: the edge's clock only ever advances, a
# later pass never moves it backwards, and a NULL never clobbers a known value.
_ACTIVITY_TS_UPSERT = (
    "activity_ts = MAX(COALESCE(path_attributions.activity_ts, excluded.activity_ts),"
    " COALESCE(excluded.activity_ts, path_attributions.activity_ts))"
)

# The #101 re-derivation: a full pass REPLACES the edge clock with the one it
# just folded from every event of that edge (a NULL still never clobbers).
_ACTIVITY_TS_REPLACE = (
    "activity_ts = COALESCE(excluded.activity_ts, path_attributions.activity_ts)"
)


def _ext(path: str) -> str:
    """Lowercased file extension (``''`` for none)."""
    return os.path.splitext(path)[1].lower()


# Per-signal base confidence for a delivery candidate. Heuristic and DELIBERATELY
# capped below certainty (see ``_delivery_confidence``): a local structural guess
# is a candidate, never a fact. session_close (a real terminal signal) is the
# strongest; a root artifact the weakest.
_DELIVERY_BASE: dict[str, float] = {
    "session_close": 0.7,
    "git_commit": 0.6,
    "root_artifact": 0.5,
}


def _delivery_confidence(signal: str, quiet_hours: float) -> float:
    """Confidence in ``[0, 0.9]`` — base(signal) plus a bounded quiescence bonus.

    Longer silence after the closing signal raises confidence slightly, but the
    result is capped at 0.9: this is a candidate surfaced with confidence, never
    a certainty. The value is heuristic and the firing signal is recorded
    alongside it so a human/agent can audit *why*, not just *how much*.
    """
    base = _DELIVERY_BASE.get(signal, 0.5)
    return round(min(0.9, base + min(0.2, quiet_hours * 0.02)), 3)


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


def _is_own_email(email: str | None, own_emails: frozenset[str] | None) -> bool:
    """True when a commit's author email belongs to the owner (decisión 1A).

    Case-insensitive; no emails configured → never own (nothing is marked).
    The email itself is never returned or surfaced — team stays latent.
    """
    if not own_emails or not email:
        return False
    return email.strip().lower() in own_emails


_KEY_TOKEN_PREFIXES = ("git_remote:", "git_root:", "path_hash:", "encoded:")


def _rewrite_project_aliases(
    classified: list[tuple[int, Any]],
    ws_rows: list[tuple],
    aliases: dict[str, str],
) -> tuple[list[tuple[int, Any]], list[str]]:
    """Rewrite classified project keys through ``[workspace] project_aliases``.

    Read-layer only (decisión 5, #64p2). An **origin** is matched by path (the
    workspace's own root/dir, or anything under it) or by key (workspace_key /
    project_key: ``git_remote:…``, ``git_root:…``, ``path_hash:…``). Every
    workspace whose project_key belongs to the origin's group is rewritten to
    the **destination** — a key form used verbatim, or a path resolved against
    the workspace rows. Chains are followed (bounded, cycle-safe). A missing
    destination or an origin matching nothing emits a warning and changes
    nothing — never a failure.

    Returns ``(classified, warnings)``.
    """
    from dataclasses import replace
    from hub.cache.portfolio_classifier import config_path, is_under

    def _is_key(t: str) -> bool:
        return str(t).startswith(_KEY_TOKEN_PREFIXES)

    # Normalize origins (path tokens) + keep raw dests for chain resolution.
    amap: dict[str, str] = {}
    for k, v in (aliases or {}).items():
        k, v = str(k).strip(), str(v).strip()
        if not k or not v:
            continue
        amap[k if _is_key(k) else config_path(k)] = v
    if not amap:
        return classified, []

    meta: dict[int, tuple[str, str | None, str | None]] = {}
    for wid, kind, _remote, root, dirp, wkey in ws_rows:
        d = (root if kind in ("git_remote", "git_root") else dirp) or ""
        meta[wid] = (wkey, d.rstrip("/"), kind)

    pkey_label: dict[str, str] = {}
    for _wid, c in classified:
        if c.project_key and c.project_label and c.project_key not in pkey_label:
            pkey_label[c.project_key] = c.project_label

    def _label_for_key(key: str) -> str:
        if key in pkey_label:
            return pkey_label[key]
        if key.startswith("git_remote:"):
            return key[len("git_remote:"):]
        if key.startswith("git_root:"):
            return os.path.basename(key[len("git_root:"):].rstrip("/")) or key
        for pref in ("path_hash:", "encoded:"):
            if key.startswith(pref):
                return os.path.basename(key[len(pref):].rstrip("/")) or key
        return key

    def _origin_match(wid: int, c: Any) -> str | None:
        wkey, d, _kind = meta[wid]
        for origin in amap:
            if _is_key(origin):
                if wkey == origin or c.project_key == origin:
                    return origin
            elif d and is_under(d, origin):
                return origin
        return None

    origin_map: dict[int, str] = {}
    pkey_origin: dict[str, str] = {}
    for wid, c in classified:
        o = _origin_match(wid, c)
        if o is not None:
            origin_map[wid] = o
            if c.project_key:
                pkey_origin.setdefault(c.project_key, o)
    # Rows that share an origin group's project_key (harness collapses) follow.
    for wid, c in classified:
        if wid in origin_map:
            continue
        if c.project_key and c.project_key in pkey_origin:
            origin_map[wid] = pkey_origin[c.project_key]

    def _canonical_for(origin: str) -> tuple[tuple[str, str] | None, str | None]:
        seen = {origin}
        token = amap[origin].strip()
        norm = token if _is_key(token) else config_path(token)
        while norm in amap:
            if norm in seen:
                return None, f"alias circular en '{origin}'"
            seen.add(norm)
            token = amap[norm].strip()
            norm = token if _is_key(token) else config_path(token)
        if _is_key(norm):
            return (norm, _label_for_key(norm)), None
        # Path destination: exact own-path match, else the shallowest project
        # row below it (a project folder whose workspaces are its children).
        exact: list[tuple[int, Any]] = []
        under: list[tuple[int, Any]] = []
        for wid, c in classified:
            wkey, d, _kind = meta[wid]
            if not d or not c.project_key:
                continue
            if config_path(d) == norm:
                exact.append((wid, c))
            elif is_under(d, norm):
                under.append((wid, c))
        pick = exact[0][1] if exact else None
        if pick is None and under:
            under.sort(key=lambda t: len(meta[t[0]][1]))
            pick = under[0][1]
        if pick is None:
            return None, f"destino inexistente: '{amap[origin]}' (origen '{origin}')"
        return (pick.project_key, pick.project_label or
                _label_for_key(pick.project_key)), None

    canon: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    for origin in amap:
        res, err = _canonical_for(origin)
        if res is None:
            warnings.append(err or f"alias sin destino: '{origin}'")
        else:
            canon[origin] = res
    used = set(origin_map.values())
    for origin in amap:
        if origin not in used:
            warnings.append(f"origen sin coincidencias: '{origin}'")

    out: list[tuple[int, Any]] = []
    for wid, c in classified:
        origin = origin_map.get(wid)
        if origin and origin in canon:
            key, label = canon[origin]
            if c.project_key != key or c.role == "orphan":
                # An aliased orphan (e.g. a container-internal ``/app``) must
                # become a project row so its history FOLDS into the canonical
                # group instead of staying in the unclassified drawer.
                role = "project" if c.role == "orphan" else c.role
                c = replace(c, project_key=key, project_label=label, role=role)
        out.append((wid, c))
    return out, warnings


def _worked_by_workspace(
    conn: sqlite3.Connection, since: str | None = None,
) -> dict[int, set[tuple[str, str, str]]] | None:
    """Days worked per workspace from ``edge_active_days`` (#123).

    Returns ``{workspace_id: {(session_id, provider, day), ...}}`` over the
    window (``day >= since``, plain string compare like the rollup), so a view
    can report DISTINCT sessions and DISTINCT days — never a sum of days —
    across any set of workspaces (a project fold, a client union). Same rule as
    the production strip (#114): only while the table's recorded zone is the
    configured zone being read, and only rows whose edge (session, provider,
    workspace, via) still exists in ``path_attributions``. ``None`` means "not
    usable" (pre-#114 workspace.db, no zone recorded yet, or another zone):
    the caller falls back to the rollup's ``active_days``.
    """
    try:
        zrow = conn.execute(
            "SELECT value FROM rollup_meta WHERE key = ?", (_ACTIVE_DAYS_ZONE_KEY,)
        ).fetchone()
        if zrow is None or zrow[0] != zone_key(user_timezone()):
            return None
        where = ""
        params: list[Any] = []
        if since:
            where = "AND d.day >= ?"
            params.append(since[:10])
        rows = conn.execute(
            f"""SELECT d.workspace_id, d.session_id, d.provider, d.day
                FROM edge_active_days d
                WHERE EXISTS (
                    SELECT 1 FROM path_attributions a
                    WHERE a.session_id = d.session_id
                      AND a.provider = d.provider
                      AND a.workspace_id = d.workspace_id
                      AND a.via = d.via)
                {where}""",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return None  # pre-#114 workspace.db: no table, no zone
    out: dict[int, set[tuple[str, str, str]]] = {}
    for wid, sid, prov, day in rows:
        out.setdefault(wid, set()).add((sid, prov or "", day))
    return out


def _worked_fields(
    worked: set[tuple[str, str, str]] | None, active_days: int,
) -> dict[str, Any]:
    """The additive #123 fields from a worked set (``None`` = fallback).

    ``sessions_worked`` = distinct sessions, ``days_worked`` = distinct local
    days, ``days_worked_basis`` says where the number came from:
    ``edge_active_days`` or ``rollup`` (fallback: ``days_worked`` is the
    rollup's ``active_days`` and ``sessions_worked`` is ``None`` — there is no
    honest session count to fall back to)."""
    if worked is None:
        return {"sessions_worked": None, "days_worked": active_days,
                "days_worked_basis": "rollup"}
    return {"sessions_worked": len({(s, p) for s, p, _ in worked}),
            "days_worked": len({d for _, _, d in worked}),
            "days_worked_basis": "edge_active_days"}


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



def _keyed_repo_project_key(repo_path: str) -> str | None:
    """Canonical project key of a clone-less repo (board #93, remote #94).

    Its ``repos.path`` is ``board:``/``remote:`` + ``github.com/owner/repo``
    — no directory to resolve — so its project is the GitHub remote identity
    itself, the same ``git_remote:`` key a local clone of it resolves to.
    ``None`` for a filesystem path.
    """
    for prefix in ("board:", "remote:"):
        if repo_path.startswith(prefix):
            return "git_remote:" + repo_path[len(prefix):].lower()
    return None


# --- Per-contributor outcome (#83, opt-in) ---

def _read_contributor_rows(src: sqlite3.Connection) -> list[tuple]:
    """Outcome rows with who to credit and PR lines, from ``github.db``.

    Only called when ``[team] per_contributor`` is on. ``github_pr_stats``
    (#80) is optional: on a DB without it the lines read as unknown (0).
    """
    cols = {r[1] for r in src.execute("PRAGMA table_info(github_issues)")}
    assignees = "i.assignees" if "assignees" in cols else "NULL"
    has_stats = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='github_pr_stats'"
    ).fetchone() is not None
    lines = ("s.additions, s.deletions" if has_stats else "NULL, NULL")
    join = ("LEFT JOIN github_pr_stats s"
            " ON s.repo_id = i.repo_id AND s.pr_number = i.number"
            if has_stats else "")
    return src.execute(
        f"""SELECT i.repo_id, i.is_pull_request, i.state, i.author, {assignees},
                   i.pr_merged_at, i.closed_at, {lines}
            FROM github_issues i {join}
            WHERE (i.is_pull_request = 1 AND i.pr_merged_at IS NOT NULL)
               OR (i.is_pull_request = 0 AND i.state IN ('open', 'closed'))"""
    ).fetchall()


def _fold_contributors(team: Any, rows: list[tuple],
                       window: tuple[str, str] | None,
                       tz: Any) -> dict[int, dict[str | None, dict[str, int]]]:
    """``{repo_id: {contributor | None: counts}}`` — ``None`` collects bots."""
    from hub.team import add_item, credited, empty_counts
    out: dict[int, dict[str | None, dict[str, int]]] = {}
    for rid, is_pr, state, author, assignees, merged_at, closed_at, add, dele in rows:
        is_pr = bool(is_pr)
        in_window = False
        if window is not None:
            day = local_day(merged_at if is_pr else closed_at, tz)
            in_window = day is not None and window[0] <= day <= window[1]
        name = credited(team, is_pr=is_pr, author=author, assignees=assignees)
        cell = out.setdefault(rid, {}).setdefault(name, empty_counts())
        add_item(cell, is_pr=is_pr, state=state, merged=merged_at is not None,
                 in_window=in_window, additions=add, deletions=dele)
    return out


def _merge_contributors(cell: dict[str, Any],
                        people: dict[str | None, dict[str, int]]) -> None:
    """Add one repo's per-contributor counts to its project's outcome cell."""
    from hub.team import empty_counts
    contributors = cell.setdefault("contributors", {})
    bots = cell.setdefault("contributors_bots", empty_counts())
    for name, counts in people.items():
        target = bots if name is None else contributors.setdefault(
            name, empty_counts())
        for k, v in counts.items():
            target[k] += v


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
        via: str = "file",
        event_ts: str | None = None,
        activity_ts: str | None = None,
        replace_activity: bool = False,
    ) -> None:
        """Record one session↔workspace edge. Idempotent and self-healing.

        ``event_ts`` only ever moves earlier (``_EVENT_TS_UPSERT``) and
        ``activity_ts`` only ever moves later (``_ACTIVITY_TS_UPSERT``) —
        except in the #101 re-derivation (``replace_activity``), where it is
        replaced by the clock a full pass folded (``_ACTIVITY_TS_REPLACE``).
        """
        activity_upsert = (_ACTIVITY_TS_REPLACE if replace_activity
                           else _ACTIVITY_TS_UPSERT)
        self._conn.execute(
            f"""INSERT INTO path_attributions
                   (session_id, provider, file_path, workspace_id, resolved_via,
                    first_seen, via, event_ts, activity_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, provider, file_path) DO UPDATE SET
                   workspace_id = excluded.workspace_id,
                   resolved_via = excluded.resolved_via,
                   via = excluded.via,
                   {_EVENT_TS_UPSERT},
                   {activity_upsert}""",
            (session_id, provider, file_path, workspace_id, resolved_via, now, via,
             event_ts, activity_ts),
        )

    def _record_active_days_locked(
        self, session_id: str, provider: str, workspace_id: int, via: str,
        days: set[str] | None,
    ) -> None:
        """Add the local days an edge worked (#114). Idempotent."""
        if days:
            self._conn.executemany(
                "INSERT OR IGNORE INTO edge_active_days"
                " (session_id, provider, workspace_id, via, day)"
                " VALUES (?, ?, ?, ?, ?)",
                [(session_id, provider, workspace_id, via, d) for d in days],
            )

    def _active_days_zone(self) -> tuple[Any, str]:
        """Zone ``edge_active_days`` is bucketed in: the recorded one, else the
        configured ``[user] timezone`` (#114). Returns ``(tzinfo, zone key)``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rollup_meta WHERE key = ?",
                (_ACTIVE_DAYS_ZONE_KEY,),
            ).fetchone()
        if row is not None:
            return resolve_timezone(row[0]), row[0]
        tz = user_timezone()
        return tz, zone_key(tz)

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

    # --- Session-attribution cursor (issue #39) ---

    _EVENTS_CURSOR = "events"

    def get_attribution_cursor(self) -> int:
        """Last ``events.id`` attributed (0 if never run — first pass = full)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_event_id FROM attribution_cursors WHERE source = ?",
                (self._EVENTS_CURSOR,),
            ).fetchone()
        return int(row[0]) if row else 0

    def set_attribution_cursor(self, last_event_id: int) -> None:
        """Persist the events.id high-water mark. Call ONLY after the
        attribution commit for every row ``<= last_event_id`` succeeded."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO attribution_cursors (source, last_event_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET
                       last_event_id = excluded.last_event_id,
                       updated_at = excluded.updated_at""",
                (self._EVENTS_CURSOR, int(last_event_id), _now()),
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

        Day grouping (#81): every signal is bucketed on the shared LOCAL day of
        ``hub.localtime`` (``[user] timezone``), the same day digests and the
        production strip use — parsed in Python, never ``substr`` of mixed
        clocks. ``path_touches.last_seen`` and ``path_attributions.event_ts``/
        ``first_seen`` (sessions are dated on ``COALESCE(event_ts, first_seen)``
        — the event, not the pass, #42) are UTC ISO-8601 TEXT, while
        ``path_touches.mtime`` is a REAL epoch — never mixed. ``last_seen`` (not
        ``first_seen``) is used for fs so a re-touched file re-dates to its
        latest activity, not its discovery day. ``last_activity`` stays UTC ISO.

        The zone the rows were built in is kept in ``rollup_meta``; when it
        differs from the current one (the v1.28.0 upgrade from UTC days, or a
        changed ``[user] timezone``) the shifted signals are moved once — see
        ``_rollup_zone_transition_locked`` — after a backup of workspace.db
        (``~/.moolmesh/backups/workspace-<date>.db``). If the backup fails the
        pass builds on the old zone and the move is retried next pass.

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

        # Resolve git repo roots BEFORE taking the lock: resolve_dir reads .git
        # on disk, which can stall on cloud-offloaded folders (#39 — the
        # rollup now also runs on the daemon's cycle while the dashboard serves).
        tz = user_timezone()
        zone = zone_key(tz)
        git_rows = self._read_git_commits(github_db_path, tz)
        git_idents: dict[str, WorkspaceIdentity] = {}
        for repo_path, *_ in git_rows:
            if repo_path not in git_idents:
                git_idents[repo_path] = resolve_dir(repo_path)
        # Registered repos are never pruned as orphans (#46). ``None`` = the
        # registry could not be read → no pruning this pass (never guess).
        registered = self._registered_repo_paths(github_db_path)
        registered_keys: set[str] | None = None
        if registered is not None:
            for repo_path in registered:
                if repo_path not in git_idents:
                    git_idents[repo_path] = resolve_dir(repo_path)
            registered_keys = {git_idents[p].key for p in registered}

        with self._lock:
            conn = self._conn
            prev_zone = self._rollup_zone_locked()
            transition = prev_zone is not None and prev_zone != zone
            if transition and conn.execute(
                    "SELECT 1 FROM workspace_rollup LIMIT 1").fetchone():
                # The transition rewrites existing rows: never without a
                # consistent copy first. No copy → build on the OLD zone this
                # pass (rows and marker unchanged) and retry next pass.
                try:
                    backup = backup_workspace_db(conn, self.db_path)
                    _log.info("rollup: respaldo de workspace.db en %s antes de "
                              "mover los días de la zona %r a %r",
                              backup, prev_zone, zone)
                except (OSError, sqlite3.Error):
                    _log.warning("rollup: no pude respaldar workspace.db; no muevo "
                                 "los días de la zona %r a %r en esta pasada",
                                 prev_zone, zone, exc_info=True)
                    tz = resolve_timezone(prev_zone)
                    zone = prev_zone
                    transition = False
                    git_rows = self._read_git_commits(github_db_path, tz)
            old_git_rows: list = []
            if transition:
                # Read outside the transaction below; github.db is read-only.
                old_git_rows = self._read_git_commits(
                    github_db_path, resolve_timezone(prev_zone))

            # One local_day per distinct timestamp string (large DBs repeat
            # first_seen / last_seen values across rows).
            day_cache: dict[str, str] = {}

            def _day(ts: str | None) -> str:
                if not ts:
                    return ""
                d = day_cache.get(ts)
                if d is None:
                    d = day_cache[ts] = local_day(ts, tz) or ""
                return d

            # 1. Session signal — one edge per (session, provider, file), or
            #    per (session, provider, cwd) for cwd-fallback edges (#40):
            #    both count the same. Dated on the earliest EVENT (#42), not
            #    on the pass that wrote the edge; both columns are UTC ISO, so
            #    the day is the UTC day, as for git (#23). The reconcile below
            #    zeroes days a re-dated edge left behind.
            for wid, ts in conn.execute(
                """SELECT workspace_id, COALESCE(event_ts, first_seen)
                   FROM path_attributions"""
            ).fetchall():
                _bump(wid, _day(ts), "session", 1, ts)

            # 2. Filesystem signal — the ISO last_seen (not the mtime epoch).
            fs_rows = conn.execute(
                "SELECT workspace_id, last_seen FROM path_touches"
            ).fetchall()
            for wid, ts in fs_rows:
                _bump(wid, _day(ts), "fs", 1, ts)

            try:
                if transition:
                    self._rollup_zone_transition_locked(
                        resolve_timezone(prev_zone), fs_rows, old_git_rows,
                        git_idents, now)
                # 3. Git signal — each repo root maps to a workspace via the
                #    SAME ladder (resolve_dir walks up to the repo's .git, so a
                #    repo root and a deep session/fs path on that repo collide on
                #    one key → one node, three signals). Resolved above, outside
                #    the lock. github.db is opened read-only; absent or
                #    unreadable → git simply contributes nothing.
                for repo_path, day, n, last in git_rows:
                    ident = git_idents.get(repo_path) or resolve_dir(repo_path)
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

                # session_touches is a projection of path_attributions (see
                # the schema note): zero it on keys with no edge left, then
                # prune rows that hold no signal at all (no fs history lost).
                session_keys = {k for k, cell in agg.items() if cell["session"]}
                stale = [
                    (now, wid, day)
                    for wid, day in conn.execute(
                        "SELECT workspace_id, day FROM workspace_rollup"
                        " WHERE session_touches > 0"
                    ).fetchall()
                    if (wid, day) not in session_keys
                ]
                conn.executemany(
                    """UPDATE workspace_rollup SET session_touches = 0, built_at = ?
                       WHERE workspace_id = ? AND day = ?""",
                    stale,
                )
                conn.execute(
                    """DELETE FROM workspace_rollup
                       WHERE session_touches = 0 AND fs_touches = 0 AND git_touches = 0"""
                )
                pruned = (
                    self._prune_orphan_workspaces_locked(registered_keys)
                    if registered_keys is not None else 0
                )
                conn.execute(
                    "INSERT OR REPLACE INTO rollup_meta (key, value) VALUES (?, ?)",
                    (_ROLLUP_ZONE_KEY, zone),
                )
                conn.commit()
            except BaseException:
                conn.rollback()  # never leave a half-built rollup on the shared conn
                raise

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
            "session_reconciled": len(stale),
            "git_repos_matched": git_repos_matched,
            "git_repos_new": git_repos_new,
            "orphans_pruned": pruned,
        }

    @staticmethod
    def _registered_repo_paths(github_db_path: str | Path) -> set[str] | None:
        """Repo paths registered in ``github.db`` (read-only).

        ``set()`` when github.db is absent (nothing is registered); ``None`` when
        it exists but cannot be read — the orphan prune then skips the pass.
        """
        if not os.path.exists(str(github_db_path)):
            return set()
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return None
        try:
            return {p for (p,) in src.execute("SELECT path FROM repos") if p}
        except sqlite3.Error:
            return None
        finally:
            src.close()

    def _prune_orphan_workspaces_locked(self, registered_keys: set[str]) -> int:
        """Delete workspaces nothing points at any more (#46). Caller holds the lock
        and owns the transaction (runs inside ``build_rollup``'s, before commit).

        An orphan has no session edge, no fs touch, no rollup row (so no git
        days either — git lives only in the rollup), no delivery candidate and
        no active day; is not a repo registered in github.db; and its key is
        not another workspace's ``project_key`` (its own classification is
        derived and cascades). No backup: every row here is derived — a
        workspace is re-minted by the next attribution, touch or git pass that
        resolves to its key, so nothing user-owned is lost.
        """
        conn = self._conn
        rows = conn.execute(
            """SELECT w.id, w.workspace_key FROM workspaces w
               WHERE NOT EXISTS (SELECT 1 FROM path_attributions a WHERE a.workspace_id = w.id)
                 AND NOT EXISTS (SELECT 1 FROM path_touches t WHERE t.workspace_id = w.id)
                 AND NOT EXISTS (SELECT 1 FROM workspace_rollup r WHERE r.workspace_id = w.id)
                 AND NOT EXISTS (SELECT 1 FROM delivery_candidates d WHERE d.workspace_id = w.id)
                 AND NOT EXISTS (SELECT 1 FROM edge_active_days e WHERE e.workspace_id = w.id)
                 AND NOT EXISTS (SELECT 1 FROM workspace_classification c
                                 WHERE c.project_key = w.workspace_key
                                   AND c.workspace_id <> w.id)"""
        ).fetchall()
        ids = [(wid,) for wid, key in rows if key not in registered_keys]
        if ids:
            conn.executemany("DELETE FROM workspaces WHERE id = ?", ids)
            _log.info("rollup: %d workspace(s) huérfano(s) eliminados (#46)", len(ids))
        return len(ids)

    def _rollup_zone_locked(self) -> str | None:
        """Zone key ``workspace_rollup.day`` was last built in (``None``: unset)."""
        try:
            row = self._conn.execute(
                "SELECT value FROM rollup_meta WHERE key = ?", (_ROLLUP_ZONE_KEY,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def _rollup_zone_transition_locked(
        self,
        old_tz: Any,
        fs_rows: list[tuple[int, str]],
        old_git_rows: list[tuple[str, str | None, int, str | None]],
        git_idents: dict[str, WorkspaceIdentity],
        now: str,
    ) -> None:
        """Move the rollup from ``old_tz`` days to the current zone, once (#81).

        The surgical reconcile the ``workspace_rollup`` schema note asks for
        when the source-timestamp → day mapping changes (never a blind
        rebuild — fs history before the current ``path_touches`` lives only
        here):

        * ``session_touches`` needs nothing: ``build_rollup`` already zeroes
          every key the current pass does not produce.
        * ``git_touches`` is reproducible from ``github.db``: zero it on every
          key the OLD mapping produced; the pass re-adds it on the new days.
        * ``fs_touches``: the keys the current ``path_touches`` rows produced
          under the OLD mapping hold exactly those rows' counts (the build
          overwrites them each pass), so zero them and let the pass re-add the
          rows on their new day. Older fs history (files re-touched since)
          cannot be re-bucketed — it has no time of day — and keeps its
          original day.

        Runs inside ``build_rollup``'s transaction; all-zero rows are pruned
        there afterwards.
        """
        conn = self._conn
        fs_keys = {(wid, d) for wid, ts in fs_rows
                   if (d := local_day(ts, old_tz))}
        conn.executemany(
            """UPDATE workspace_rollup SET fs_touches = 0, built_at = ?
               WHERE workspace_id = ? AND day = ?""",
            [(now, wid, d) for wid, d in fs_keys],
        )
        git_keys: set[tuple[int, str]] = set()
        for repo_path, day, _n, _last in old_git_rows:
            if day is None:
                continue
            ident = git_idents.get(repo_path) or resolve_dir(repo_path)
            row = conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key = ?", (ident.key,)
            ).fetchone()
            if row:
                git_keys.add((row[0], day))
        conn.executemany(
            """UPDATE workspace_rollup SET git_touches = 0, built_at = ?
               WHERE workspace_id = ? AND day = ?""",
            [(now, wid, d) for wid, d in git_keys],
        )
        _log.info("rollup: días movidos de la zona %r a la actual "
                  "(fs=%d, git=%d claves)", zone_key(old_tz), len(fs_keys),
                  len(git_keys))

    @staticmethod
    def _read_git_commits(
        github_db_path: str | Path,
        tz: Any = CONFIGURED,
    ) -> list[tuple[str, str | None, int, str | None]]:
        """Read git commit day-counts per repo from ``github.db`` (read-only).

        Yields ``(repo_path, day, count, last_ts)`` rows plus, for every repo, a
        sentinel ``(repo_path, None, 0, None)`` so the caller can tell repos it
        matched to a workspace apart from ones it minted (build stats). Returns
        ``[]`` if github.db is absent or has no git tables — git is optional.

        The commit ``day`` is bucketed in Python on the ``_parse_ts``-normalized
        (aware UTC) instant, NOT via SQL ``substr(timestamp, 1, 10)``:
        ``git_commits.timestamp`` is NAIVE *system*-local (git_store migration
        3), so a raw ``substr`` would date a commit in the machine's zone, not
        in the shared local day of ``hub.localtime`` (``tz``, #81) the session
        and filesystem signals use (#23 first aligned them on UTC days).
        ``_parse_ts`` can't run inside SQL, so we pull the (bounded) per-repo
        commits and fold them here; ``last`` is the normalized UTC ISO so the
        caller's lexicographic ``max`` compares against the other signals on
        one clock.
        """
        if not os.path.exists(str(github_db_path)):
            return []
        if tz is CONFIGURED:
            tz = user_timezone()
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
                # day -> (count, latest-UTC-iso), folded on _parse_ts, not substr.
                buckets: dict[str, tuple[int, str]] = {}
                for (ts,) in src.execute(
                    "SELECT timestamp FROM git_commits WHERE repo_id = ?",
                    (repo_id,),
                ).fetchall():
                    dt = _parse_ts(ts)
                    if dt is None:  # unparseable → drop, never mis-bucket
                        continue
                    day = local_day(dt, tz)
                    iso = dt.isoformat()
                    n, last = buckets.get(day, (0, ""))
                    buckets[day] = (n + 1, iso if iso > last else last)
                for day, (n, last) in buckets.items():
                    out.append((repo_path, day, n, last))
        except sqlite3.OperationalError:
            return []
        finally:
            src.close()
        return out

    # --- Outcome layer (issue #27 — Unit 1, authoritative delivery) ---

    @staticmethod
    def read_github_outcome(
        workspace_db_path: str | Path,
        github_db_path: str | Path,
        window: tuple[str, str] | None = None,
        tz: Any = CONFIGURED,
        team: Any = None,
    ) -> dict[str, dict[str, int]]:
        """Merged-PR / closed-issue / open-issue counts per CANONICAL project.

        Reads ``github.db`` READ-ONLY (``mode=ro``, like ``_read_git_commits``)
        and folds each repo's outcome onto the same canonical project the rollup
        already groups commits/sessions under: ``repos.path`` → ``resolve_dir()``
        → ``workspaces.workspace_key`` → ``workspace_classification.project_key``.
        Returns ``{project_key: {"merged_prs": n, "closed_issues": n,
        "open_issues": n}}``.

        With ``window=(first_day, last_day)`` (inclusive LOCAL days in ``tz``,
        issue #81) each project also carries ``merged_prs_window`` and
        ``closed_issues_window``: the PRs whose ``pr_merged_at`` and the issues
        whose ``closed_at`` fall on a local day of the window (``hub.localtime``
        — the same day definition as digests and the rollup). A closed issue
        with no ``closed_at`` counts in the all-time total but never in a
        window: its closing day is unknown.

        The two DBs are held separate (invariant §2.4) and the join goes ONE
        direction — the portfolio reads ``github.db``, never the reverse. Only
        ``github.db`` is opened ``mode=ro``; ``workspace.db`` is opened with a
        plain (read-only-used) connection, the same idiom every read helper in
        ``mcp_server`` uses, avoiding the WAL-``mode=ro`` shm-recovery trap while
        writing nothing.

        **Contributor-agnostic / team-latent (locked #27 decision):** all
        authors are summed at project level — ``author`` is NEVER selected here,
        so no per-person signal leaks out; it stays stored in ``github.db`` for
        the deferred v2.x team view. Merged-PR/closed-issue is a **FACT** for
        git-backed projects; it is a DIFFERENT signal from ``delivery_candidate``
        (#22, the gitless heuristic) and is never conflated with it.

        **Per-contributor, opt-in (#83):** only when ``team`` (a
        ``hub.team.TeamConfig``) has ``per_contributor`` on, each project also
        carries ``contributors`` (``{login or alias: counts}``, the same counts
        plus PR lines from ``github_pr_stats``) and ``contributors_bots`` (what
        bots and login-less items produced), each item credited once
        (``hub.team.credited``). Off — the default — the author is never read.

        Returns ``{}`` when either DB (or the needed tables) is absent —
        GitHub/outcome is optional, exactly like the git signal. A read error
        that fires mid-way (e.g. the classifier is DELETE+re-inserting
        ``workspace_classification`` and a busy timeout trips) discards the WHOLE
        outcome map and returns ``{}`` — all-or-nothing on purpose, so the view
        never shows a half-populated set of counts that reads as authoritative.
        """
        if not os.path.exists(str(github_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        try:
            repos = src.execute("SELECT id, path FROM repos").fetchall()
            # Per-repo outcome (contributor-agnostic — author never selected).
            agg = src.execute(
                """SELECT repo_id,
                          SUM(CASE WHEN is_pull_request = 1
                                    AND pr_merged_at IS NOT NULL
                                   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN is_pull_request = 0
                                    AND state = 'closed'
                                   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN is_pull_request = 0
                                    AND state = 'open'
                                   THEN 1 ELSE 0 END)
                   FROM github_issues GROUP BY repo_id"""
            ).fetchall()
            window_rows = []
            if window is not None:
                window_rows = src.execute(
                    """SELECT repo_id, is_pull_request, pr_merged_at, closed_at
                       FROM github_issues
                       WHERE (is_pull_request = 1 AND pr_merged_at IS NOT NULL)
                          OR (is_pull_request = 0 AND state = 'closed'
                              AND closed_at IS NOT NULL)"""
                ).fetchall()
            people_rows = []
            per_contributor = bool(getattr(team, "per_contributor", False))
            if per_contributor:
                people_rows = _read_contributor_rows(src)
        except sqlite3.OperationalError:
            src.close()
            return {}
        src.close()
        outcome_by_repo = {rid: (m or 0, c or 0, o or 0) for rid, m, c, o in agg}
        # repo_id -> [merged in window, closed in window] (#81)
        window_by_repo: dict[int, list[int]] = {}
        if window is not None:
            if tz is CONFIGURED:
                tz = user_timezone()
            first_day, last_day = window
            for rid, is_pr, merged_at, closed_at in window_rows:
                day = local_day(merged_at if is_pr else closed_at, tz)
                if day is None or not (first_day <= day <= last_day):
                    continue
                cell_w = window_by_repo.setdefault(rid, [0, 0])
                cell_w[0 if is_pr else 1] += 1
        # repo_id -> {contributor | None (bots): counts} (#83, opt-in)
        people_by_repo: dict[int, dict[str | None, dict[str, int]]] = {}
        if per_contributor:
            people_by_repo = _fold_contributors(team, people_rows, window, tz)

        try:
            wconn = sqlite3.connect(str(workspace_db_path), timeout=5)
        except sqlite3.OperationalError:
            return {}
        out: dict[str, dict[str, int]] = {}
        key_cache: dict[str, str | None] = {}
        try:
            for repo_id, repo_path in repos:
                if not repo_path:
                    continue
                counts = outcome_by_repo.get(repo_id)
                if not counts or not any(counts):
                    continue
                keyed = _keyed_repo_project_key(repo_path)
                if keyed is not None:  # clone-less repo (#93/#94)
                    key_cache.setdefault(repo_path, keyed)
                pk = key_cache.get(repo_path, ...)  # type: ignore[arg-type]
                if pk is ...:
                    ident = resolve_dir(repo_path)
                    try:
                        row = wconn.execute(
                            """SELECT c.project_key
                               FROM workspaces w
                               JOIN workspace_classification c
                                 ON c.workspace_id = w.id
                               WHERE w.workspace_key = ?""",
                            (ident.key,),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        return {}
                    pk = row[0] if row and row[0] else None
                    key_cache[repo_path] = pk
                if pk is None:  # repo has no canonical project — skip, never guess
                    continue
                cell = out.setdefault(
                    pk, {"merged_prs": 0, "closed_issues": 0, "open_issues": 0}
                )
                cell["merged_prs"] += counts[0]
                cell["closed_issues"] += counts[1]
                cell["open_issues"] += counts[2]
                if window is not None:
                    wcounts = window_by_repo.get(repo_id, (0, 0))
                    cell["merged_prs_window"] = (
                        cell.get("merged_prs_window", 0) + wcounts[0])
                    cell["closed_issues_window"] = (
                        cell.get("closed_issues_window", 0) + wcounts[1])
                if per_contributor:
                    _merge_contributors(cell, people_by_repo.get(repo_id, {}))
        finally:
            wconn.close()
        return out

    @staticmethod
    def read_history_coverage(
        workspace_db_path: str | Path,
        github_db_path: str | Path,
        tz: Any = CONFIGURED,
    ) -> dict[str, dict[str, Any]]:
        """Per-project git-history coverage: was the full history ingested (#59)?

        Reads ``github.db`` read-only: each repo's ``history_complete`` flag
        (marked by ``repo add/sync --all``; absent column on a pre-#59 DB reads
        as NOT complete) and its earliest ingested commit date. Folds onto the
        same canonical project as the outcome (``resolve_dir`` → classification
        ``project_key``). Returns ``{project_key: {"complete": bool, "since":
        "YYYY-MM-DD"|None}}``; ``since`` is the earliest commit across the
        project's repos, as a LOCAL day (``hub.localtime``, #81).
        All-or-nothing; ``{}`` when either DB/table is absent.
        """
        if not os.path.exists(str(github_db_path)):
            return {}
        if tz is CONFIGURED:
            tz = user_timezone()
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        try:
            try:
                repos = src.execute(
                    "SELECT id, path, history_complete FROM repos").fetchall()
            except sqlite3.OperationalError:
                repos = [(r[0], r[1], 0) for r in
                         src.execute("SELECT id, path FROM repos").fetchall()]
            mins = dict(src.execute(
                "SELECT repo_id, MIN(timestamp) FROM git_commits GROUP BY repo_id"
            ).fetchall())
        except sqlite3.OperationalError:
            src.close()
            return {}
        src.close()

        try:
            wconn = sqlite3.connect(str(workspace_db_path), timeout=5)
        except sqlite3.OperationalError:
            return {}
        out: dict[str, dict[str, Any]] = {}
        key_cache: dict[str, str | None] = {}
        try:
            for repo_id, repo_path, complete in repos:
                if not repo_path:
                    continue
                keyed = _keyed_repo_project_key(repo_path)
                if keyed is not None:  # clone-less repo (#93/#94)
                    key_cache.setdefault(repo_path, keyed)
                pk = key_cache.get(repo_path, ...)  # type: ignore[arg-type]
                if pk is ...:
                    ident = resolve_dir(repo_path)
                    try:
                        row = wconn.execute(
                            """SELECT c.project_key
                               FROM workspaces w
                               JOIN workspace_classification c
                                 ON c.workspace_id = w.id
                               WHERE w.workspace_key = ?""",
                            (ident.key,),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        return {}
                    pk = row[0] if row and row[0] else None
                    key_cache[repo_path] = pk
                if pk is None:
                    continue
                cell = out.setdefault(pk, {"complete": True, "since": None})
                if not complete:
                    cell["complete"] = False
                ts = mins.get(repo_id)
                if ts:
                    day = local_day(ts, tz) or str(ts)[:10]
                    if cell["since"] is None or day < cell["since"]:
                        cell["since"] = day
        finally:
            wconn.close()
        return out

    # --- Derived project state (issue #28 — Unit 2, integrate) ---

    # Human-scale quiescence bands for the per-project state chip. UNLIKE
    # ``QUIET_WINDOW_SECONDS`` (a modest 2h gate where a recorded SECOND signal
    # carries the claim), a *state* IS the read of evidence, so the bands are
    # day-scale: below ACTIVE the project is warm, between ACTIVE and COOLING it
    # is tapering (enfriándose), past COOLING it is quiet and its GitHub outcome
    # decides delivered / stalled / paused. Injectable (like
    # ``quiet_window_seconds``) so tests pin exact ages instead of racing wall time.
    STATE_ACTIVE_WINDOW_SECONDS: float = 3 * 86400.0     # < 3d  → activo/caliente
    STATE_COOLING_WINDOW_SECONDS: float = 14 * 86400.0   # 3–14d → enfriándose; ≥14d quiet
    # A GitHub delivery (merged PR / closed issue) counts as *closing the burst*
    # only when it lands within this slack of the project's last local activity —
    # the same "did this signal close the burst" discipline as
    # ``detect_delivery_candidates``, but deliberately day-scale, NOT that
    # method's 120s: the remote GitHub clock and the local activity clock are
    # independent streams, so a merge is essentially never within minutes of the
    # last local touch. Too-tight a slack would make ``entregado`` never fire.
    STATE_DELIVERY_SLACK_SECONDS: float = 3 * 86400.0

    def derive_project_states(
        self,
        events_db_path: str | Path | None = None,
        github_db_path: str | Path | None = None,
        now: datetime | None = None,
        active_window_seconds: float | None = None,
        cooling_window_seconds: float | None = None,
        delivery_slack_seconds: float | None = None,
        session_ingest: dict[tuple[str, str], datetime] | None = None,
        own_emails: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Derive one honest STATE per canonical project — the epic integrator.

        Fuses **local activity** (session ingest, filesystem touches, git commits)
        with **GitHub outcome** (#27) into a single read per ``project_key``:

          * ``activo``      — real activity within the ACTIVE window.
          * ``enfriandose`` — activity tapering (ACTIVE ≤ age < COOLING).
          * ``entregado``   — quiet + a merged-PR/closed-issue that *closed the
            burst* (git, a FACT); or, for a gitless project, a
            ``delivery_candidate`` (#22, the heuristic) — never conflated.
          * ``estancado``   — quiet + a repo with open issues still hanging.
          * ``pausado``     — quiet + nothing open and no measurable outcome.

        **Honest clocks only.** Quiescence age is measured from the *real* last
        activity: ``max`` of each edge's OWN clock
        (``path_attributions.activity_ts``, #56 — the latest event of that edge
        per the #53 rule, so a long session that touched project A weeks ago
        and B today dates A to weeks ago and B to today, not both to today),
        ``path_touches.last_seen``,
        and ``git_commits.timestamp``, each normalized to aware UTC (epoch via
        ``fromtimestamp(..., utc)``, the two string clocks via ``_parse_ts``).
        The session activity clock (:func:`read_session_activity`) is only a
        fallback for an edge with NO per-edge clock at all (a legacy row whose
        events are gone from events.db); it is not scanned when every edge
        carries its own clock. NEVER ``path_attributions.first_seen`` (a
        backfill artifact) and NEVER
        session ``duration`` (unreliable on resume) — same discipline as
        ``detect_delivery_candidates``. A project with NO real activity yields NO
        state (evidence-first; absence is never fabricated into ``pausado``).

        **``outcome_measurable`` (has-repo vs not).** Comes from the *repos* side
        of ``github.db``, not the outcome counts: a repo-backed project with zero
        PRs still has a repo, so it CAN be ``estancado`` when issues hang; a
        gitless project's outcome is *not measurable* and it can only fall to
        ``entregado`` (via ``delivery_candidate``) or ``pausado`` — never
        ``estancado`` for lacking PRs it could never have (the #28 comment).

        Each state carries its ``basis`` (which signal determined it) + the
        contributing counts + ``last_activity``/``age_days`` — a read of evidence
        surfaced with its ground, never a bare asserted flag. Reads ``events.db``
        and ``github.db`` strictly read-only; the         ``delivery_candidates`` table is
        only READ here (this never runs detection or writes it). Returns
        ``{project_key: {state, basis, last_activity, age_days,
        outcome_measurable, merged_prs, closed_issues, open_issues, team_only}}``.
        With ``own_emails`` set, a hot state sustained only by commits of other
        people carries ``team_only`` (nothing per-person is exposed).
        """
        events_db_path = events_db_path or (self.db_path.parent / "events.db")
        github_db_path = github_db_path or (self.db_path.parent / "github.db")
        now = now or datetime.now(timezone.utc)
        active_w = (
            self.STATE_ACTIVE_WINDOW_SECONDS if active_window_seconds is None
            else active_window_seconds
        )
        cooling_w = (
            self.STATE_COOLING_WINDOW_SECONDS if cooling_window_seconds is None
            else cooling_window_seconds
        )
        slack = timedelta(seconds=(
            self.STATE_DELIVERY_SLACK_SECONDS if delivery_slack_seconds is None
            else delivery_slack_seconds
        ))

        # Readers that open their own connections / take the store lock: call
        # them OUTSIDE the lock block below (never re-enter self._lock).
        own = frozenset(
            e.strip().lower() for e in (own_emails or []) if e and e.strip()
        )
        git_last = self._read_git_latest_by_workspace(github_db_path, own or None)
        github_state = self._read_github_state(github_db_path)

        # Real last-activity clock per canonical project, folded over every
        # workspace that maps to it. ``last_own`` folds the SAME clocks but only
        # for sources that are always the owner's: session edges, filesystem
        # touches, and commits authored by one of ``own_emails`` (decisión 1A).
        last_real: dict[str, datetime] = {}
        last_own: dict[str, datetime] = {}

        def _bump(pk: str | None, ts: datetime | None) -> None:
            if pk is None or ts is None:
                return
            cur = last_real.get(pk)
            if cur is None or ts > cur:
                last_real[pk] = ts

        def _bump_own(pk: str | None, ts: datetime | None) -> None:
            if pk is None or ts is None:
                return
            cur = last_own.get(pk)
            if cur is None or ts > cur:
                last_own[pk] = ts

        # Edge clocks (#56) collected under the lock, folded after it: an edge
        # with no clock of its own may need the session-activity scan, which
        # opens events.db and must stay outside self._lock.
        edge_clocks: list[tuple[int, str, str, datetime | None]] = []
        wid_pk: dict[int, str] = {}
        with self._lock:
            conn = self._conn
            for wid, pk in conn.execute(
                "SELECT workspace_id, project_key FROM workspace_classification "
                "WHERE project_key IS NOT NULL"
            ):
                wid_pk[wid] = pk
            # Each edge's own clock: activity_ts (#56), or event_ts as the
            # pre-#56 seed; first_seen is never a clock.
            for wid, sid, prov, ats, ets in conn.execute(
                "SELECT workspace_id, session_id, provider, activity_ts, event_ts"
                " FROM path_attributions"
            ):
                edge_clocks.append(
                    (wid, sid, prov or "", _parse_ts(ats) or _parse_ts(ets))
                )
            # Filesystem watcher touches (always the owner's activity).
            for wid, last_seen in conn.execute(
                "SELECT workspace_id, last_seen FROM path_touches"
            ):
                pk = wid_pk.get(wid)
                ts_fs = _parse_ts(last_seen)
                _bump(pk, ts_fs)
                _bump_own(pk, ts_fs)
            # Git commits (already parsed to aware UTC by the reader). The third
            # element is the latest commit authored by the owner (or None).
            for wid, (ts, _sha, own_ts) in git_last.items():
                pk = wid_pk.get(wid)
                _bump(pk, ts)
                _bump_own(pk, own_ts)
            # Gitless delivery heuristic: which projects have a candidate row.
            dc_projects: set[str] = set()
            for (wid,) in conn.execute(
                "SELECT DISTINCT workspace_id FROM delivery_candidates"
            ):
                pk = wid_pk.get(wid)
                if pk is not None:
                    dc_projects.add(pk)

        # Lazy session fallback: only when some edge carries no clock at all.
        # A caller that already computed the map (the production view) hands it
        # in, so the ~194MB scan is paid once at most (invariant §2.4).
        if session_ingest is None and any(dt is None for *_, dt in edge_clocks):
            session_ingest = self._read_session_activity(events_db_path)
        for wid, sid, prov, dt in edge_clocks:
            if dt is None and session_ingest:
                dt = session_ingest.get((sid, prov))
            pk = wid_pk.get(wid)
            _bump(pk, dt)
            _bump_own(pk, dt)  # a local session edge is always the owner's

        out: dict[str, dict[str, Any]] = {}
        for pk, lr in last_real.items():
            gh = github_state.get(pk, {})
            measurable = bool(gh.get("has_repo", False))
            merged = int(gh.get("merged_prs", 0))
            closed = int(gh.get("closed_issues", 0))
            open_ = int(gh.get("open_issues", 0))
            age = (now - lr).total_seconds()

            if age < active_w:
                state, basis = "activo", "recent_activity"
            elif age < cooling_w:
                state, basis = "enfriandose", "tapering_activity"
            else:
                # Quiet band — the outcome decides. A delivery closes the burst
                # only if it landed within slack of the real last activity.
                cutoff = lr - slack
                lm = gh.get("latest_merge")
                lc = gh.get("latest_close")
                merged_closing = lm is not None and lm >= cutoff
                closed_closing = lc is not None and lc >= cutoff
                if merged_closing or closed_closing:
                    state = "entregado"
                    # Attribute to whichever qualifying signal is the more recent.
                    if merged_closing and (lc is None or lm >= lc):
                        basis = "merged_pr"
                    else:
                        basis = "closed_issue"
                elif not measurable and pk in dc_projects:
                    # Gitless: fall back to the local heuristic (never a FACT).
                    state, basis = "entregado", "delivery_candidate"
                elif measurable and open_ > 0:
                    state, basis = "estancado", "open_issues"
                else:
                    state, basis = "pausado", "quiescent_no_outcome"

            # Team-latent mark (issue #36, decisión 1A): only meaningful with
            # own emails configured. A hot state is "team_only" when no own
            # activity falls inside the band that sustains it — the state's own
            # window (activo → ACTIVE band; enfriándose → COOLING band). A
            # recent own commit/session/fs touch clears the mark. Nothing
            # per-person is ever exposed: just the boolean.
            team_only = False
            if own and state in ("activo", "enfriandose"):
                own_dt = last_own.get(pk)
                band = active_w if state == "activo" else cooling_w
                if own_dt is None or (now - own_dt).total_seconds() >= band:
                    team_only = True

            out[pk] = {
                "state": state,
                "basis": basis,
                "last_activity": lr.isoformat(),
                "age_days": round(age / 86400.0, 2),
                "outcome_measurable": measurable,
                "merged_prs": merged,
                "closed_issues": closed,
                "open_issues": open_,
                "team_only": team_only,
            }
        return out

    @staticmethod
    def _read_session_activity(
        events_db_path: str | Path,
    ) -> dict[tuple[str, str], datetime]:
        """Real session activity recency from events.db (read-only).

        ``{(session_id, provider): aware-UTC datetime}`` from
        :func:`read_session_activity`: the ingest epoch for live rows (#18),
        the parsed event time for historical rows (#53).
        """
        if not os.path.exists(str(events_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        try:
            return read_session_activity(src)
        except sqlite3.OperationalError:
            return {}
        finally:
            src.close()

    def _read_github_state(
        self, github_db_path: str | Path
    ) -> dict[str, dict[str, Any]]:
        """Per-project GitHub state for the state chip (read-only over github.db).

        Like ``read_github_outcome`` but two things differ, both required by the
        #28 model: (1) ``has_repo`` is folded from the *repos* side, so a
        repo-backed project with ZERO PRs/issues still reports measurable (the
        outcome map would drop it — line-733 ``any(counts)`` filter — reproducing
        the "repo-with-0-PRs looks gitless" bug the #28 comment calls out); and
        (2) the *latest* merged-PR / closed-issue timestamps are carried so the
        state layer can tell a delivery that CLOSED the burst from an old one.
        ``read_github_outcome`` (#27, tested + consumed by the production view) is
        left untouched — the state layer uses its own reader.

        Contributor-agnostic (author never selected). Returns
        ``{project_key: {merged_prs, closed_issues, open_issues, has_repo,
        latest_merge, latest_close}}`` (the two timestamps aware-UTC or ``None``);
        ``{}`` when github.db or its tables are absent.
        """
        if not os.path.exists(str(github_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        try:
            repos = src.execute("SELECT id, path FROM repos").fetchall()
            agg = src.execute(
                """SELECT repo_id,
                          SUM(CASE WHEN is_pull_request = 1
                                    AND pr_merged_at IS NOT NULL
                                   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN is_pull_request = 0
                                    AND state = 'closed'
                                   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN is_pull_request = 0
                                    AND state = 'open'
                                   THEN 1 ELSE 0 END),
                          MAX(CASE WHEN is_pull_request = 1
                                    AND pr_merged_at IS NOT NULL
                                   THEN pr_merged_at END),
                          MAX(CASE WHEN is_pull_request = 0
                                    AND state = 'closed'
                                   THEN closed_at END)
                   FROM github_issues GROUP BY repo_id"""
            ).fetchall()
        except sqlite3.OperationalError:
            src.close()
            return {}
        src.close()
        by_repo = {
            rid: (m or 0, c or 0, o or 0, mt, ct)
            for rid, m, c, o, mt, ct in agg
        }

        out: dict[str, dict[str, Any]] = {}
        key_cache: dict[str, str | None] = {}
        with self._lock:
            for repo_id, repo_path in repos:
                if not repo_path:
                    continue
                keyed = _keyed_repo_project_key(repo_path)
                if keyed is not None:  # clone-less repo (#93/#94)
                    key_cache.setdefault(repo_path, keyed)
                pk = key_cache.get(repo_path, ...)  # type: ignore[arg-type]
                if pk is ...:
                    ident = resolve_dir(repo_path)
                    try:
                        row = self._conn.execute(
                            """SELECT c.project_key
                               FROM workspaces w
                               JOIN workspace_classification c
                                 ON c.workspace_id = w.id
                               WHERE w.workspace_key = ?""",
                            (ident.key,),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        return {}
                    pk = row[0] if row and row[0] else None
                    key_cache[repo_path] = pk
                if pk is None:  # repo has no canonical project — skip, never guess
                    continue
                m, c, o, mt, ct = by_repo.get(repo_id, (0, 0, 0, None, None))
                cell = out.setdefault(pk, {
                    "merged_prs": 0, "closed_issues": 0, "open_issues": 0,
                    "has_repo": True, "latest_merge": None, "latest_close": None,
                })
                cell["has_repo"] = True
                cell["merged_prs"] += m
                cell["closed_issues"] += c
                cell["open_issues"] += o
                mtd = _parse_ts(mt)
                if mtd is not None and (
                    cell["latest_merge"] is None or mtd > cell["latest_merge"]
                ):
                    cell["latest_merge"] = mtd
                ctd = _parse_ts(ct)
                if ctd is not None and (
                    cell["latest_close"] is None or ctd > cell["latest_close"]
                ):
                    cell["latest_close"] = ctd
        return out

    # --- delivery_candidate (issue #22 — Phase C, correlate) ---

    # Precondition window: a workspace must have been silent (across REAL clocks)
    # at least this long before it can be a delivery candidate. Modest on purpose
    # — quiescence is only the gate; the recorded second signal carries the claim.
    QUIET_WINDOW_SECONDS: float = 2 * 3600.0
    # A signal must be the *closing* activity — its timestamp within this slack of
    # the last real activity — not an old event buried mid-burst.
    _SIGNAL_SLACK_SECONDS: float = 120.0

    def detect_delivery_candidates(
        self,
        events_db_path: str | Path | None = None,
        github_db_path: str | Path | None = None,
        quiet_window_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Detect likely-delivered workspaces LOCALLY — candidates, never facts.

        Quiescence (the workspace went silent across the REAL clocks —
        ``path_touches.last_seen``, git commit times, and session
        ``COALESCE(ended_at, last_event_at)``; NEVER ``path_attributions.first_seen``,
        which is a backfill artifact) is a *precondition only*. A row is written
        only when, on top of quiescence, one of three admissible second signals
        *closed the burst*: ``session_close`` (real ``ended_at``, #16),
        ``git_commit`` (a commit was the last activity), or ``root_artifact`` (a
        file at the workspace root with an extension outside the working set).

        The firing signal + its detail are recorded per row so the confidence is
        auditable. A workspace that is no longer quiescent (or fires no signal)
        has its rows EVICTED — never a stale candidate over an active workspace.
        No LLM, no writes to events.db (opened read-only).
        """
        events_db_path = events_db_path or (self.db_path.parent / "events.db")
        github_db_path = github_db_path or (self.db_path.parent / "github.db")
        quiet_window = (
            self.QUIET_WINDOW_SECONDS if quiet_window_seconds is None
            else quiet_window_seconds
        )
        now = now or datetime.now(timezone.utc)
        slack = timedelta(seconds=self._SIGNAL_SLACK_SECONDS)
        now_iso = _now()

        # Real session clocks, keyed (session_id, provider): (activity_ts, close_ts).
        sess_clock = self._read_session_clocks(events_db_path)
        # Latest git commit per workspace_id: (ts, sha).
        git_last = self._read_git_latest_by_workspace(github_db_path)

        emitted = 0
        by_signal: dict[str, int] = {"session_close": 0, "git_commit": 0, "root_artifact": 0}
        quiescent = 0

        with self._lock:
            conn = self._conn
            workspaces = conn.execute(
                "SELECT id, kind, root_path, dir_path FROM workspaces"
            ).fetchall()

            # Per-workspace filesystem touches and session/attribution files.
            touches: dict[int, list[tuple[str, datetime | None]]] = {}
            for wid, path, last_seen in conn.execute(
                "SELECT workspace_id, path, last_seen FROM path_touches"
            ):
                touches.setdefault(wid, []).append((path, _parse_ts(last_seen)))

            work_exts: dict[int, dict[str, int]] = {}
            wsessions: dict[int, set[tuple[str, str]]] = {}
            for wid, sid, prov, fp, via in conn.execute(
                """SELECT workspace_id, session_id, provider, file_path, via
                   FROM path_attributions"""
            ):
                wsessions.setdefault(wid, set()).add((sid, prov or ""))
                work_exts.setdefault(wid, {})
                if via == "cwd":
                    continue  # a directory, not a file — no extension signal
                work_exts[wid][_ext(fp)] = work_exts[wid].get(_ext(fp), 0) + 1

            # ONE transaction for every per-workspace DELETE/INSERT below, so a
            # WAL reader (the dashboard, while the daemon refreshes — #39) sees
            # the previous candidate set or the new one, never a half-built mix.
            try:
                for wid, kind, root_path, dir_path in workspaces:
                    root = root_path if kind in ("git_remote", "git_root") else dir_path
                    w_touches = touches.get(wid, [])

                    # --- Real clocks -------------------------------------------------
                    fs_last = max(
                        (t for _, t in w_touches if t is not None), default=None
                    )
                    gl = git_last.get(wid)  # (datetime, sha) | None
                    git_ts = gl[0] if gl else None

                    sess_activity_last: datetime | None = None
                    close_ts: datetime | None = None
                    close_sid: str | None = None
                    for (sid, prov) in wsessions.get(wid, set()):
                        act, close = sess_clock.get((sid, prov), (None, None))
                        if act and (sess_activity_last is None or act > sess_activity_last):
                            sess_activity_last = act
                        if close and (close_ts is None or close > close_ts):
                            close_ts, close_sid = close, sid

                    candidates_ts = [t for t in (fs_last, git_ts, sess_activity_last) if t]
                    if not candidates_ts:
                        conn.execute("DELETE FROM delivery_candidates WHERE workspace_id=?", (wid,))
                        continue
                    last_real = max(candidates_ts)

                    # --- Precondition: quiescence across REAL clocks -----------------
                    if (now - last_real).total_seconds() < quiet_window:
                        # Still active (or a pause) — evict any stale candidate.
                        conn.execute("DELETE FROM delivery_candidates WHERE workspace_id=?", (wid,))
                        continue
                    quiescent += 1

                    # --- Second signal: what CLOSED the burst (within slack) ---------
                    fires: list[tuple[str, str | None]] = []
                    if git_ts and git_ts >= last_real - slack:
                        fires.append(("git_commit", gl[1]))
                    if close_ts and close_ts >= last_real - slack:
                        fires.append(("session_close", close_sid))

                    # root_artifact: a file AT the workspace root whose extension is
                    # outside the working set (the extensions of the normal source
                    # tree). Restricted to git workspaces: a "root" is only
                    # meaningful when there is a real tree BELOW it. For a path_hash
                    # node every directory is its own workspace, so root == dir_path
                    # and every touch is "at root" with an empty below-root set —
                    # two single-occurrence extensions (brief.pdf + logo.svg in a
                    # materials folder) would each read as an artifact and fabricate
                    # a delivery. Skip path_hash here (issue #22).
                    if root and kind in ("git_remote", "git_root"):
                        ws = self._working_set_exts(wid, work_exts, w_touches, root)
                        best_art: tuple[datetime, str] | None = None
                        for path, t in w_touches:
                            if t is None or os.path.dirname(path) != root:
                                continue
                            e = _ext(path)
                            if not e or e in ws:
                                continue
                            if best_art is None or t > best_art[0]:
                                best_art = (t, path)
                        if best_art and best_art[0] >= last_real - slack:
                            fires.append(("root_artifact", best_art[1]))

                    # Quiescence ALONE never emits.
                    conn.execute("DELETE FROM delivery_candidates WHERE workspace_id=?", (wid,))
                    if not fires:
                        continue

                    q_since = last_real.isoformat()
                    quiet_hours = (now - last_real).total_seconds() / 3600.0
                    for signal, detail in fires:
                        conn.execute(
                            """INSERT OR REPLACE INTO delivery_candidates
                                   (workspace_id, signal, signal_detail,
                                    quiescent_since, confidence, detected_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (wid, signal, detail, q_since,
                             _delivery_confidence(signal, quiet_hours), now_iso),
                        )
                        by_signal[signal] += 1
                        emitted += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"candidates": emitted, "by_signal": by_signal, "quiescent_workspaces": quiescent}

    @staticmethod
    def _working_set_exts(
        wid: int,
        work_exts: dict[int, dict[str, int]],
        touches: list[tuple[str, datetime | None]],
        root: str,
    ) -> set[str]:
        """Extensions that make up a workspace's *normal* working tree.

        An extension is "in the working set" if it appears in a file BELOW the
        root (part of the source tree) or occurs ≥2 times overall — so a one-off
        deliverable dropped at the root (a ``.zip``/``.pdf``/``.pptx`` export)
        with a singleton, otherwise-unseen extension reads as a new artifact.
        """
        counts: dict[str, int] = dict(work_exts.get(wid, {}))
        below_root: set[str] = set()
        for path, _ in touches:
            e = _ext(path)
            counts[e] = counts.get(e, 0) + 1
            if os.path.dirname(path) != root:
                below_root.add(e)
        return {e for e in below_root if e} | {e for e, n in counts.items() if e and n >= 2}

    @staticmethod
    def _read_session_clocks(
        events_db_path: str | Path,
    ) -> dict[tuple[str, str], tuple[datetime | None, datetime | None]]:
        """Read real session clocks from events.db (read-only).

        Returns ``{(session_id, provider): (activity_ts, close_ts)}`` where
        ``activity_ts = COALESCE(NULLIF(ended_at,''), NULLIF(last_event_at,''))``
        (the real last activity) and ``close_ts`` is ``ended_at`` ONLY (a real
        terminal signal, #16) — never inferred from event age.
        """
        if not os.path.exists(str(events_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        out: dict[tuple[str, str], tuple[datetime | None, datetime | None]] = {}
        try:
            for sid, prov, last_event_at, ended_at in src.execute(
                "SELECT id, provider, last_event_at, ended_at FROM sessions"
            ):
                close = _parse_ts(ended_at)
                activity = close or _parse_ts(last_event_at)
                out[(sid, prov or "")] = (activity, close)
        except sqlite3.OperationalError:
            return {}
        finally:
            src.close()
        return out

    def _read_git_latest_by_workspace(
        self, github_db_path: str | Path,
        own_emails: frozenset[str] | None = None,
    ) -> dict[int, tuple[datetime, str, datetime | None]]:
        """Latest git commit per workspace_id: ``{wid: (ts, sha, own_ts)}``.

        Read-only. Each repo root is resolved to its workspace via the SAME
        ladder as the rollup, so git lands on the same node as session/fs
        activity. Timestamps are parsed to aware UTC (git stores naive-local —
        see ``_parse_ts``).

        ``own_ts`` (issue #36, decisión 1A) is the latest commit whose author
        email is in ``own_emails`` (pre-normalized lowercase), or ``None`` when
        no own commit exists / no emails are configured. The author is NEVER
        surfaced — only the timestamp is folded, so team-latent holds.
        """
        if not os.path.exists(str(github_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        out: dict[int, tuple[datetime, str, datetime | None]] = {}
        try:
            rows = src.execute(
                """SELECT r.path, c.sha, c.timestamp, c.author_email
                   FROM git_commits c JOIN repos r ON r.id = c.repo_id"""
            ).fetchall()
        except sqlite3.OperationalError:
            # Legacy/synthetic DB without ``author_email``: no own-commit signal
            # is derivable, so the team mark simply stays off (never a false one).
            try:
                rows = src.execute(
                    """SELECT r.path, c.sha, c.timestamp, NULL
                       FROM git_commits c JOIN repos r ON r.id = c.repo_id"""
                ).fetchall()
            except sqlite3.OperationalError:
                src.close()
                return {}
        src.close()
        # Resolve repo roots to workspace keys OUTSIDE the lock (disk I/O).
        repo_keys: dict[str, str] = {}
        for repo_path, _, _, _ in rows:
            if repo_path and repo_path not in repo_keys:
                repo_keys[repo_path] = resolve_dir(repo_path).key
        key_cache: dict[str, int | None] = {}
        with self._lock:
            for repo_path, sha, ts_raw, email in rows:
                if not repo_path:
                    continue
                wid = key_cache.get(repo_path, ...)  # type: ignore[arg-type]
                if wid is ...:
                    row = self._conn.execute(
                        "SELECT id FROM workspaces WHERE workspace_key = ?",
                        (repo_keys[repo_path],),
                    ).fetchone()
                    wid = row[0] if row else None
                    key_cache[repo_path] = wid
                if wid is None:
                    continue
                ts = _parse_ts(ts_raw)
                if ts is None:
                    continue
                cur = out.get(wid)
                if cur is None:
                    own = ts if _is_own_email(email, own_emails) else None
                    out[wid] = (ts, sha, own)
                else:
                    own = cur[2]
                    if _is_own_email(email, own_emails) and (own is None or ts > own):
                        own = ts
                    out[wid] = (ts, sha, own) if ts > cur[0] else (cur[0], cur[1], own)
        return out

    def get_delivery_candidates(self) -> list[dict[str, Any]]:
        """Current delivery candidates, joined to their workspace identity."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT w.workspace_key, w.kind, w.remote_url, w.root_path,
                          w.dir_path, d.signal, d.signal_detail, d.quiescent_since,
                          d.confidence, d.detected_at
                   FROM delivery_candidates d
                   JOIN workspaces w ON w.id = d.workspace_id
                   ORDER BY d.confidence DESC, d.quiescent_since DESC"""
            ).fetchall()
        return [
            {
                "workspace_key": r[0], "kind": r[1], "remote_url": r[2],
                "root_path": r[3], "dir_path": r[4], "signal": r[5],
                "signal_detail": r[6], "quiescent_since": r[7],
                "confidence": r[8], "detected_at": r[9],
            }
            for r in rows
        ]

    # --- Backfill (populate from already-persisted events, read-only) ---

    # Rows upserted per lock hold / commit. Bounds how long one attribution
    # pass can hold self._lock (the watcher and the portfolio reads share it).
    _ATTRIBUTION_CHUNK = 500

    # Only absolute paths qualify: non-absolute ``events.file_path`` values are
    # overwhelmingly Bash command strings, not paths (issue #20). "Absolute" is
    # portable (#47): POSIX ``/...`` plus Windows drive-letter/UNC spellings,
    # which the Codex adapter already normalizes with ``ntpath``.
    _ABS_FILE_PATH = SQL_PORTABLE_ABS.format(col="file_path")
    _ATTR_WHERE = (
        f"{_ABS_FILE_PATH} AND session_id IS NOT NULL AND session_id != ''"
    )
    _SKIPPED_WHERE = (
        f"file_path IS NOT NULL AND file_path != '' AND NOT {_ABS_FILE_PATH}"
        " AND session_id IS NOT NULL AND session_id != ''"
    )

    # Shell tools put their COMMAND in ``events.file_path`` (#58): a command
    # starting with an absolute binary path (``/usr/libexec/... --flag``) passed
    # ``file_path LIKE '/%'`` and minted fake workspaces. Attribution ignores
    # the file_path of these events BY TOOL NAME — never by text pattern, which
    # would drop real folders with spaces. One entry per adapter carrier:
    #   * Claude  ``Bash``               — command[:80] on the event
    #   * OpenCode ``bash``              — command[:80] on the event
    #   * Qwen    ``run_shell_command``  — command[:80] on the event
    #   * Codex   ``shell``              — legacy function_call; command[:80]
    #   * Codex   ``exec_command``       — its successor; today its file_path is
    #     already NULL (the adapter no longer writes the command), but older
    #     databases ingested live by previous adapters may carry a command row.
    # Deliberately NOT listed: Codex ``exec`` / ``apply_patch`` / ``view_image``
    # — since #40 those only store paths extracted from the patch (real files),
    # and ``exec`` custom calls never put a command in file_path.
    # Single source of truth: ``hub.models.base.SHELL_TOOLS`` (#58). The
    # adapters never mint a shell command as a path, and this pass still
    # ignores any legacy row that carries one.
    _SHELL_TOOLS = SHELL_TOOLS

    @staticmethod
    def _read_max_event_id(events_db_path: str | Path) -> int:
        """``MAX(events.id)`` from events.db (read-only); 0 for an empty table."""
        src = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True, timeout=5)
        try:
            return src.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        finally:
            src.close()

    # cwd-fallback edges (#40): only for sessions with NO file edge anywhere in
    # path_attributions. The existence check is repeated inside the INSERT so a
    # file edge committed after the pre-check still wins (no mixed session).
    _CWD_EDGE_SQL_TEMPLATE = """INSERT INTO path_attributions
               (session_id, provider, file_path, workspace_id, resolved_via,
                first_seen, via, event_ts, activity_ts)
           SELECT ?, ?, ?, ?, ?, ?, 'cwd', ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM path_attributions
               WHERE session_id = ? AND provider = ? AND via = 'file')
           ON CONFLICT(session_id, provider, file_path) DO UPDATE SET
               workspace_id = excluded.workspace_id,
               resolved_via = excluded.resolved_via,
               {event},
               {activity}"""
    _CWD_EDGE_SQL = _CWD_EDGE_SQL_TEMPLATE.format(
        event=_EVENT_TS_UPSERT, activity=_ACTIVITY_TS_UPSERT)
    # #101 re-derivation: the full pass replaces the cwd edge's clock too.
    _CWD_EDGE_SQL_REPLACE = _CWD_EDGE_SQL_TEMPLATE.format(
        event=_EVENT_TS_UPSERT, activity=_ACTIVITY_TS_REPLACE)

    @staticmethod
    def _cwd_qualifies(cwd: str | None, home: str) -> bool:
        """An absolute cwd that is neither the filesystem root nor ``~``.

        "Absolute" is portable (#47): a Windows drive-letter/UNC cwd qualifies
        on any host and is normalized with ``ntpath``. Everything else (noise
        parents, harness dirs) is left to the read-layer classification (#36).
        """
        if not cwd or not is_portable_abs(cwd):
            return False
        mod = path_module(cwd)
        norm = mod.normpath(cwd)
        return norm != home and mod.dirname(norm) != norm

    def _attribute_event_range(
        self, events_db_path: str | Path, lo: int, hi: int,
        replace_activity: bool = False,
        day_tz: Any = CONFIGURED,
    ) -> dict[str, int]:
        """Attribute events with ``lo < id <= hi``.

        Order is load-bearing (the #34 data-loss class): the caller reads
        ``hi = MAX(id)`` FIRST and it bounds the scan, so a row inserted mid-pass
        (id > hi) is left for the next cycle instead of being skipped by a
        cursor that jumps past it. Directory resolution (``resolve_dir`` stats/reads ``.git`` on disk,
        which can hang on cloud-offloaded folders) runs OUTSIDE ``self._lock``
        and outside any transaction; the lock is held only for short, chunked
        upsert+commit batches. Any failure rolls the open chunk back and
        propagates — the caller must then NOT advance the cursor.

        Two passes over the range:

        1. File edges (``via='file'``) from absolute ``file_path`` values, minus
           the shell-tool events whose ``file_path`` is the command itself
           (#58, ``_SHELL_TOOLS``). Each chunk first deletes the cwd edges of
           its sessions, in the same transaction — a session never counts both
           ways.
        2. cwd fallback (#40, every provider): each ``(session, provider)`` in
           the range with no file edge anywhere in the table gets one
           ``via='cwd'`` edge per distinct qualifying cwd of its events in the
           range, resolved on the directory ITSELF (``file_path`` = the cwd).

        The scan also folds each edge's TWO clocks (#42/#56): the earliest event
        (``event_ts``, for the rollup's work day) and the latest event clock per
        the #53 rule (``activity_ts`` — live rows: ``created_at``; historical:
        parsed event time). On a FULL pass (``lo == 0``) it additionally
        computes the shell-only keys — ``(session, provider, file_path)`` whose
        every event is a shell tool — and deletes their stale edges, the
        one-time cleanup of what earlier attribution runs minted (#58). The
        caller's next ``build_rollup`` reconciles ``session_touches``.

        ``replace_activity`` (only with ``lo == 0``, the #101 re-derivation)
        replaces each seen edge's ``activity_ts`` with the clock folded from
        ALL its events instead of keeping the later of the two.

        Every pass also records the LOCAL day of each event's #53 clock in
        ``edge_active_days`` (#114, ``INSERT OR IGNORE``): one row per
        (session, provider, workspace, via, day). ``day_tz`` is the zone the
        table is bucketed in (default: the recorded one, else the configured
        zone — ``_active_days_zone``); one ``local_day`` per event, no extra
        events.db read.

        ``events.db`` is opened ``mode=ro``; this never writes to it.
        """
        uri = f"file:{events_db_path}?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=5)
        rng, params = "id > ? AND id <= ?", (lo, hi)
        full_pass = lo == 0
        if replace_activity and not full_pass:
            raise ValueError("replace_activity needs a full pass (lo == 0)")
        if day_tz is CONFIGURED:
            day_tz, day_zone = self._active_days_zone()
        else:
            day_zone = zone_key(day_tz)
        # Per-edge clocks (#42/#56), folded in Python: timestamp formats are
        # mixed, so a SQL MIN()/MAX() over the raw strings would be wrong.
        # (session, provider, file_path) -> [cwd, event_ts, activity_ts, {day}]
        file_edges: dict[tuple[str, str, str], list] = {}
        # key -> does ANY event of this edge come from a NON-shell tool?
        edge_evidence: dict[tuple[str, str, str], bool] = {}
        # (session, provider, raw cwd) -> [event_ts, activity_ts, {day}]
        cwd_rows: dict[tuple[str, str, str], list] = {}
        # Local day of one event's #53 clock (#114): one tz conversion per
        # event (~1 µs), no extra read and no per-event cache (clocks are
        # nearly all distinct, a cache would only grow).
        def _local(dt: datetime | None) -> str | None:
            if dt is None:
                return None
            local = dt.astimezone(day_tz) if day_tz is not None else dt.astimezone()
            return local.date().isoformat()
        try:
            has_hist = "historical" in {
                r[1] for r in src.execute("PRAGMA table_info(events)")
            }
            hist_col = ", historical" if has_hist else ""
            for (session_id, provider, file_path, cwd, ts, created_at,
                 tool_name, *hist) in src.execute(
                f"""SELECT session_id, provider, file_path, cwd, timestamp,
                           created_at, tool_name{hist_col}
                    FROM events WHERE {rng} AND {self._ATTR_WHERE}""",
                params,
            ):
                key = (session_id, provider or "", file_path)
                if (tool_name or "") in self._SHELL_TOOLS:
                    # file_path is the command (#58): never an edge clock.
                    edge_evidence.setdefault(key, False)
                    continue
                historical = bool(hist[0]) if has_hist else False
                ets = _event_ts(ts, created_at)
                ats_dt = _edge_activity_dt(ts, created_at, historical)
                ats = ats_dt.isoformat(timespec="microseconds") if ats_dt else None
                day = _local(ats_dt)
                edge_evidence[key] = True
                cell = file_edges.get(key)
                if cell is None:
                    cell = file_edges[key] = [cwd, ets, ats, set()]
                else:
                    cell[1] = _min_ts(cell[1], ets)
                    cell[2] = _max_ts(cell[2], ats)
                if day:
                    cell[3].add(day)
            skipped = src.execute(
                f"""SELECT COUNT(*) FROM (
                        SELECT DISTINCT session_id, provider, file_path FROM events
                        WHERE {rng} AND {self._SKIPPED_WHERE}
                    )""",
                params,
            ).fetchone()[0]
            for session_id, provider, cwd, ts, created_at, *hist in src.execute(
                f"""SELECT session_id, provider, cwd, timestamp, created_at{hist_col}
                    FROM events
                    WHERE {rng} AND session_id IS NOT NULL AND session_id != ''
                      AND cwd IS NOT NULL AND cwd != ''""",
                params,
            ):
                key = (session_id, provider or "", cwd)
                historical = bool(hist[0]) if has_hist else False
                ets = _event_ts(ts, created_at)
                ats_dt = _edge_activity_dt(ts, created_at, historical)
                ats = ats_dt.isoformat(timespec="microseconds") if ats_dt else None
                day = _local(ats_dt)
                cell = cwd_rows.get(key)
                if cell is None:
                    cell = cwd_rows[key] = [ets, ats, set()]
                else:
                    cell[0] = _min_ts(cell[0], ets)
                    cell[1] = _max_ts(cell[1], ats)
                if day:
                    cell[2].add(day)
        finally:
            src.close()

        # Shell-only keys (#58): stale edges minted by earlier passes from a
        # command string. Only computed on a full pass — an incremental range
        # does not see the edge's whole history. An edge with ANY non-shell
        # event is real and survives.
        shell_only = (
            [k for k, real in edge_evidence.items() if not real]
            if full_pass else []
        )

        # Resolve identities with NO lock held and no open transaction.
        # An absolute file_path ignores cwd in _containing_dir, so keeping the
        # first row's cwd per edge changes nothing.
        dir_cache: dict[str, WorkspaceIdentity] = {}
        resolved: list[tuple[str, str, str, WorkspaceIdentity, str | None,
                             str | None, set[str]]] = []
        for (session_id, provider, file_path), (cwd, ets, ats, edays) in file_edges.items():
            container = _containing_dir(file_path, cwd)
            ident = dir_cache.get(container)
            if ident is None:
                ident = resolve_dir(container)
                dir_cache[container] = ident
            resolved.append((session_id, provider, file_path, ident, ets, ats,
                             edays))

        now = _now()
        if file_edges or cwd_rows:
            with self._lock:
                # First write records the zone the days are bucketed in (#114).
                self._conn.execute(
                    "INSERT OR IGNORE INTO rollup_meta (key, value) VALUES (?, ?)",
                    (_ACTIVE_DAYS_ZONE_KEY, day_zone),
                )
                self._conn.commit()
        attributed = 0
        for i in range(0, len(resolved), self._ATTRIBUTION_CHUNK):
            chunk = resolved[i:i + self._ATTRIBUTION_CHUNK]
            with self._lock:
                try:
                    for session_id, provider in {(r[0], r[1]) for r in chunk}:
                        self._conn.execute(
                            """DELETE FROM path_attributions
                               WHERE session_id = ? AND provider = ? AND via = 'cwd'""",
                            (session_id, provider),
                        )
                        self._conn.execute(
                            """DELETE FROM edge_active_days
                               WHERE session_id = ? AND provider = ? AND via = 'cwd'""",
                            (session_id, provider),
                        )
                    for session_id, provider, file_path, ident, ets, ats, edays in chunk:
                        wid = self._upsert_workspace_locked(ident, now)
                        self._record_attribution_locked(
                            session_id, provider, file_path, wid, ident.kind, now,
                            event_ts=ets, activity_ts=ats,
                            replace_activity=replace_activity,
                        )
                        self._record_active_days_locked(
                            session_id, provider, wid, "file", edays)
                    self._conn.commit()
                except BaseException:
                    # Never leave a half-written chunk open on the shared
                    # connection — the next unrelated commit would persist it.
                    self._conn.rollback()
                    raise
            attributed += len(chunk)

        cwd_attributed = self._attribute_cwd_fallback(
            cwd_rows, {(r[0], r[1]) for r in resolved}, dir_cache, now,
            replace_activity=replace_activity,
        )

        # #58 cleanup: drop the stale edges a full pass proved shell-only. Same
        # chunked rollback discipline as the upserts; the workspace rows are
        # left in place (they may still own filesystem touches — #46 owns
        # pruning orphans, this never deletes one).
        shell_removed = 0
        for i in range(0, len(shell_only), self._ATTRIBUTION_CHUNK):
            chunk = shell_only[i:i + self._ATTRIBUTION_CHUNK]
            with self._lock:
                try:
                    cur = self._conn.executemany(
                        """DELETE FROM path_attributions
                           WHERE session_id = ? AND provider = ? AND file_path = ?
                             AND via = 'file'""",
                        chunk,
                    )
                    shell_removed += cur.rowcount
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        with self._lock:
            workspace_count = self._conn.execute(
                "SELECT COUNT(*) FROM workspaces"
            ).fetchone()[0]

        return {
            "attributed": attributed,
            "cwd_attributed": cwd_attributed,
            "workspaces": workspace_count,
            "directories": len(dir_cache),
            "skipped_non_absolute": skipped,
            "shell_edges_removed": shell_removed,
        }

    def _attribute_cwd_fallback(
        self,
        cwd_rows: dict[tuple[str, str, str], list],
        file_sessions: set[tuple[str, str]],
        dir_cache: dict[str, WorkspaceIdentity],
        now: str,
        replace_activity: bool = False,
    ) -> int:
        """Pass 2 of :meth:`_attribute_event_range` — the ``via='cwd'`` edges.

        ``cwd_rows`` maps ``(session, provider, raw cwd)`` to its two clocks
        ``[earliest, latest]``; both are re-folded after ``normpath`` so spelling
        variants of one directory keep the same clocks (#42/#56).

        Same discipline as the file pass: the has-file-edge pre-check is one
        short lock hold, ``resolve_dir`` runs with no lock, and inserts commit
        in rolled-back-on-failure chunks. Returns the edges written.
        """
        home = os.path.normpath(os.path.expanduser("~"))
        # (session, provider) -> {normalized cwd: [event_ts, activity_ts]}
        wanted: dict[tuple[str, str], dict[str, list]] = {}
        for (session_id, provider, cwd), (ets, ats, *edays) in cwd_rows.items():
            edays = edays[0] if edays else set()
            key = (session_id, provider)
            if key in file_sessions or not self._cwd_qualifies(cwd, home):
                continue
            norm = path_module(cwd).normpath(cwd)
            cwds = wanted.setdefault(key, {})
            if norm in cwds:
                cwds[norm][0] = _min_ts(cwds[norm][0], ets)
                cwds[norm][1] = _max_ts(cwds[norm][1], ats)
                cwds[norm][2] |= edays
            else:
                cwds[norm] = [ets, ats, set(edays)]
        if not wanted:
            return 0

        with self._lock:
            for key in list(wanted):
                if self._conn.execute(
                    """SELECT 1 FROM path_attributions
                       WHERE session_id = ? AND provider = ? AND via = 'file'
                       LIMIT 1""",
                    key,
                ).fetchone():
                    del wanted[key]

        edges: list[tuple[str, str, str, WorkspaceIdentity, str | None, str | None,
                          set[str]]] = []
        for (session_id, provider), cwds in wanted.items():
            for cwd, (ets, ats, edays) in cwds.items():
                ident = dir_cache.get(cwd)
                if ident is None:
                    ident = resolve_dir(cwd)  # the directory itself, not its parent
                    dir_cache[cwd] = ident
                edges.append((session_id, provider, cwd, ident, ets, ats, edays))

        edge_sql = (self._CWD_EDGE_SQL_REPLACE if replace_activity
                    else self._CWD_EDGE_SQL)
        written = 0
        for i in range(0, len(edges), self._ATTRIBUTION_CHUNK):
            chunk = edges[i:i + self._ATTRIBUTION_CHUNK]
            with self._lock:
                try:
                    for session_id, provider, cwd, ident, ets, ats, edays in chunk:
                        # Re-check in-transaction before minting a workspace
                        # row, so a session that just gained a file edge
                        # leaves no orphan workspace behind.
                        if self._conn.execute(
                            """SELECT 1 FROM path_attributions
                               WHERE session_id = ? AND provider = ? AND via = 'file'
                               LIMIT 1""",
                            (session_id, provider),
                        ).fetchone():
                            continue
                        wid = self._upsert_workspace_locked(ident, now)
                        cur = self._conn.execute(
                            edge_sql,
                            (session_id, provider, cwd, wid, ident.kind, now, ets,
                             ats, session_id, provider),
                        )
                        written += cur.rowcount
                        if cur.rowcount:
                            self._record_active_days_locked(
                                session_id, provider, wid, "cwd", edays)
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
        return written

    def backfill_from_events(self, events_db_path: str | Path) -> dict[str, int]:
        """Populate attributions from ``events.db`` — a FULL read-only pass.

        Ignores the attribution cursor (re-reads every event up to the current
        ``MAX(id)``) and then sets ``cursor = hi``: this is the reset path the
        CLI ``mool workspace backfill`` keeps, e.g. when rows behind the cursor
        newly qualify for attribution. Idempotent — attributions upsert.

        Reads only absolute file paths; the count of skipped non-absolute rows
        is reported (no silent truncation). ``events.db`` is opened ``mode=ro``.
        """
        hi = self._read_max_event_id(events_db_path)
        result = self._attribute_event_range(events_db_path, 0, hi)
        self.set_attribution_cursor(hi)
        result["cursor"] = hi
        return result

    def attribute_incremental(self, events_db_path: str | Path) -> dict[str, Any]:
        """Attribute only events newer than the cursor (the daemon's pass, #39).

        Reads ``hi = MAX(id)`` first; if ``hi < cursor`` events.db was reset or
        replaced, so the cursor restarts at 0 (logged). Processes ``(cursor, hi]``
        and persists ``cursor = hi`` ONLY after its commits succeeded — on any
        exception the cursor stays put and the next call re-reads the range.
        A missing events.db is not an error: nothing to attribute yet.
        """
        cursor = self.get_attribution_cursor()
        empty = {
            "attributed": 0, "cwd_attributed": 0, "directories": 0,
            "skipped_non_absolute": 0, "shell_edges_removed": 0,
            "cursor_from": cursor, "cursor_to": cursor, "reset": False,
        }
        if not os.path.exists(str(events_db_path)):
            return empty

        hi = self._read_max_event_id(events_db_path)  # FIRST — bounds the pass
        # Both one-time re-derivations (#101 edge clocks, #114 days worked)
        # are the same full pass: whichever is due runs it, with one backup.
        replace = self._activity_rederive_ready(events_db_path)
        if replace or self._active_days_rederive_due():
            rederived = self._rederive_full(events_db_path, hi, replace)
            if rederived is not None:
                return rederived
        reset = False
        if hi < cursor:
            _log.warning(
                "events.db max id %d < attribution cursor %d — events.db was "
                "reset or replaced; restarting attribution from 0", hi, cursor,
            )
            cursor, reset = 0, True
        if hi == cursor and not reset:
            return empty

        result = self._attribute_event_range(events_db_path, cursor, hi)
        self.set_attribution_cursor(hi)  # only reached if every commit succeeded
        return {
            "attributed": result["attributed"],
            "cwd_attributed": result["cwd_attributed"],
            "directories": result["directories"],
            "skipped_non_absolute": result["skipped_non_absolute"],
            "shell_edges_removed": result["shell_edges_removed"],
            "cursor_from": cursor,
            "cursor_to": hi,
            "reset": reset,
        }

    def _active_days_rederive_due(self) -> bool:
        """True when ``edge_active_days`` must be (re-)derived (#114): its first
        derivation is pending, or ``[user] timezone`` changed since its days
        were bucketed."""
        with self._lock:
            rows = dict(self._conn.execute(
                "SELECT key, value FROM rollup_meta WHERE key IN (?, ?)",
                (_ACTIVE_DAYS_REDERIVE_KEY, _ACTIVE_DAYS_ZONE_KEY),
            ).fetchall())
        if rows.get(_ACTIVE_DAYS_REDERIVE_KEY) in ("pending", _REDERIVE_RUNNING):
            return True
        stored = rows.get(_ACTIVE_DAYS_ZONE_KEY)
        return stored is not None and stored != zone_key(user_timezone())

    def _rederive_full(
        self, events_db_path: str | Path, hi: int, replace_activity: bool,
    ) -> dict[str, Any] | None:
        """Run the pending one-time re-derivations: one full read-only pass
        over ``(0, hi]``, then ``cursor = hi`` and the markers cleared.

        * #114: ``edge_active_days`` is rebuilt in the CURRENT zone (its first
          derivation, or a changed ``[user] timezone``).
        * #101 (``replace_activity``): each seen edge's ``activity_ts`` is
          REPLACED by the clock folded from all its events.

        The days are always rebuilt — a #101 re-derivation moves clocks, and
        the days are bucketed on the same clocks. Backup of workspace.db first;
        if it fails nothing is touched and ``None`` is returned: the caller
        runs its normal pass (days keep the recorded zone) and this is retried
        next cycle. Otherwise the old days are cleared and the #114 marker set
        to ``running`` in ONE transaction, so a pass that fails midway is re-run
        from scratch next cycle without a new backup (#130; every insert is
        idempotent). Until it completes, readers fall back to ``activity_ts``
        for edges with no day rows. An edge none of whose events is still in events.db keeps its
        clock and gets no days (readers then date it by ``activity_ts``).
        """
        tz = user_timezone()
        zone = zone_key(tz)
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rollup_meta WHERE key = ?",
                (_ACTIVE_DAYS_REDERIVE_KEY,)).fetchone()
            resumed = bool(row) and row[0] == _REDERIVE_RUNNING
            try:
                # A resumed pass (#130): the first attempt already backed up
                # the state before anything was cleared; a new backup now would
                # only copy the cleared table and, retried enough, rotate the
                # useful one out.
                backup = None if resumed else backup_workspace_db(
                    self._conn, self.db_path)
            except (OSError, sqlite3.Error):
                _log.warning("atribución: no pude respaldar workspace.db; no "
                             "recalculo relojes ni días activos de las aristas "
                             "en esta pasada (#101, #114)", exc_info=True)
                return None
            before = (dict(self._conn.execute(
                "SELECT id, activity_ts FROM path_attributions").fetchall())
                if replace_activity else {})
            try:
                self._conn.execute("DELETE FROM edge_active_days")
                self._conn.execute(
                    "INSERT OR REPLACE INTO rollup_meta (key, value)"
                    " VALUES (?, ?)", (_ACTIVE_DAYS_REDERIVE_KEY, _REDERIVE_RUNNING))
                self._conn.execute(
                    "INSERT OR REPLACE INTO rollup_meta (key, value)"
                    " VALUES (?, ?)", (_ACTIVE_DAYS_ZONE_KEY, zone))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        what = ("relojes y días activos (#101, #114)" if replace_activity
                else "los días activos (#114)")
        if resumed:
            _log.info("atribución: retomo el recálculo de %s de las aristas sin "
                      "un respaldo nuevo; el del primer intento sigue vigente "
                      "(zona %r) (#130)", what, zone)
        else:
            _log.info("atribución: respaldo de workspace.db en %s antes de "
                      "recalcular %s de las aristas (zona %r)", backup, what, zone)
        result = self._attribute_event_range(events_db_path, 0, hi,
                                             replace_activity=replace_activity,
                                             day_tz=tz)
        self.set_attribution_cursor(hi)  # only reached if every commit succeeded
        with self._lock:
            self._conn.execute(
                "DELETE FROM rollup_meta WHERE key = ?", (_ACTIVE_DAYS_REDERIVE_KEY,))
            if replace_activity:
                self._conn.execute(
                    "DELETE FROM rollup_meta WHERE key = ?", (_ACTIVITY_REDERIVE_KEY,))
            self._conn.commit()
            after = (self._conn.execute(
                "SELECT id, activity_ts FROM path_attributions").fetchall()
                if replace_activity else [])
            edges, day_rows = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(n), 0) FROM ("
                " SELECT COUNT(*) AS n FROM edge_active_days"
                " GROUP BY session_id, provider, workspace_id, via)").fetchone()
        out: dict[str, Any] = {
            "attributed": result["attributed"],
            "cwd_attributed": result["cwd_attributed"],
            "directories": result["directories"],
            "skipped_non_absolute": result["skipped_non_absolute"],
            "shell_edges_removed": result["shell_edges_removed"],
            "cursor_from": 0,
            "cursor_to": hi,
            "reset": False,
            "active_days_edges": edges,
            "active_days": day_rows,
        }
        if replace_activity:
            redated = sum(1 for eid, ats in after
                          if eid in before and before[eid] != ats)
            _log.info("atribución: %d aristas cambiaron de día de actividad "
                      "(#101)", redated)
            out["rederived"] = redated
        _log.info("atribución: días activos recalculados — %d aristas, %d días "
                  "(zona %r) (#114)", edges, day_rows, zone)
        return out

    def _activity_rederive_ready(self, events_db_path: str | Path) -> bool:
        """True when the #101 re-derivation is pending AND events.db already
        recorded the migration it follows (read-only; any read error → wait)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rollup_meta WHERE key = ?",
                (_ACTIVITY_REDERIVE_KEY,),
            ).fetchone()
        if not row or row[0] != "pending":
            return False
        from hub.cache.event_store import OUTAGE_CLOCK_MIGRATION
        try:
            src = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True,
                                  timeout=5)
            try:
                done = src.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (OUTAGE_CLOCK_MIGRATION,),
                ).fetchone()
            finally:
                src.close()
        except sqlite3.Error:
            return False  # no schema_migrations yet: events.db not migrated
        return done is not None

    # --- Read surface ---

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                          w.first_seen, COUNT(a.id) AS attributions,
                   COUNT(CASE WHEN a.via = 'cwd' THEN NULL ELSE a.id END) AS files,
                   COUNT(CASE WHEN a.via = 'cwd' THEN a.id END) AS cwd_edges,
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
                # ``attributions`` = every edge; ``files`` excludes the
                # ``via='cwd'`` fallback edges, reported apart (#48).
                "attributions": r[6], "files": r[7], "cwd_edges": r[8],
                "sessions": r[9], "touches": r[10],
            }
            for r in rows
        ]

    def get_session_workspaces(
        self, session_id: str, provider: str | None = None
    ) -> list[dict[str, Any]]:
        """Which workspaces a session touched, with per-workspace file counts.

        ``files`` excludes ``via='cwd'`` edges; those are in ``cwd_edges`` (#48).
        """
        where = "a.session_id = ?"
        params: list[Any] = [session_id]
        if provider:
            where += " AND a.provider = ?"
            params.append(provider)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                           a.provider, COUNT(DISTINCT CASE WHEN a.via = 'cwd' THEN NULL ELSE a.file_path END) AS files,
                   COUNT(DISTINCT CASE WHEN a.via = 'cwd' THEN a.file_path END) AS cwd_edges
                    FROM path_attributions a
                    JOIN workspaces w ON w.id = a.workspace_id
                    WHERE {where}
                    GROUP BY w.id, a.provider
                    ORDER BY files DESC, cwd_edges DESC""",
                params,
            ).fetchall()
        return [
            {
                "workspace_key": r[0], "kind": r[1], "remote_url": r[2],
                "root_path": r[3], "dir_path": r[4], "provider": r[5],
                # ``files`` counts real file edges; ``via='cwd'`` fallback
                # edges are reported apart in ``cwd_edges`` (#48).
                "files": r[6], "cwd_edges": r[7],
            }
            for r in rows
        ]

    def get_workspace_sessions(self, workspace_key: str) -> list[dict[str, Any]]:
        """Which sessions touched a workspace, with per-session file counts."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT a.session_id, a.provider, COUNT(DISTINCT CASE WHEN a.via = 'cwd' THEN NULL ELSE a.file_path END) AS files,
                   COUNT(DISTINCT CASE WHEN a.via = 'cwd' THEN a.file_path END) AS cwd_edges
                   FROM path_attributions a
                   JOIN workspaces w ON w.id = a.workspace_id
                   WHERE w.workspace_key = ?
                   GROUP BY a.session_id, a.provider
                   ORDER BY files DESC, cwd_edges DESC""",
                (workspace_key,),
            ).fetchall()
        return [
            {"session_id": r[0], "provider": r[1], "files": r[2], "cwd_edges": r[3]}
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
                           MAX(r.last_activity) AS last_activity,
                           w.id AS workspace_id
                    FROM workspace_rollup r
                    JOIN workspaces w ON w.id = r.workspace_id
                    {where}
                    GROUP BY w.id
                    ORDER BY last_activity DESC""",
                params,
            ).fetchall()
            worked = _worked_by_workspace(self._conn, since)
        out = []
        for r in rows:
            d = _portfolio_row(r)
            d.update(_worked_fields(
                None if worked is None else worked.get(r[10], set()),
                d["active_days"]))
            out.append(d)
        return out

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

    # --- Portfolio classification (issue #24 — Stage 1, read-layer) ---

    @staticmethod
    def _read_session_cwds(events_db_path: str | Path) -> dict[str, str]:
        """Real per-session ``cwd`` from events.db (read-only).

        ``{session_id: cwd}`` for sessions with a usable absolute cwd. This is
        the PRIMARY collapse signal — a scratchpad folder embeds the session
        uuid, so its real project is recovered here with no decode. ``events.db``
        is opened ``mode=ro``; this never writes to it.
        """
        if not os.path.exists(str(events_db_path)):
            return {}
        try:
            src = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            return {}
        out: dict[str, str] = {}
        try:
            for sid, cwd in src.execute(
                "SELECT id, cwd FROM sessions "
                "WHERE cwd IS NOT NULL AND cwd != '' AND cwd LIKE '/%' AND cwd != '/'"
            ):
                if sid:
                    out[sid] = cwd
        except sqlite3.OperationalError:
            return {}
        finally:
            src.close()
        return out

    def classify_workspaces(
        self, events_db_path: str | Path | None = None,
        project_aliases: dict[str, str] | None = None,
        reference_containers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Classify every workspace into the #24 taxonomy (read-layer, additive).

        Collapses harness folders onto their real project, nests subdirs and
        materials under their anchor, de-prioritizes config dotfolders, and
        orphans degenerate/home-config roots. Rebuilds the whole
        ``workspace_classification`` table each pass (it is derived, not durable
        — see the schema note); ``events.db`` is read strictly read-only.

        ``project_aliases`` (decisión 5, #64p2) rewrites the canonical
        ``project_key`` of an origin folder (and everything under it) to its
        destination — the origin row disappears as its own project and its
        history/outcome/state fold onto the canonical one. ``reference_containers``
        (decisión 4c) mark owner-declared reference folders + children as never-
        projects (they surface in their own section). Both are read-layer only;
        the resolver is untouched and an alias to a missing destination only
        warns (``alias_warnings`` in the result), never fails.
        """
        from collections import Counter
        from hub.cache.portfolio_classifier import (
            classify, config_path, index_real_dir,
        )

        events_db_path = events_db_path or (self.db_path.parent / "events.db")
        home = os.path.expanduser("~")
        session_cwds = self._read_session_cwds(events_db_path)
        ref_paths = tuple(config_path(p) for p in (reference_containers or []))

        with self._lock:
            ws_rows = self._conn.execute(
                "SELECT id, kind, remote_url, root_path, dir_path, workspace_key "
                "FROM workspaces"
            ).fetchall()
            git_roots = [
                r[0] for r in self._conn.execute(
                    "SELECT root_path FROM workspaces "
                    "WHERE kind IN ('git_remote','git_root') AND root_path IS NOT NULL"
                ).fetchall()
            ]

        # Build the encode-match index outside the lock (disk-only, no DB).
        enc_index: dict[str, Any] = {}
        for cwd in set(session_cwds.values()):
            index_real_dir(cwd, enc_index)
        for rp in git_roots:
            index_real_dir(rp, enc_index)

        # classify() itself touches disk (resolve_dir / fs_decode walk real
        # dirs), so every row is computed here, outside the lock too.
        now = _now()
        classified: list[tuple[int, Any]] = []
        for wid, kind, remote_url, root_path, dir_path, _wkey in ws_rows:
            c = classify(
                kind, remote_url, root_path, dir_path,
                session_cwds=session_cwds, enc_index=enc_index, home=home,
                reference_paths=ref_paths,
            )
            classified.append((wid, c))

        # Aliases (decisión 5) rewrite the derived project_key BEFORE storage,
        # so every downstream read (grouped, states, outcome, production) sees
        # one canonical key — the resolver never learns about aliases.
        alias_warnings: list[str] = []
        if project_aliases:
            classified, alias_warnings = _rewrite_project_aliases(
                classified, ws_rows, project_aliases)
            for w in alias_warnings:
                _log.warning("project_aliases: %s", w)

        cat = Counter()
        via = Counter()
        role = Counter()
        for _wid, c in classified:
            cat[c.category] += 1
            via[c.resolved_via] += 1
            role[c.role] += 1

        with self._lock:
            conn = self._conn
            # DELETE + reinsert in ONE transaction: a WAL reader sees the old
            # classification or the new one, never an empty/partial table.
            try:
                conn.execute("DELETE FROM workspace_classification")
                conn.executemany(
                    """INSERT INTO workspace_classification
                           (workspace_id, category, subtype, role, project_key,
                            project_label, resolved_via, classified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (wid, c.category, c.subtype, c.role, c.project_key,
                         c.project_label, c.resolved_via, now)
                        for wid, c in classified
                    ],
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            projects = conn.execute(
                "SELECT COUNT(DISTINCT project_key) FROM workspace_classification "
                "WHERE role IN ('project','collapse','nest') AND project_key IS NOT NULL"
            ).fetchone()[0]

        return {
            "classified": len(ws_rows),
            "projects": projects,
            "by_category": dict(cat),
            "by_role": dict(role),
            "by_resolved_via": dict(via),
            "collapsed_harness": role.get("collapse", 0),
            "unclassified": role.get("orphan", 0),
            "alias_warnings": alias_warnings,
        }

    def get_portfolio_grouped(
        self, since: str | None = None,
        temporary_containers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Hierarchical portfolio: real projects with nested children + orphans.

        Folds harness (role=collapse) activity INTO its project at READ TIME —
        the ``workspace_rollup`` is never re-keyed (its schema forbids it). Each
        project carries its folded totals, a ``collapsed_harness`` count (so the
        collapsed folders read as grouped, never deleted), and its visible
        children (subdirs/materials/config, role=nest). Degenerate/home-config
        roots (role=orphan) go to a separate ``unclassified`` list.

        **Totals and state use ONE rule (#57):** the folded totals include
        ``nest`` children exactly like ``project`` and ``collapse`` — the
        derived state (``derive_project_states``) already reads every row with
        a ``project_key``, so a project whose activity lives in a subfolder no
        longer shows a state with 0 own sessions/days. No double count is
        possible: each ``path_attributions`` edge belongs to exactly one
        workspace (``UNIQUE(session_id, provider, file_path)``), and
        ``active_days`` is a UNION of days across the group's workspaces — a day
        shared by a project and its child counts once. A child keeps its own
        visible row under the project as well, by design.

        Returns raw labels; masking is applied by the MCP read wrapper. Empty
        structure when the classification table is absent (never classified).
        """
        day_where = ""
        params: list[Any] = []
        if since:
            day_where = "AND r.day >= ?"
            params.append(since[:10])

        with self._lock:
            conn = self._conn
            try:
                cls = conn.execute(
                    """SELECT c.workspace_id, c.role, c.category, c.subtype,
                              c.project_key, c.project_label, c.resolved_via,
                              w.workspace_key, w.kind, w.remote_url,
                              w.root_path, w.dir_path
                       FROM workspace_classification c
                       JOIN workspaces w ON w.id = c.workspace_id"""
                ).fetchall()
            except sqlite3.OperationalError:
                return {"projects": [], "unclassified": [], "reference": [],
                        "temporary": [], "summary": {}}
            # Per-workspace day rows (bounded); folded/aggregated in Python so
            # active_days is DISTINCT across a folded group, not a naive sum.
            roll = conn.execute(
                f"""SELECT r.workspace_id, r.day, r.session_touches,
                           r.fs_touches, r.git_touches, r.last_activity
                    FROM workspace_rollup r
                    WHERE 1=1 {day_where}""",
                params,
            ).fetchall()
            worked_by_wid = _worked_by_workspace(conn, since)

        roll_by_wid: dict[int, list[Any]] = {}
        for row in roll:
            roll_by_wid.setdefault(row[0], []).append(row)

        def _agg(wids: list[int]) -> dict[str, Any]:
            s = f = g = 0
            days: set[str] = set()
            last = ""
            for wid in wids:
                for _, day, sn, fn, gn, la in roll_by_wid.get(wid, []):
                    s += sn or 0
                    f += fn or 0
                    g += gn or 0
                    if day:
                        days.add(day)
                    if la and la > last:
                        last = la
            worked: set[tuple[str, str, str]] | None = None
            if worked_by_wid is not None:
                worked = set()
                for wid in wids:
                    worked |= worked_by_wid.get(wid, set())
            return {
                "session_touches": s, "fs_touches": f, "git_touches": g,
                "active_days": len(days), "last_activity": last or None,
                "sources": _lit_sources(s, f, g),
                # Days worked (#123): distinct sessions/days over the fold.
                **_worked_fields(worked, len(days)),
                # Internal: the DISTINCT day set (not just its count) so the
                # client rollup (#29) can UNION days across a client's projects
                # instead of summing counts (same discipline as active_days).
                # Stripped before the payload leaves ``_get_portfolio_grouped``.
                "_day_set": sorted(days),
                # Internal (#123): the (session, provider, day) set, so a
                # client unions sessions/days across its projects. None =
                # fallback (table unusable). Stripped like ``_day_set``.
                "_worked_set": None if worked is None else sorted(worked),
            }

        # Group projects by project_key; children (nest) held per project.
        groups: dict[str, dict[str, Any]] = {}
        children: dict[str, list[dict[str, Any]]] = {}
        orphans: list[dict[str, Any]] = []
        reference: list[dict[str, Any]] = []

        # Rung 4d (decisión 4d): the git repos that are DIRECT children of a
        # folder, keyed by that folder, so a gitless container with a strict
        # majority of one known org's repos can attribute to that client.
        from hub.cache.portfolio_classifier import org_of_remote
        child_orgs_by_parent: dict[str, list[str]] = {}
        for (_wid, _role, _cat, _sub, _pk, _pl, _via,
             _wk, kind, remote_url, root_path, _dir) in cls:
            if kind == "git_remote" and remote_url and root_path:
                org = org_of_remote(remote_url)
                if org:
                    child_orgs_by_parent.setdefault(
                        os.path.dirname(os.path.normpath(root_path)), []).append(org)

        def _path_missing(kind, remote_url, root_path, dir_path):
            """Ruta ya no existe (solo informativo). git_remote no depende de la
            ruta, así que nunca se marca (decisión 3, #36)."""
            if kind == "git_remote" or remote_url:
                return False
            p = root_path if kind == "git_root" else dir_path
            return bool(p) and not os.path.exists(p)

        for (wid, role, category, subtype, pkey, plabel, via,
             wkey, kind, remote_url, root_path, dir_path) in cls:
            if role == "orphan":
                o = _agg([wid])
                o.update({"workspace_key": wkey, "kind": kind,
                          "remote_url": remote_url, "root_path": root_path,
                          "dir_path": dir_path, "category": category,
                          "subtype": subtype,
                          "path_missing": _path_missing(
                              kind, remote_url, root_path, dir_path)})
                # "contenedores / referencia" (decisión 4c) is its own section,
                # next to externos — never the unclassified noise drawer.
                (reference if subtype == "reference" else orphans).append(o)
                continue
            if pkey is None:
                continue
            grp = groups.setdefault(pkey, {
                "project_key": pkey, "project_label": plabel,
                "_fold_wids": [], "collapsed_harness": 0,
                "_remote_url": None, "_anchor_path": None,
            })
            if not grp.get("project_label"):
                grp["project_label"] = plabel
            # Totals include EVERY role that shares the project_key — project,
            # collapsed harness and nest (#57), the same set the derived state
            # reads. Distinct workspaces, unique edges: no double count.
            grp["_fold_wids"].append(wid)
            if role == "collapse":
                grp["collapsed_harness"] += 1
            elif role == "project":
                # The real project row carries the evidence the client
                # ladder reads (#29): git remote (→ org) and the on-disk
                # anchor path (→ parent-folder client). Harness/collapse
                # rows hold scratchpad paths, so never source it from them.
                if grp["_remote_url"] is None:
                    grp["_remote_url"] = remote_url
                if grp["_anchor_path"] is None:
                    grp["_anchor_path"] = root_path or dir_path
            else:  # nest — folded into the totals AND shown as its own child
                child = _agg([wid])
                child.update({"workspace_key": wkey, "kind": kind,
                              "remote_url": remote_url, "root_path": root_path,
                              "dir_path": dir_path, "category": category,
                              "subtype": subtype})
                children.setdefault(pkey, []).append(child)

        projects: list[dict[str, Any]] = []
        for pkey, grp in groups.items():
            agg = _agg(grp["_fold_wids"])
            kids = sorted(
                children.get(pkey, []),
                key=lambda c: (c.get("last_activity") or ""), reverse=True,
            )
            anchor = grp["_anchor_path"]
            projects.append({
                "project_key": pkey,
                "project_label": grp["project_label"],
                **agg,
                "collapsed_harness": grp["collapsed_harness"],
                "children": kids,
                # Informative only (decisión 3, #36): the folder no longer exists
                # on disk. git_remote never depends on the path.
                "path_missing": bool(anchor) and not grp["_remote_url"]
                                and not os.path.exists(anchor),
                # Internal evidence for the client ladder (#29/#36-4d); stripped
                # before the payload leaves the read wrapper.
                "_remote_url": grp["_remote_url"],
                "_anchor_path": anchor,
                "_child_orgs": child_orgs_by_parent.get(
                    os.path.dirname(os.path.normpath(anchor or "")) or "\0", []),
            })

        projects.sort(
            key=lambda p: (p.get("last_activity") or "", p["session_touches"]
                           + p["fs_touches"] + p["git_touches"]),
            reverse=True,
        )
        orphans.sort(key=lambda o: (o.get("last_activity") or ""), reverse=True)
        reference.sort(key=lambda o: (o.get("last_activity") or ""), reverse=True)

        # Temporary containers (decisión 3): the DIRECT children move to their
        # own visible, collapsed "Temporal" section, keeping their state and
        # history — display grouping only, nothing is retracted.
        temporary: list[dict[str, Any]] = []
        if temporary_containers:
            from hub.cache.portfolio_classifier import config_path
            temp_paths = [config_path(p) for p in temporary_containers]

            def _is_direct_child(anchor: str | None) -> bool:
                if not anchor:
                    return False
                parent = os.path.dirname(config_path(anchor))
                return any(parent == tp for tp in temp_paths)

            kept = []
            for p in projects:
                if _is_direct_child(p.get("_anchor_path")):
                    temporary.append(p)
                else:
                    kept.append(p)
            projects = kept

        return {
            "projects": projects,
            "unclassified": orphans,
            "reference": reference,
            "temporary": temporary,
            "summary": {
                "projects": len(projects) + len(temporary),
                "collapsed_harness": sum(
                    p["collapsed_harness"] for p in projects + temporary),
                "children": sum(
                    len(p["children"]) for p in projects + temporary),
                "unclassified": len(orphans),
                "reference": len(reference),
                "temporary": len(temporary),
            },
        }
