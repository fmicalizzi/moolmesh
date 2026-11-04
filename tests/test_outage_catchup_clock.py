"""Events caught up after an outage keep the ingest clock (#101).

Before #62 (v1.28.1) a catch-up after a daemon outage stored every caught-up
event LIVE with ``created_at`` = the catch-up day, so the #53 rule clocked the
whole outage on that one day and the production strip showed the outage days
empty. events.db migration 8 applies the #62 ingest rule to those rows (after a
backup); workspace.db migration 5 schedules one full attribution pass that
REPLACES each edge's ``activity_ts`` (after a backup), once events.db has
migration 8 recorded.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import hub.cache.event_store as es
import hub.cache.workspace_store as ws
from hub.cache.event_store import LIVE_WINDOW_HOURS, EventStore
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from hub.mcp_server import _portfolio_production
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc
D = datetime(2026, 9, 18, 12, 0)          # naive LOCAL noon of the catch-up day
TODAY = "2026-09-18"


def _day(k: int) -> str:
    return (D - timedelta(days=k)).strftime("%Y-%m-%d")


def _ts_formats(k: int) -> str:
    """Event time of day D-k in one of the formats events.db really holds."""
    utc = (D - timedelta(days=k)).astimezone(UTC)
    forms = [
        utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z",
        utc.isoformat(timespec="microseconds"),                       # +00:00
        (D - timedelta(days=k)).astimezone().isoformat(),             # local offset
        (D - timedelta(days=k)).isoformat(),                          # naive local
    ]
    return forms[k % len(forms)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
    events = tmp_path / "events.db"
    EventStore(db_path=events).close()     # current schema, all migrations
    return tmp_path, events, repo


def _insert(events, session, file_path, timestamp, created_at, historical=0):
    c = sqlite3.connect(events)
    c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, tool_name, file_path, cwd, fingerprint, created_at,"
        " historical) VALUES ('claude', 'p', 'tool_use', ?, 's', ?, 'Edit', ?,"
        " NULL, ?, ?, ?)",
        (timestamp, session, file_path, f"fp-{session}-{timestamp}", created_at,
         historical),
    )
    c.commit()
    c.close()


def _forget_migration(db, version):
    """Simulate a DB that predates ``version`` (it ran on an older release)."""
    c = sqlite3.connect(db)
    c.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
    c.commit()
    c.close()


def _outage(events, repo):
    """A synthetic outage: one session per day D-7..D-1, caught up LIVE on D
    (pre-#62), plus two rows that must stay live: a recent one inside the live
    window and one whose timestamp is unreadable."""
    caught_up_at = D.timestamp()
    for k in range(1, 8):
        _insert(events, f"out-{k}", str(repo / f"f{k}.py"), _ts_formats(k),
                caught_up_at)
    _insert(events, "recent", str(repo / "r.py"),
            (D - timedelta(hours=LIVE_WINDOW_HOURS - 1)).astimezone(UTC).isoformat(),
            caught_up_at)
    _insert(events, "garbled", str(repo / "g.py"), "not-a-time", caught_up_at)


def _historical(events):
    c = sqlite3.connect(events)
    try:
        return dict(c.execute("SELECT session_id, historical FROM events"))
    finally:
        c.close()


def _edges(store):
    with store._lock:
        return {(sid, prov, fp, wid): ats for sid, prov, fp, wid, ats in
                store._conn.execute(
                    "SELECT session_id, provider, file_path, workspace_id,"
                    " activity_ts FROM path_attributions")}


def _strip(tmp_path, events, repo):
    data = _portfolio_production(str(events), str(tmp_path / "workspace.db"),
                                 days=30, today=TODAY, hide=False, tz=None)
    row = next(p for p in data["projects"]
               if p["project_key"] == resolve_dir(str(repo)).key)
    return {day: sum(v.values()) for day, v in row["days"].items()}


def _pre_upgrade_workspace(tmp_path, events):
    """Attribute on the old clocks, then make workspace.db predate migration 5."""
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        s.attribute_incremental(events)
        s.classify_workspaces(events)
    finally:
        s.close()
    _forget_migration(tmp_path / "workspace.db", 5)


def _backups(tmp_path, prefix):
    return sorted((tmp_path / "backups").glob(f"{prefix}-*.db"))


class TestSyntheticOutage:
    def test_outage_days_move_back_to_their_own_days(self, env):
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _outage(events, repo)
        _pre_upgrade_workspace(tmp_path, events)

        # Before: the whole outage piles up on the catch-up day D.
        assert _strip(tmp_path, events, repo) == {TODAY: 9}

        EventStore(db_path=events).close()          # migration 8
        hist = _historical(events)
        assert all(hist[f"out-{k}"] == 1 for k in range(1, 8))
        assert hist["recent"] == 0                  # inside the live window
        assert hist["garbled"] == 0                 # unreadable: never guessed
        assert len(_backups(tmp_path, "events")) == 1

        s = WorkspaceStore(tmp_path / "workspace.db")   # migration 5: pending
        try:
            before = _edges(s)
            res = s.attribute_incremental(events)
            after = _edges(s)
            assert res["rederived"] == 7
            # Same edges, none duplicated or lost — only their clocks moved.
            assert set(after) == set(before) and len(after) == 9
            assert len(_backups(tmp_path, "workspace")) == 1
            # Done once: the next cycle is a plain incremental no-op.
            again = s.attribute_incremental(events)
            assert "rederived" not in again and again["attributed"] == 0
            assert _edges(s) == after
        finally:
            s.close()

        expected = {_day(k): 1 for k in range(1, 8)}
        expected[TODAY] = 2                          # recent + garbled stay on D
        assert _strip(tmp_path, events, repo) == expected

    def test_live_rows_inside_the_window_are_untouched(self, env):
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _insert(events, "edge", str(repo / "e.py"),
                (D - timedelta(hours=LIVE_WINDOW_HOURS) + timedelta(minutes=1))
                .astimezone(UTC).isoformat(), D.timestamp())
        _insert(events, "empty", str(repo / "x.py"), "", D.timestamp())
        EventStore(db_path=events).close()
        assert _historical(events) == {"edge": 0, "empty": 0}
        assert _backups(tmp_path, "events") == []    # nothing to rewrite, no copy


class TestEventsMigration8:
    def test_idempotent(self, env):
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _outage(events, repo)
        EventStore(db_path=events).close()
        first = _historical(events)
        EventStore(db_path=events).close()           # recorded: never reruns
        conn = sqlite3.connect(events)
        try:
            assert es._mig_8_live_rows_older_than_ingest(conn) is None
            conn.commit()
        finally:
            conn.close()
        assert _historical(events) == first
        assert len(_backups(tmp_path, "events")) == 1

    def test_backup_failure_defers_without_changes(self, env, monkeypatch):
        import hub.backfill as backfill
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _outage(events, repo)
        before = _historical(events)

        def _fail(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(backfill, "backup_events_db", _fail)
        EventStore(db_path=events).close()
        assert _historical(events) == before
        c = sqlite3.connect(events)
        assert c.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 8").fetchone() is None
        c.close()

        monkeypatch.undo()
        EventStore(db_path=events).close()           # retried on the next open
        assert _historical(events)["out-1"] == 1


class TestWorkspaceRederive:
    def test_waits_for_events_migration(self, env):
        """Pending re-derivation never runs on un-migrated events.db: the
        cycle is a normal incremental pass and the marker stays."""
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _outage(events, repo)
        _pre_upgrade_workspace(tmp_path, events)
        _forget_migration(events, 8)   # still pre-upgrade on the events side
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            res = s.attribute_incremental(events)
            assert "rederived" not in res
            assert s._activity_rederive_ready(events) is False
            with s._lock:
                assert s._conn.execute(
                    "SELECT value FROM rollup_meta WHERE key = ?",
                    (ws._ACTIVITY_REDERIVE_KEY,)).fetchone() == ("pending",)
        finally:
            s.close()
        assert _backups(tmp_path, "workspace") == []

    def test_backup_failure_rewrites_nothing_and_retries(self, env, monkeypatch):
        tmp_path, events, repo = env
        _forget_migration(events, 8)
        _outage(events, repo)
        _pre_upgrade_workspace(tmp_path, events)
        EventStore(db_path=events).close()           # migration 8

        def _fail(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(ws, "backup_workspace_db", _fail)
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            before = _edges(s)
            res = s.attribute_incremental(events)
            assert "rederived" not in res
            assert _edges(s) == before
            monkeypatch.undo()
            res = s.attribute_incremental(events)    # next cycle: runs
            assert res["rederived"] == 7
        finally:
            s.close()

    def test_fresh_workspace_db_schedules_nothing(self, env):
        tmp_path, *_ = env
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            with s._lock:
                assert s._conn.execute(
                    "SELECT 1 FROM rollup_meta WHERE key = ?",
                    (ws._ACTIVITY_REDERIVE_KEY,)).fetchone() is None
        finally:
            s.close()

    def test_replace_needs_a_full_pass(self, env):
        tmp_path, events, _ = env
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            with pytest.raises(ValueError):
                s._attribute_event_range(events, 5, 10, replace_activity=True)
        finally:
            s.close()
