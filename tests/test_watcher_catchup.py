"""Tests for the daemon's startup catch-up after an outage (issue #45)."""

from __future__ import annotations

import collections
import json
import os
import sqlite3
import time
from pathlib import Path

from hub.cache.event_store import EventStore
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.opencode_watcher import OpenCodeWatcher

DAY = 86400


def _claude_file(base: Path, sid: str, age: float) -> Path:
    proj = base / "-Users-test-alpha"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps({
        "type": "user", "sessionId": sid, "cwd": "/Users/test/alpha",
        "uuid": f"{sid}-{i}", "timestamp": f"2026-03-15T10:0{i}:00.000Z",
        "message": {"role": "user", "content": f"msg {i} {sid}"},
    }) for i in range(2)) + "\n")
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def _setup(tmp_path, last_cycle_age: float | None):
    base = tmp_path / "claude"
    base.mkdir()
    store = EventStore(db_path=tmp_path / "events.db")
    if last_cycle_age is not None:
        store.set_watcher_cycle("claude", time.time() - last_cycle_age)
    sse: collections.deque = collections.deque()
    w = ClaudeWatcher(store, sse, claude_base=base)
    return base, store, sse, w


def _sessions(store) -> set[str]:
    return {r[0] for r in store._conn.execute("SELECT DISTINCT session_id FROM events")}


class TestCatchupCutoff:
    def test_stale_heartbeat_widens_the_first_pass(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        _claude_file(base, "during-outage", age=2 * DAY)
        _claude_file(base, "before-outage", age=5 * DAY)
        w._plan_catchup()
        assert [p.stem for p in w._catchup_queue] == ["during-outage"]
        w._running = True
        w._drain_catchup(100)
        assert _sessions(store) == {"during-outage"}
        store.close()

    def test_cutoff_is_capped(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=90 * DAY)
        _claude_file(base, "day-20", age=20 * DAY)
        _claude_file(base, "day-40", age=40 * DAY)
        cutoff = w._catchup_cutoff()
        assert abs(cutoff - (time.time() - w.CATCHUP_MAX_DAYS * DAY)) < 5
        w._plan_catchup()
        assert [p.stem for p in w._catchup_queue] == ["day-20"]
        store.close()

    def test_no_heartbeat_means_no_catchup(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=None)
        _claude_file(base, "old", age=2 * DAY)
        assert w._catchup_cutoff() is None
        w._plan_catchup()
        assert not w._catchup_queue
        store.close()

    def test_recent_heartbeat_means_no_catchup(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3600)
        _claude_file(base, "old", age=2 * DAY)
        assert w._catchup_cutoff() is None
        store.close()

    def test_second_pass_returns_to_the_live_window(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        _claude_file(base, "during-outage", age=2 * DAY)
        w._plan_catchup()
        w._running = True
        w._drain_catchup(100)
        # Queue drained → heartbeat is fresh → no further widening.
        assert time.time() - store.get_watcher_cycle("claude") < 5
        assert w._catchup_cutoff() is None
        w._rescan()
        assert w.watched_count == 0  # the 2-day-old file is not live-watched
        store.close()

    def test_catchup_events_never_reach_sse_but_live_ones_do(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        _claude_file(base, "during-outage", age=2 * DAY)
        _claude_file(base, "live", age=600)
        w._plan_catchup()
        w._running = True
        w._drain_catchup(100)
        assert len(sse) == 0
        w._rescan()
        for path, fp in list(w._watched_files.items()):
            w._harvest_file(path, fp)
        assert {e["session_id"] for e in sse} == {"live"}
        assert _sessions(store) == {"during-outage", "live"}
        store.close()

    def test_heartbeat_holds_while_catchup_is_pending(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        for i in range(3):
            _claude_file(base, f"s{i}", age=2 * DAY - i)
        before = store.get_watcher_cycle("claude")
        w._plan_catchup()
        w._running = True
        w._drain_catchup(1)
        w._rescan()
        assert store.get_watcher_cycle("claude") == before  # 2 files still queued
        w._drain_catchup(10)
        assert store.get_watcher_cycle("claude") > before
        store.close()

    def test_cloud_placeholder_is_skipped(self, tmp_path, monkeypatch):
        import hub.cloudfiles as cloudfiles
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        cloud = _claude_file(base, "in-cloud", age=2 * DAY)
        monkeypatch.setattr(cloudfiles, "is_cloud_placeholder", lambda p: Path(p) == cloud)
        w._plan_catchup()
        w._running = True
        w._drain_catchup(10)
        assert _sessions(store) == set()
        assert w.catchup_skipped == [("nube", cloud)]
        store.close()

    def test_loop_runs_catchup_in_the_watcher_thread(self, tmp_path):
        base, store, sse, w = _setup(tmp_path, last_cycle_age=3 * DAY)
        _claude_file(base, "during-outage", age=2 * DAY)
        w.POLL_INTERVAL = 0.05
        w.start()
        deadline = time.time() + 5
        while time.time() < deadline and not _sessions(store):
            time.sleep(0.05)
        w.stop()
        assert _sessions(store) == {"during-outage"}
        assert len(sse) == 0
        store.close()

    def test_sqlite_providers_do_not_catch_up(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        store.set_watcher_cycle("opencode", time.time() - 3 * DAY)
        w = OpenCodeWatcher(store, None)
        assert w.CATCHUP is False
        w._record_cycle()  # no-op, no row written for opencode
        assert store.get_watcher_cycle("opencode") < time.time() - 2 * DAY
        store.close()


class TestWatcherStateMigration:
    def test_migration_creates_table_once(self, tmp_path):
        db = tmp_path / "events.db"
        EventStore(db_path=db).close()
        EventStore(db_path=db).close()
        conn = sqlite3.connect(db)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert versions.count(2) == 1
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='watcher_state'"
        ).fetchone()
        conn.close()
