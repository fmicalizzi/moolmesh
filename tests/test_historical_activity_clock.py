"""Session activity clock for imported history (issue #53).

Backfill / catch-up / ``--reparse`` (#45) insert old events with
``created_at = now`` and ``historical = 1``. The per-session activity clock
(``read_session_activity``) must date those rows by their EVENT time, while
live rows (``historical = 0``) keep the ingest clock (#18). Covered here: the
shared reader, derived project state, the production view, and session detail.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hub.cache.event_store import EventStore
from hub.cache.workspace_store import WorkspaceStore, read_session_activity
from hub.correlation.workspace_resolver import resolve_dir, resolve_path
from hub.mcp_server import _portfolio_production

UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _events_db(path: Path, rows: list[tuple], with_historical: bool = True) -> str:
    """rows: (session_id, provider, timestamp, created_at, historical)."""
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT,"
        " project TEXT, event_type TEXT, timestamp TEXT, summary TEXT,"
        " session_id TEXT, file_path TEXT, cwd TEXT, created_at REAL NOT NULL"
        + (", historical INTEGER NOT NULL DEFAULT 0)" if with_historical else ")")
    )
    for sid, prov, ts, ca, hist in rows:
        if with_historical:
            c.execute(
                "INSERT INTO events (provider, project, event_type, timestamp,"
                " summary, session_id, created_at, historical)"
                " VALUES (?, 'p', 'user', ?, '', ?, ?, ?)",
                (prov, ts, sid, ca, hist),
            )
        else:
            c.execute(
                "INSERT INTO events (provider, project, event_type, timestamp,"
                " summary, session_id, created_at) VALUES (?, 'p', 'user', ?, '', ?, ?)",
                (prov, ts, sid, ca),
            )
    c.commit()
    c.close()
    return str(path)


def _read(path: str) -> dict:
    c = sqlite3.connect(path)
    try:
        return read_session_activity(c)
    finally:
        c.close()


NOW = datetime.now(UTC)
AGO_60 = NOW - timedelta(days=60)


class TestReadSessionActivity:
    def test_historical_dated_by_event_time(self, tmp_path):
        ev = _events_db(tmp_path / "e.db", [
            ("h", "claude", _iso(AGO_60), NOW.timestamp(), 1),
            ("h", "claude", _iso(AGO_60 - timedelta(hours=2)), NOW.timestamp(), 1),
        ])
        got = _read(ev)[("h", "claude")]
        assert abs((got - AGO_60).total_seconds()) < 1

    def test_live_keeps_ingest_clock(self, tmp_path):
        # Resumed live session: months-old timestamp, recent ingest → ingest wins (#18).
        ca = NOW.timestamp() - 3600
        ev = _events_db(tmp_path / "e.db", [
            ("l", "claude", "2026-01-01T00:00:00Z", ca, 0),
        ])
        assert _read(ev)[("l", "claude")] == datetime.fromtimestamp(ca, tz=UTC)

    def test_mixed_takes_max_of_both_clocks(self, tmp_path):
        old_live = (NOW - timedelta(days=30)).timestamp()
        newer_hist = NOW - timedelta(days=10)
        ev = _events_db(tmp_path / "e.db", [
            # historical wins: event 10d ago beats a live ingest 30d ago
            ("m1", "claude", "2025-01-01T00:00:00Z", old_live, 0),
            ("m1", "claude", _iso(newer_hist), NOW.timestamp(), 1),
            # live wins: live ingest 1h ago beats a 60d-old historical event
            ("m2", "claude", "2025-01-01T00:00:00Z", NOW.timestamp() - 3600, 0),
            ("m2", "claude", _iso(AGO_60), NOW.timestamp(), 1),
        ])
        got = _read(ev)
        assert abs((got[("m1", "claude")] - newer_hist).total_seconds()) < 1
        assert got[("m2", "claude")] == datetime.fromtimestamp(
            NOW.timestamp() - 3600, tz=UTC)

    def test_unparseable_historical_falls_back_to_created_at(self, tmp_path):
        ca = NOW.timestamp() - 120
        ev = _events_db(tmp_path / "e.db", [("u", "claude", "garbage", ca, 1)])
        assert _read(ev)[("u", "claude")] == datetime.fromtimestamp(ca, tz=UTC)

    def test_numeric_epoch_timestamps(self, tmp_path):
        ev = _events_db(tmp_path / "e.db", [
            ("ms", "opencode", str(int(AGO_60.timestamp() * 1000)), NOW.timestamp(), 1),
            ("s", "opencode", str(int(AGO_60.timestamp())), NOW.timestamp(), 1),
        ])
        got = _read(ev)
        assert abs((got[("ms", "opencode")] - AGO_60).total_seconds()) < 1
        assert abs((got[("s", "opencode")] - AGO_60).total_seconds()) < 1

    def test_pre_45_db_without_historical_column(self, tmp_path):
        # No `historical` column → every row is live: old MAX(created_at) behavior,
        # never an empty map.
        ca = NOW.timestamp()
        ev = _events_db(tmp_path / "e.db",
                        [("x", "claude", _iso(AGO_60), ca, 0)],
                        with_historical=False)
        assert _read(ev)[("x", "claude")] == datetime.fromtimestamp(ca, tz=UTC)

    def test_session_filter_and_empty_ids(self, tmp_path):
        ev = _events_db(tmp_path / "e.db", [
            ("a", "claude", _iso(AGO_60), NOW.timestamp(), 1),
            ("b", "claude", _iso(AGO_60), NOW.timestamp(), 1),
            ("", "claude", _iso(NOW), NOW.timestamp(), 0),
        ])
        c = sqlite3.connect(ev)
        try:
            assert set(read_session_activity(c)) == {("a", "claude"), ("b", "claude")}
            assert set(read_session_activity(c, "a")) == {("a", "claude")}
        finally:
            c.close()


def _mkrepo(root: Path, name: str) -> Path:
    d = root / name
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = git@github.com:acme/{name}.git\n')
    return d


def _classified_store(tmp_path: Path, sessions: list[tuple[str, str]]):
    repo = _mkrepo(tmp_path, "R")
    s = WorkspaceStore(tmp_path / "workspace.db")
    for sid, prov in sessions:
        f = str(repo / f"{sid}.py")
        s.record_attribution(sid, prov, f, resolve_path(f))
    with s._lock:
        wid = s._conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key=?",
            (resolve_dir(str(repo)).key,),
        ).fetchone()[0]
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_classification
               (workspace_id, category, subtype, role, project_key,
                project_label, resolved_via, classified_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, "A", "project", "project", "proj:acme", "acme", "self",
             "2026-01-01T00:00:00"),
        )
        s._conn.commit()
    return s


class TestProjectState:
    def test_backfilled_old_session_is_not_activo(self, tmp_path):
        s = _classified_store(tmp_path, [("h", "claude")])
        ev = _events_db(tmp_path / "events.db", [
            ("h", "claude", _iso(AGO_60), time.time(), 1),   # imported "today"
        ])
        st = s.derive_project_states(ev, str(tmp_path / "nope.db"))
        s.close()
        assert st["proj:acme"]["state"] != "activo"
        assert st["proj:acme"]["age_days"] >= 59

    def test_live_session_today_is_activo(self, tmp_path):
        s = _classified_store(tmp_path, [("l", "claude")])
        ev = _events_db(tmp_path / "events.db", [
            ("l", "claude", "2026-01-01T00:00:00Z", time.time(), 0),
        ])
        st = s.derive_project_states(ev, str(tmp_path / "nope.db"))
        s.close()
        assert st["proj:acme"]["state"] == "activo"


def _epoch_local(y, m, d, h=12) -> float:
    return datetime(y, m, d, h).timestamp()


class TestProduction:
    def test_historical_session_counts_on_its_real_day(self, tmp_path):
        s = _classified_store(tmp_path, [("h", "claude"), ("l", "claude")])
        s.close()
        backfill_day = _epoch_local(2026, 9, 18, 9)
        real = datetime(2026, 9, 13, 15)  # local
        ev = _events_db(tmp_path / "events.db", [
            ("h", "claude", _iso(real.astimezone()), backfill_day, 1),
            ("l", "claude", "2026-01-01T00:00:00Z", _epoch_local(2026, 9, 18), 0),
        ])
        d = _portfolio_production(ev, str(tmp_path / "workspace.db"), days=7,
                                  today="2026-09-18", hide=False,
                                  github_db=str(tmp_path / "nope.db"))
        proj = next(p for p in d["projects"] if p["project_key"] == "proj:acme")
        assert proj["days"]["2026-09-13"] == {"claude": 1}
        assert proj["days"]["2026-09-18"] == {"claude": 1}   # only the live one
        assert proj["sessions"] == 2

    def test_sixty_day_old_history_imported_today(self, tmp_path):
        # DoD: a 60-day-old session imported "today" lands on its real day.
        s = _classified_store(tmp_path, [("h", "claude")])
        s.close()
        real = datetime(2026, 7, 20, 15)  # local, 60 days before 2026-09-18
        ev = _events_db(tmp_path / "events.db", [
            ("h", "claude", _iso(real.astimezone()), _epoch_local(2026, 9, 18), 1),
        ])
        d = _portfolio_production(ev, str(tmp_path / "workspace.db"), days=90,
                                  today="2026-09-18", hide=False,
                                  github_db=str(tmp_path / "nope.db"))
        proj = next(p for p in d["projects"] if p["project_key"] == "proj:acme")
        assert list(proj["days"]) == ["2026-07-20"]

    def test_backfilled_session_outside_window_is_dropped(self, tmp_path):
        s = _classified_store(tmp_path, [("h", "claude")])
        s.close()
        ev = _events_db(tmp_path / "events.db", [
            ("h", "claude", "2026-07-01T12:00:00.000Z", _epoch_local(2026, 9, 18), 1),
        ])
        d = _portfolio_production(ev, str(tmp_path / "workspace.db"), days=7,
                                  today="2026-09-18", hide=False,
                                  github_db=str(tmp_path / "nope.db"))
        assert d["projects"] == []


class TestSessionDetail:
    def test_last_activity_at_uses_event_time_for_history(self, tmp_path):
        store = EventStore(tmp_path / "events.db")
        try:
            ts = "2026-07-01T12:00:00.000Z"
            store.upsert_session(
                {"id": "h", "provider": "claude", "project": "p", "title": "t"}, ts)
            store.store_with_offset(
                [{"provider": "claude", "project": "p", "event_type": "user",
                  "timestamp": ts, "summary": "old", "session_id": "h"}],
                "fp", "claude", str(tmp_path / "h.jsonl"), 1, historical=True,
            )
            d = store.get_session_detail("h")
        finally:
            store.close()
        assert d["last_activity_at"] == "2026-07-01T12:00:00Z"
