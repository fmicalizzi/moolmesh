"""Per-edge activity clock (#56).

``path_attributions.activity_ts`` holds the LATEST event clock of each edge per
the #53 rule (live rows: ``created_at``; historical rows: parsed event time),
normalized to fixed-format UTC ISO. ``derive_project_states`` and the
production view date a project by ITS edges, not by the session's max clock
applied to every project the session touched. The workspace.db migration adds
the column and resets the attribution cursor once, so the daemon's next pass is
a full pass that fills every edge.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from hub.mcp_server import _portfolio_production
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc


def _utc(y, m, d, h=12, minute=0) -> float:
    return datetime(y, m, d, h, minute, tzinfo=UTC).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="microseconds")


def _local_epoch(y, m, d, h=12) -> float:
    """Local-time epoch (production buckets days in local time)."""
    return datetime(y, m, d, h, 0, 0).timestamp()


def _make_events_db(path: Path) -> Path:
    c = sqlite3.connect(str(path))
    c.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
        event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
        tokens_json TEXT, tool_name TEXT, file_path TEXT, model TEXT, cwd TEXT,
        fingerprint TEXT, created_at REAL NOT NULL,
        historical INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE sessions (
        id TEXT, provider TEXT, cwd TEXT, last_event_at TEXT, ended_at TEXT)""")
    c.commit()
    c.close()
    return path


def _add_event(db: Path, session: str, file_path: str | None = None,
               cwd: str | None = None, provider: str = "claude",
               tool_name: str = "Edit",
               timestamp: str = "2026-01-01T00:00:00Z",
               created_at: float | None = None, historical: int = 0) -> int:
    c = sqlite3.connect(str(db))
    cur = c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, tool_name, file_path, cwd, created_at, historical)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (provider, "p", "tool_use", timestamp, "s", session, tool_name,
         file_path, cwd, _utc(2026, 9, 26, 12) if created_at is None else created_at,
         historical),
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _rows(store: WorkspaceStore) -> dict[tuple[str, str], dict]:
    with store._lock:
        return {
            (sid, fp): {"via": via, "event_ts": ets, "activity_ts": ats}
            for sid, fp, via, ets, ats in store._conn.execute(
                "SELECT session_id, file_path, via, event_ts, activity_ts"
                " FROM path_attributions"
            ).fetchall()
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    events = _make_events_db(tmp_path / "events.db")
    repo = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
    plain = tmp_path / "plain"
    plain.mkdir()
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s, events, repo, plain, tmp_path
    s.close()


class TestEdgeActivityClock:
    def test_live_rows_use_created_at_and_keep_earliest_event_ts(self, env):
        """A live row's edge clock is its ingest epoch (#18); event_ts keeps the
        earliest parsed event time (#42). Both rules coexist on one edge."""
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        old = _utc(2026, 9, 1, 10)
        new = _utc(2026, 9, 10, 10)
        _add_event(events, "s1", f, timestamp="2026-01-05T10:00:00Z",
                   created_at=old)
        _add_event(events, "s1", f, timestamp="2026-01-06T10:00:00Z",
                   created_at=new)
        s.attribute_incremental(events)
        row = _rows(s)[("s1", f)]
        assert row["activity_ts"] == _iso(new)              # latest ingest clock
        assert row["event_ts"] == "2026-01-05T10:00:00.000000+00:00"  # earliest

    def test_historical_rows_use_the_parsed_event_time(self, env):
        """Imported history stored created_at = import time; the event time wins
        for activity too (#53 rule)."""
        s, events, repo, *_ = env
        f = str(repo / "b.py")
        _add_event(events, "s1", f, timestamp="2026-06-01T10:00:00Z",
                   created_at=_utc(2026, 9, 26, 12), historical=1)
        s.attribute_incremental(events)
        row = _rows(s)[("s1", f)]
        assert row["activity_ts"] == "2026-06-01T10:00:00.000000+00:00"

    def test_cwd_edge_carries_its_own_events_clock(self, env):
        s, events, _, plain, _ = env
        early = _utc(2026, 9, 11, 10)
        late = _utc(2026, 9, 14, 10)
        _add_event(events, "cx", None, cwd=str(plain), provider="codex",
                   timestamp="2026-09-11T10:00:00Z", created_at=early)
        _add_event(events, "cx", None, cwd=str(plain), provider="codex",
                   timestamp="2026-09-14T10:00:00Z", created_at=late)
        s.attribute_incremental(events)
        row = _rows(s)[("cx", str(plain))]
        assert row["via"] == "cwd"
        assert row["activity_ts"] == _iso(late)
        assert row["event_ts"] == "2026-09-11T10:00:00.000000+00:00"

    def test_activity_ts_only_advances_and_null_never_clobbers(self, env):
        s, _, repo, *_ = env
        wid = s.upsert_workspace(resolve_dir(str(repo)))
        f = str(repo / "a.py")
        now = "2026-09-17T00:00:00+00:00"

        def rec(ats):
            with s._lock:
                s._record_attribution_locked(
                    "s1", "claude", f, wid, "git_remote", now, activity_ts=ats)
                s._conn.commit()

        rec("2026-09-10T10:00:00.000000+00:00")
        rec("2026-09-05T10:00:00.000000+00:00")  # older never moves it back
        assert _rows(s)[("s1", f)]["activity_ts"] == (
            "2026-09-10T10:00:00.000000+00:00")
        rec(None)                                 # NULL never clobbers
        assert _rows(s)[("s1", f)]["activity_ts"] == (
            "2026-09-10T10:00:00.000000+00:00")
        rec("2026-09-15T10:00:00.000000+00:00")   # later advances
        assert _rows(s)[("s1", f)]["activity_ts"] == (
            "2026-09-15T10:00:00.000000+00:00")


class TestPerProjectState:
    def test_long_session_dates_each_project_by_its_own_edges(self, tmp_path,
                                                              monkeypatch):
        """The DoD case: one session touched A 20 days ago and B today. A must
        NOT read 'activo' off the session's max clock; B must."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        events = _make_events_db(tmp_path / "events.db")
        a = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        b = _mkrepo(tmp_path, "B", "git@github.com:acme/B.git")
        now = datetime(2026, 9, 26, 22, 0, tzinfo=UTC)
        _add_event(events, "s1", str(a / "old.py"),
                   timestamp="2026-01-01T00:00:00Z",
                   created_at=_utc(2026, 9, 6, 12))    # 20.4 days before now
        _add_event(events, "s1", str(b / "new.py"),
                   timestamp="2026-01-01T00:00:00Z",
                   created_at=_utc(2026, 9, 26, 12))   # today
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            s.attribute_incremental(events)
            s.classify_workspaces(events)
            st = s.derive_project_states(
                str(events), str(tmp_path / "nope.db"), now=now)
        finally:
            s.close()
        a_key = resolve_dir(str(a)).key
        b_key = resolve_dir(str(b)).key
        assert st[a_key]["state"] != "activo"
        assert st[a_key]["age_days"] > 19
        assert st[b_key]["state"] == "activo"
        assert st[b_key]["basis"] == "recent_activity"


class TestProductionEdgeDays:
    def test_session_counts_on_the_edge_day_per_project(self, tmp_path):
        """The DoD case in the production view: the session lands on A's old
        edge day and on B's own day — never both on the session's last day."""
        events = _make_events_db(tmp_path / "events.db")
        a = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        b = _mkrepo(tmp_path, "B", "git@github.com:acme/B.git")
        _add_event(events, "s1", str(a / "old.py"),
                   timestamp="2026-01-01T00:00:00Z",
                   created_at=_local_epoch(2026, 9, 5))
        _add_event(events, "s1", str(b / "new.py"),
                   timestamp="2026-01-01T00:00:00Z",
                   created_at=_local_epoch(2026, 9, 17))
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            s.attribute_incremental(events)
            s.build_rollup(str(tmp_path / "absent-github.db"))
            s.classify_workspaces(events)
        finally:
            s.close()
        data = _portfolio_production(
            str(events), str(tmp_path / "workspace.db"),
            days=30, today="2026-09-18", hide=False)
        by_key = {p["project_key"]: p for p in data["projects"]}
        a_row = by_key[resolve_dir(str(a)).key]
        b_row = by_key[resolve_dir(str(b)).key]
        assert a_row["sessions"] == 1 and a_row["days"] == {"2026-09-05": {"claude": 1}}
        assert b_row["sessions"] == 1 and b_row["days"] == {"2026-09-17": {"claude": 1}}
        # One session touching the same project several times that day counts
        # once per project-day, not per edge.
        assert a_row["active_days"] == 1 and b_row["active_days"] == 1

    def test_multiple_edges_same_session_project_day_count_once(self, tmp_path):
        events = _make_events_db(tmp_path / "events.db")
        a = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        day = _local_epoch(2026, 9, 17)
        _add_event(events, "s1", str(a / "one.py"),
                   created_at=day + 3600)
        _add_event(events, "s1", str(a / "two.py"),
                   created_at=day + 7200)
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            s.attribute_incremental(events)
            s.classify_workspaces(events)
        finally:
            s.close()
        data = _portfolio_production(
            str(events), str(tmp_path / "workspace.db"),
            days=30, today="2026-09-18", hide=False)
        row = next(p for p in data["projects"]
                   if p["project_key"] == resolve_dir(str(a)).key)
        assert row["sessions"] == 1
        assert row["days"] == {"2026-09-17": {"claude": 1}}


# A v1.23-era DB: path_attributions has ``via`` + ``event_ts``, migrations 1-2
# are recorded and the attribution cursor sits at N — no ``activity_ts`` column.
_PRE_56 = """
CREATE TABLE workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, remote_url TEXT,
    root_path TEXT, dir_path TEXT, first_seen TEXT NOT NULL
);
CREATE TABLE path_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resolved_via TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    via TEXT NOT NULL DEFAULT 'file',
    event_ts TEXT,
    UNIQUE(session_id, provider, file_path)
);
CREATE TABLE attribution_cursors (
    source TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES (1, 'attribution_via', 't');
INSERT INTO schema_migrations VALUES (2, 'attribution_event_ts', 't');
INSERT INTO workspaces VALUES (1, 'path_hash:abc', 'path_hash', NULL, NULL, '/x', 't');
INSERT INTO path_attributions
    (session_id, provider, file_path, workspace_id, resolved_via, first_seen, via, event_ts)
    VALUES ('s1', 'claude', '/x/a.py', 1, 'path_hash', '2026-09-20T00:00:00+00:00',
            'file', '2026-09-01T10:00:00.000000+00:00');
INSERT INTO attribution_cursors VALUES ('events', 42, 't');
"""


class TestActivityTsMigration:
    def test_adds_column_seeds_from_event_ts_and_resets_cursor_once(self, tmp_path):
        db = tmp_path / "workspace.db"
        c = sqlite3.connect(str(db))
        c.executescript(_PRE_56)
        c.close()

        s = WorkspaceStore(db)
        try:
            with s._lock:
                cols = {r[1] for r in s._conn.execute(
                    "PRAGMA table_info(path_attributions)")}
                applied = set(s._conn.execute(
                    "SELECT version, name FROM schema_migrations").fetchall())
                ats = s._conn.execute(
                    "SELECT activity_ts FROM path_attributions"
                ).fetchone()[0]
            assert "activity_ts" in cols
            assert (3, "attribution_activity_ts") in applied
            assert ats == "2026-09-01T10:00:00.000000+00:00"  # seeded from event_ts
            assert s.get_attribution_cursor() == 0            # full re-pass scheduled
            s.set_attribution_cursor(99)
        finally:
            s.close()

        s = WorkspaceStore(db)  # re-open: the reset does NOT run again
        try:
            assert s.get_attribution_cursor() == 99
        finally:
            s.close()

    def test_full_pass_advances_seed_to_the_true_latest_event(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        db = tmp_path / "workspace.db"
        c = sqlite3.connect(str(db))
        c.executescript(_PRE_56)
        c.close()
        events = _make_events_db(tmp_path / "events.db")
        _add_event(events, "s1", "/x/a.py", timestamp="2026-09-01T10:00:00Z",
                   created_at=_utc(2026, 9, 10, 10))
        _add_event(events, "s1", "/x/a.py", timestamp="2026-09-01T10:00:00Z",
                   created_at=_utc(2026, 9, 15, 10))

        s = WorkspaceStore(db)
        try:
            assert s.get_attribution_cursor() == 0       # migration reset
            s.attribute_incremental(events)              # full pass
            with s._lock:
                ats = s._conn.execute(
                    "SELECT activity_ts FROM path_attributions"
                ).fetchone()[0]
        finally:
            s.close()
        assert ats == _iso(_utc(2026, 9, 15, 10))
