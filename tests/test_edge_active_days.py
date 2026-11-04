"""A session counts on every local day it worked a project (#114, #101 cause 2).

Synthetic fixtures only (an ``acme`` repo, invented sessions), no network.
Events are inserted as HISTORICAL rows so their #53 clock is the event time
itself and the local day is exact.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

import hub.cache.workspace_store as ws
from hub.cache.event_store import EventStore
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from hub.mcp_server import _portfolio_production
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc
TOKYO = ZoneInfo("Asia/Tokyo")
D1, D = "2026-09-17", "2026-09-18"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(ws, "user_timezone", lambda: UTC)
    repo = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
    events = tmp_path / "events.db"
    EventStore(db_path=events).close()
    return tmp_path, events, repo


def _insert(events, session, file_path, timestamp, cwd=None, tool="Edit"):
    c = sqlite3.connect(events)
    c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, tool_name, file_path, cwd, fingerprint, created_at,"
        " historical) VALUES ('claude', 'p', 'tool_use', ?, 's', ?, ?, ?, ?,"
        " ?, 1800000000, 1)",
        (timestamp, session, tool, file_path, cwd,
         f"fp-{session}-{file_path}-{timestamp}"),
    )
    c.commit()
    c.close()


def _two_day_session(events, repo):
    """``long`` works the repo on D-1 and D (two files on D); ``short`` only on D."""
    _insert(events, "long", str(repo / "a.py"), f"{D1}T10:00:00Z")
    _insert(events, "long", str(repo / "a.py"), f"{D}T09:00:00Z")
    _insert(events, "long", str(repo / "b.py"), f"{D}T11:00:00Z")
    _insert(events, "short", str(repo / "c.py"), f"{D}T15:00:00Z")


def _attribute(tmp_path, events):
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        res = s.attribute_incremental(events)
        s.classify_workspaces(events)
        return res
    finally:
        s.close()


def _row(tmp_path, events, repo, days=30, tz=UTC, today=D):
    data = _portfolio_production(str(events), str(tmp_path / "workspace.db"),
                                 days=days, today=today, hide=False, tz=tz)
    key = resolve_dir(str(repo)).key
    return next(p for p in data["projects"] if p["project_key"] == key)


def _strip(row):
    return {day: sum(v.values()) for day, v in row["days"].items()}


def _db_rows(tmp_path, sql, params=()):
    c = sqlite3.connect(tmp_path / "workspace.db")
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _meta(tmp_path):
    return dict(_db_rows(tmp_path, "SELECT key, value FROM rollup_meta"))


def _backups(tmp_path):
    return sorted((tmp_path / "backups").glob("workspace-*.db"))


def _make_pre_114(tmp_path):
    """Make workspace.db look like it ran on v1.31.x: no days, no migration 6."""
    c = sqlite3.connect(tmp_path / "workspace.db")
    c.execute("DELETE FROM edge_active_days")
    c.execute("DELETE FROM rollup_meta WHERE key LIKE 'active_days%'")
    c.execute("DELETE FROM schema_migrations WHERE version = 6")
    c.commit()
    c.close()


class TestDaysWorked:
    def test_session_counts_on_both_days_and_once_in_the_range(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        row = _row(tmp_path, events, repo)
        assert _strip(row) == {D1: 1, D: 2}       # distinct sessions per day
        assert row["sessions"] == 2                # never the sum of the days (3)
        assert row["active_days"] == 2
        assert row["last_day"] == D

    def test_one_day_window_counts_only_that_day(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        row = _row(tmp_path, events, repo, days=1)
        assert _strip(row) == {D: 2}
        assert row["sessions"] == 2 and row["active_days"] == 1

    def test_only_the_session_alone_is_one_in_total(self, env):
        tmp_path, events, repo = env
        _insert(events, "long", str(repo / "a.py"), f"{D1}T10:00:00Z")
        _insert(events, "long", str(repo / "a.py"), f"{D}T09:00:00Z")
        _attribute(tmp_path, events)
        row = _row(tmp_path, events, repo)
        assert _strip(row) == {D1: 1, D: 1}
        assert row["sessions"] == 1

    def test_incremental_pass_adds_a_new_day(self, env):
        tmp_path, events, repo = env
        _insert(events, "long", str(repo / "a.py"), f"{D1}T10:00:00Z")
        _attribute(tmp_path, events)
        assert _strip(_row(tmp_path, events, repo)) == {D1: 1}
        _insert(events, "long", str(repo / "a.py"), f"{D}T10:00:00Z")
        _attribute(tmp_path, events)
        row = _row(tmp_path, events, repo)
        assert _strip(row) == {D1: 1, D: 1} and row["sessions"] == 1

    def test_one_row_per_workspace_day_not_per_file(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        rows = _db_rows(tmp_path, "SELECT session_id, day, via FROM edge_active_days"
                        " ORDER BY session_id, day")
        assert rows == [("long", D1, "file"), ("long", D, "file"),
                        ("short", D, "file")]

    def test_cwd_days_leave_when_the_session_gains_a_file_edge(self, env):
        tmp_path, events, repo = env
        _insert(events, "s", None, f"{D1}T10:00:00Z", cwd=str(repo), tool="Read")
        _attribute(tmp_path, events)
        assert _db_rows(tmp_path, "SELECT via, day FROM edge_active_days") == [
            ("cwd", D1)]
        _insert(events, "s", str(repo / "a.py"), f"{D}T10:00:00Z")
        _attribute(tmp_path, events)
        assert _db_rows(tmp_path, "SELECT via, day FROM edge_active_days") == [
            ("file", D)]
        assert _strip(_row(tmp_path, events, repo)) == {D: 1}

    def test_edge_without_days_falls_back_to_activity_ts(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("DELETE FROM edge_active_days WHERE session_id = 'long'")
        c.commit()
        c.close()
        # Only the edges' latest events remain visible for ``long``: D.
        assert _strip(_row(tmp_path, events, repo)) == {D: 2}


class TestInitialDerivation:
    def test_upgrade_derives_days_with_a_backup(self, env, caplog):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        _make_pre_114(tmp_path)
        backups = len(_backups(tmp_path))

        s = WorkspaceStore(tmp_path / "workspace.db")       # migration 6
        try:
            assert _meta(tmp_path)["active_days_rederive"] == "pending"
            assert _db_rows(tmp_path, "SELECT COUNT(*) FROM edge_active_days") == [(0,)]
            with caplog.at_level(logging.INFO, logger="hub"):
                res = s.attribute_incremental(events)
        finally:
            s.close()
        assert len(_backups(tmp_path)) == backups + 1
        assert res["active_days_edges"] == 2 and res["active_days"] == 3
        assert any("2 aristas, 3 días" in r.getMessage() for r in caplog.records)
        meta = _meta(tmp_path)
        assert "active_days_rederive" not in meta
        assert meta["active_days_zone"] == "UTC"
        assert _strip(_row(tmp_path, events, repo)) == {D1: 1, D: 2}

    def test_idempotent(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        _make_pre_114(tmp_path)
        _attribute(tmp_path, events)
        rows = _db_rows(tmp_path, "SELECT * FROM edge_active_days ORDER BY 1, 5")
        backups = len(_backups(tmp_path))
        res = _attribute(tmp_path, events)                  # nothing pending
        assert "active_days" not in res
        assert len(_backups(tmp_path)) == backups
        assert _db_rows(tmp_path, "SELECT * FROM edge_active_days ORDER BY 1, 5") == rows
        WorkspaceStore(tmp_path / "workspace.db").close()   # migration runs once
        assert "active_days_rederive" not in _meta(tmp_path)

    def test_fresh_db_schedules_nothing(self, env):
        tmp_path, events, repo = env
        WorkspaceStore(tmp_path / "workspace.db").close()
        assert "active_days_rederive" not in _meta(tmp_path)
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        assert _backups(tmp_path) == []
        assert _meta(tmp_path)["active_days_zone"] == "UTC"

    def test_backup_failure_rewrites_nothing_and_retries(self, env, monkeypatch):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        _make_pre_114(tmp_path)
        WorkspaceStore(tmp_path / "workspace.db").close()

        def _fail(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(ws, "backup_workspace_db", _fail)
        res = _attribute(tmp_path, events)
        assert "active_days" not in res
        assert _meta(tmp_path)["active_days_rederive"] == "pending"
        assert _db_rows(tmp_path, "SELECT COUNT(*) FROM edge_active_days") == [(0,)]

        monkeypatch.undo()
        monkeypatch.setattr(ws, "user_timezone", lambda: UTC)
        res = _attribute(tmp_path, events)                  # retried next cycle
        assert res["active_days"] == 3
        assert "active_days_rederive" not in _meta(tmp_path)

    def test_failed_pass_is_resumed_next_cycle(self, env, monkeypatch):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _attribute(tmp_path, events)
        _make_pre_114(tmp_path)
        WorkspaceStore(tmp_path / "workspace.db").close()

        real = WorkspaceStore._attribute_event_range

        def _boom(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", _boom)
        with pytest.raises(sqlite3.OperationalError):
            _attribute(tmp_path, events)
        assert _meta(tmp_path)["active_days_rederive"] == "pending"

        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", real)
        res = _attribute(tmp_path, events)
        assert res["active_days"] == 3
        assert "active_days_rederive" not in _meta(tmp_path)


class TestZoneChange:
    """23:30 UTC on D-1 is already D in Tokyo (+9)."""

    def _late_session(self, events, repo):
        _insert(events, "late", str(repo / "a.py"), f"{D1}T23:30:00Z")

    def test_changed_zone_rederives_with_a_backup(self, env, monkeypatch):
        tmp_path, events, repo = env
        self._late_session(events, repo)
        _attribute(tmp_path, events)
        assert _db_rows(tmp_path, "SELECT day FROM edge_active_days") == [(D1,)]
        assert _backups(tmp_path) == []

        monkeypatch.setattr(ws, "user_timezone", lambda: TOKYO)
        res = _attribute(tmp_path, events)
        assert res["active_days"] == 1
        assert len(_backups(tmp_path)) == 1
        assert _db_rows(tmp_path, "SELECT day FROM edge_active_days") == [(D,)]
        assert _meta(tmp_path)["active_days_zone"] == "Asia/Tokyo"
        assert _strip(_row(tmp_path, events, repo, tz=TOKYO)) == {D: 1}
        _attribute(tmp_path, events)                        # once, not every pass
        assert len(_backups(tmp_path)) == 1

    def test_reading_another_zone_falls_back_to_activity_ts(self, env):
        tmp_path, events, repo = env
        self._late_session(events, repo)
        _attribute(tmp_path, events)                        # bucketed in UTC
        assert _strip(_row(tmp_path, events, repo, tz=TOKYO)) == {D: 1}

    def test_backup_failure_keeps_the_old_zone(self, env, monkeypatch):
        tmp_path, events, repo = env
        self._late_session(events, repo)
        _attribute(tmp_path, events)
        monkeypatch.setattr(ws, "user_timezone", lambda: TOKYO)

        def _fail(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(ws, "backup_workspace_db", _fail)
        _insert(events, "late", str(repo / "a.py"), f"{D}T23:30:00Z")
        _attribute(tmp_path, events)
        # New days are still bucketed in the recorded zone, never mixed.
        assert _meta(tmp_path)["active_days_zone"] == "UTC"
        assert _db_rows(tmp_path, "SELECT day FROM edge_active_days ORDER BY day") == [
            (D1,), (D,)]
