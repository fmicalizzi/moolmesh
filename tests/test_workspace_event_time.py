"""The rollup dates session attributions by EVENT time (issue #42).

``path_attributions.event_ts`` holds the earliest event clock of each edge,
normalized to fixed-format UTC ISO. ``build_rollup`` buckets session activity on
``COALESCE(event_ts, first_seen)``, so a backlog attributed in one pass (upgrade,
backfill) no longer lands entirely on the attribution day. Migration 2 adds the
column and resets the attribution cursor once, so the first daemon cycle after an
upgrade re-derives the whole history.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

import hub.cache.workspace_store as store_mod
from hub.cache.workspace_store import WorkspaceStore, _event_ts
from hub.correlation.workspace_resolver import resolve_dir
from tests.test_workspace_attribution import _add_event, _make_events_db
from tests.test_workspace_store import _mkrepo


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


def _ets(store: WorkspaceStore) -> dict[tuple[str, str], str | None]:
    """(session, file_path) -> event_ts."""
    with store._lock:
        return {
            (sid, fp): ets for sid, fp, ets in store._conn.execute(
                "SELECT session_id, file_path, event_ts FROM path_attributions"
            ).fetchall()
        }


def _session_days(store: WorkspaceStore) -> dict[str, int]:
    with store._lock:
        return dict(store._conn.execute(
            """SELECT day, SUM(session_touches) FROM workspace_rollup
               WHERE session_touches > 0 GROUP BY day"""
        ).fetchall())


class TestEventTsFormat:
    def test_z_form(self):
        assert _event_ts("2026-09-26T03:34:31.831Z", 0) == "2026-09-26T03:34:31.831000+00:00"

    def test_local_offset_is_moved_to_the_utc_day(self):
        # OpenCode stores a local offset: substr(ts,1,10) would say 09-25.
        assert _event_ts("2026-09-25T18:37:02.129000-06:00", 0) == (
            "2026-09-26T00:37:02.129000+00:00")

    def test_zero_microseconds_keep_the_fixed_width(self):
        a = _event_ts("2026-09-26T03:34:31Z", 0)
        b = _event_ts("2026-09-26T03:34:31.000001Z", 0)
        assert a == "2026-09-26T03:34:31.000000+00:00"
        assert len(a) == len(b) and a < b

    @pytest.mark.parametrize("bad", ["", "   ", None, "not-a-date"])
    def test_unreadable_timestamp_falls_back_to_created_at(self, bad):
        epoch = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc).timestamp()
        assert _event_ts(bad, epoch) == "2026-09-12T05:00:00.000000+00:00"

    def test_no_clock_at_all_is_none(self):
        assert _event_ts("", None) is None


class TestAttributionCarriesEventTs:
    def test_file_edge_gets_earliest_event_of_the_range(self, env):
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        _add_event(events, "s1", f, timestamp="2026-09-12T10:00:00Z")
        _add_event(events, "s1", f, timestamp="2026-09-10T10:00:00Z")
        _add_event(events, "s1", f, timestamp="2026-09-11T10:00:00Z")
        s.attribute_incremental(events)
        assert _ets(s)[("s1", f)] == "2026-09-10T10:00:00.000000+00:00"

    def test_mixed_formats_compare_as_instants_not_strings(self, env):
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        # Lexicographically the offset form is "earlier"; as an instant it is later.
        _add_event(events, "s1", f, timestamp="2026-09-25T18:37:02.129000-06:00")
        _add_event(events, "s1", f, timestamp="2026-09-26T00:10:00Z")
        s.attribute_incremental(events)
        assert _ets(s)[("s1", f)] == "2026-09-26T00:10:00.000000+00:00"

    def test_empty_timestamp_uses_created_at(self, env):
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        epoch = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).timestamp()
        _add_event(events, "s1", f, timestamp="", created_at=epoch)
        s.attribute_incremental(events)
        assert _ets(s)[("s1", f)] == "2026-09-03T12:00:00.000000+00:00"

    def test_later_pass_never_moves_an_edge_later(self, env):
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        _add_event(events, "s1", f, timestamp="2026-09-10T10:00:00Z")
        s.attribute_incremental(events)
        _add_event(events, "s1", f, timestamp="2026-09-15T10:00:00Z")
        s.attribute_incremental(events)
        assert _ets(s)[("s1", f)] == "2026-09-10T10:00:00.000000+00:00"

    def test_earlier_event_with_higher_id_moves_it_earlier(self, env):
        s, events, repo, *_ = env
        f = str(repo / "a.py")
        _add_event(events, "s1", f, timestamp="2026-09-15T10:00:00Z")
        s.attribute_incremental(events)
        _add_event(events, "s1", f, timestamp="2026-09-10T10:00:00Z")  # late ingest
        s.attribute_incremental(events)
        assert _ets(s)[("s1", f)] == "2026-09-10T10:00:00.000000+00:00"

    def test_null_is_filled_and_never_clobbers(self, env):
        s, _, repo, *_ = env
        wid = s.upsert_workspace(resolve_dir(str(repo)))
        f = str(repo / "a.py")

        def rec(ets):
            with s._lock:
                s._record_attribution_locked("s1", "claude", f, wid, "git_remote",
                                             "2026-09-17T00:00:00+00:00", event_ts=ets)
                s._conn.commit()

        rec(None)
        assert _ets(s)[("s1", f)] is None
        rec("2026-09-10T10:00:00.000000+00:00")
        assert _ets(s)[("s1", f)] == "2026-09-10T10:00:00.000000+00:00"
        rec(None)
        assert _ets(s)[("s1", f)] == "2026-09-10T10:00:00.000000+00:00"

    def test_cwd_edge_gets_event_ts_across_spelling_variants(self, env):
        s, events, _, plain, _ = env
        _add_event(events, "cx", None, cwd=str(plain) + "/", provider="codex",
                   timestamp="2026-09-12T10:00:00Z")
        _add_event(events, "cx", None, cwd=str(plain), provider="codex",
                   timestamp="2026-09-11T10:00:00Z")
        s.attribute_incremental(events)
        assert _ets(s) == {("cx", str(plain)): "2026-09-11T10:00:00.000000+00:00"}

    def test_cwd_edge_keeps_earliest_on_a_later_pass(self, env):
        s, events, _, plain, _ = env
        _add_event(events, "cx", None, cwd=str(plain), provider="codex",
                   timestamp="2026-09-11T10:00:00Z")
        s.attribute_incremental(events)
        _add_event(events, "cx", None, cwd=str(plain), provider="codex",
                   timestamp="2026-09-14T10:00:00Z")
        s.attribute_incremental(events)
        assert _ets(s) == {("cx", str(plain)): "2026-09-11T10:00:00.000000+00:00"}


class TestRollupByEventDay:
    def test_backlog_spreads_over_event_days_not_the_pass_day(self, env, monkeypatch):
        s, events, repo, _, tmp = env
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-17T10:00:00+00:00")
        _add_event(events, "s1", str(repo / "a.py"), timestamp="2026-09-01T10:00:00Z")
        _add_event(events, "s2", str(repo / "b.py"), timestamp="2026-09-05T10:00:00Z")
        _add_event(events, "s3", str(repo / "c.py"),
                   timestamp="2026-09-05T20:00:00-06:00")  # UTC 09-06
        s.backfill_from_events(events)
        s.build_rollup(tmp / "absent-github.db")
        assert _session_days(s) == {"2026-09-01": 1, "2026-09-05": 1, "2026-09-06": 1}
        with s._lock:
            last = s._conn.execute(
                "SELECT MAX(last_activity) FROM workspace_rollup").fetchone()[0]
        assert last == "2026-09-06T02:00:00.000000+00:00"

    def test_null_event_ts_falls_back_to_first_seen(self, env):
        s, _, repo, _, tmp = env
        wid = s.upsert_workspace(resolve_dir(str(repo)))
        with s._lock:
            s._record_attribution_locked("s1", "claude", str(repo / "a.py"), wid,
                                         "git_remote", "2026-09-17T08:00:00+00:00")
            s._conn.commit()
        s.build_rollup(tmp / "absent-github.db")
        assert _session_days(s) == {"2026-09-17": 1}


# A v1.20/#40-era DB: path_attributions has ``via`` but no ``event_ts``, the
# attribution cursor sits at N, and migration 1 is already recorded.
_PRE_42 = """
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
    UNIQUE(session_id, provider, file_path)
);
CREATE TABLE attribution_cursors (
    source TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES (1, 'attribution_via', 't');
INSERT INTO attribution_cursors VALUES ('events', 42, 't');
"""


class TestEventTsMigration:
    def test_old_db_gets_column_and_cursor_reset_once(self, tmp_path):
        db = tmp_path / "workspace.db"
        c = sqlite3.connect(db)
        c.executescript(_PRE_42)
        c.close()

        s = WorkspaceStore(db)
        try:
            with s._lock:
                cols = {r[1] for r in s._conn.execute(
                    "PRAGMA table_info(path_attributions)")}
                applied = set(s._conn.execute(
                    "SELECT version, name FROM schema_migrations").fetchall())
            assert "event_ts" in cols
            assert (2, "attribution_event_ts") in applied
            assert s.get_attribution_cursor() == 0
            s.set_attribution_cursor(99)
        finally:
            s.close()

        s = WorkspaceStore(db)  # re-open: the reset does NOT run again
        try:
            assert s.get_attribution_cursor() == 99
        finally:
            s.close()

    def test_fresh_db_has_column_and_records_migration(self, tmp_path):
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            with s._lock:
                cols = {r[1] for r in s._conn.execute(
                    "PRAGMA table_info(path_attributions)")}
                applied = {r[0] for r in s._conn.execute(
                    "SELECT version FROM schema_migrations")}
            assert "event_ts" in cols and 2 in applied
            assert s.get_attribution_cursor() == 0
        finally:
            s.close()

    def test_upgrade_rederives_history_and_reconciles_old_day(self, tmp_path, monkeypatch):
        """End to end: edges written pre-#42 on the backfill day (09-17), with
        fs history on that same row. After the upgrade, ONE incremental pass
        (the reset cursor makes it full) plus a rollup moves the session count
        to the event days, zeroes 09-17's session count, and keeps its fs."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        events = _make_events_db(tmp_path / "events.db")
        repo = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
        gh = tmp_path / "absent-github.db"
        _add_event(events, "s1", str(repo / "a.py"), timestamp="2026-09-01T10:00:00Z")
        _add_event(events, "s2", str(repo / "b.py"), timestamp="2026-09-03T10:00:00Z")

        # Simulate v1.20: edges with first_seen = backfill day, no event_ts.
        db = tmp_path / "workspace.db"
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-17T10:00:00+00:00")
        s = WorkspaceStore(db)
        s.attribute_incremental(events)
        with s._lock:
            s._conn.execute("UPDATE path_attributions SET event_ts = NULL")
            s._conn.execute("DELETE FROM schema_migrations WHERE version = 2")
            s._conn.commit()
        s.build_rollup(gh)
        with s._lock:
            s._conn.execute(
                "UPDATE workspace_rollup SET fs_touches = 5 WHERE day = '2026-09-17'")
            s._conn.commit()
        assert _session_days(s) == {"2026-09-17": 2}
        assert s.get_attribution_cursor() > 0
        s.close()

        # Upgrade: re-open runs migration 2 (ALTER skipped, cursor reset).
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-26T10:00:00+00:00")
        s = WorkspaceStore(db)
        try:
            assert s.get_attribution_cursor() == 0
            s.attribute_incremental(events)
            r = s.build_rollup(gh)
            assert r["session_reconciled"] == 1
            assert _session_days(s) == {"2026-09-01": 1, "2026-09-03": 1}
            key = resolve_dir(str(repo)).key
            with s._lock:
                row = s._conn.execute(
                    """SELECT r.session_touches, r.fs_touches FROM workspace_rollup r
                       JOIN workspaces w ON w.id = r.workspace_id
                       WHERE w.workspace_key = ? AND r.day = '2026-09-17'""",
                    (key,),
                ).fetchone()
            assert row == (0, 5)
        finally:
            s.close()
