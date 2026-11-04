"""Live rows of an outage catch-up count on their own local day (#131).

The daemon comes back just after midnight: the previous evening's events are
ingested live (each is under 12 h old), and used to be dated by ingest time,
so that evening counted on the restart day. Synthetic fixtures only (an
``acme`` repo, invented sessions), no network.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import hub.cache.workspace_store as ws
from hub.cache.event_store import LIVE_WINDOW_HOURS, EventStore
from hub.cache.workspace_store import WorkspaceStore, _edge_activity_dt
from tests.test_edge_active_days import _attribute, _backups, _db_rows, _meta, _row, _strip
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc
D, D2 = "2026-09-17", "2026-09-18"
RESTART = datetime(2026, 9, 18, 0, 10, tzinfo=UTC).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(ws, "user_timezone", lambda: UTC)
    repo = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
    events = tmp_path / "events.db"
    EventStore(db_path=events).close()
    return tmp_path, events, repo


def _insert_live(events, session, file_path, timestamp, created_at):
    c = sqlite3.connect(events)
    c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, tool_name, file_path, cwd, fingerprint, created_at,"
        " historical) VALUES ('claude', 'p', 'tool_use', ?, 's', ?, 'Edit', ?,"
        " NULL, ?, ?, 0)",
        (timestamp, session, file_path, f"fp-{session}-{timestamp}", created_at),
    )
    c.commit()
    c.close()


def _catchup(events, repo):
    """Two sessions work D from 18:00 to 23:50, all ingested live at D+1 00:10."""
    for hh in ("18:00", "21:30", "23:50"):
        _insert_live(events, "evening", str(repo / "a.py"), f"{D}T{hh}:00Z", RESTART)
    _insert_live(events, "late", str(repo / "b.py"), f"{D}T23:40:00Z", RESTART)


class TestClock:
    def test_window_matches_events_db(self):
        assert ws._LIVE_WINDOW == timedelta(hours=LIVE_WINDOW_HOURS)

    def test_recent_live_row_uses_its_own_time(self):
        dt = _edge_activity_dt(f"{D}T23:50:00Z", RESTART, False)
        assert dt == datetime(2026, 9, 17, 23, 50, tzinfo=UTC)

    def test_old_replay_keeps_ingest_time(self):
        """#18: a resumed session replaying a 3-month-old timestamp live."""
        dt = _edge_activity_dt("2026-06-17T10:00:00Z", RESTART, False)
        assert dt == datetime.fromtimestamp(RESTART, tz=UTC)

    def test_future_timestamp_keeps_ingest_time(self):
        dt = _edge_activity_dt(f"{D2}T05:00:00Z", RESTART, False)
        assert dt == datetime.fromtimestamp(RESTART, tz=UTC)

    def test_unreadable_or_empty_timestamp_keeps_ingest_time(self):
        for ts in ("", None, "not a time"):
            assert _edge_activity_dt(ts, RESTART, False) == datetime.fromtimestamp(
                RESTART, tz=UTC)

    def test_offset_and_epoch_formats(self):
        assert _edge_activity_dt(f"{D}T17:50:00-06:00", RESTART, False) == datetime(
            2026, 9, 17, 23, 50, tzinfo=UTC)
        epoch = datetime(2026, 9, 17, 22, 0, tzinfo=UTC).timestamp()
        assert _edge_activity_dt(str(epoch), RESTART, False) == datetime(
            2026, 9, 17, 22, 0, tzinfo=UTC)


class TestStrip:
    def test_catchup_counts_on_its_own_day(self, env):
        tmp_path, events, repo = env
        _catchup(events, repo)
        _attribute(tmp_path, events)
        row = _row(tmp_path, events, repo, today=D2)
        assert _strip(row) == {D: 2}
        assert row["sessions"] == 2

    def test_replay_still_counts_on_ingest_day(self, env):
        tmp_path, events, repo = env
        _insert_live(events, "resumed", str(repo / "a.py"),
                     "2026-06-17T10:00:00Z", RESTART)
        _attribute(tmp_path, events)
        assert _strip(_row(tmp_path, events, repo, today=D2)) == {D2: 1}


class TestRederive:
    def _as_pre_131(self, tmp_path):
        """workspace.db as 1.34.1 left it: catch-up bucketed on the ingest day."""
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("UPDATE edge_active_days SET day = ?", (D2,))
        c.execute("UPDATE path_attributions SET activity_ts = ?",
                  (datetime.fromtimestamp(RESTART, tz=UTC).isoformat(
                      timespec="microseconds"),))
        c.execute("DELETE FROM schema_migrations WHERE version = 7")
        c.commit()
        c.close()

    def test_existing_edges_are_redated_once_with_backup(self, env):
        tmp_path, events, repo = env
        _catchup(events, repo)
        _attribute(tmp_path, events)
        self._as_pre_131(tmp_path)
        assert _strip(_row(tmp_path, events, repo, today=D2)) == {D2: 2}
        n_backups = len(_backups(tmp_path))

        res = _attribute(tmp_path, events)       # reopen → migration 7 → re-derive
        assert res["active_days_edges"] == 2
        assert res["rederived"] == 2
        assert len(_backups(tmp_path)) == n_backups + 1
        assert _strip(_row(tmp_path, events, repo, today=D2)) == {D: 2}
        meta = _meta(tmp_path)
        assert "active_days_rederive" not in meta
        assert "activity_rederive" not in meta
        ats = {r[0] for r in _db_rows(tmp_path,
                                      "SELECT activity_ts FROM path_attributions")}
        assert all(a.startswith(D) for a in ats)

        _attribute(tmp_path, events)             # second cycle: no-op
        assert len(_backups(tmp_path)) == n_backups + 1

    def test_fresh_db_schedules_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        s.close()
        assert _meta(tmp_path).get("active_days_rederive") is None
        assert _meta(tmp_path).get("activity_rederive") is None
