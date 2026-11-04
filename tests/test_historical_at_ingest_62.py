"""Any ingest of an event older than the live window is history (issue #62).

Generalizes #61: a live watcher's first full read of a DB-backed provider
(OpenCode, Cursor), the startup catch-up and a backfill all store an event
whose parsed time is older than ``MAX_AGE_HOURS`` with ``historical = 1`` —
off the recent feed and off SSE. Also checks the interaction with #18
(resumed Claude sessions replaying old timestamps live).
"""

from __future__ import annotations

import collections
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from hub.cache.event_store import EventStore, is_older_than
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.opencode_watcher import OpenCodeWatcher

DAY = 86400


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _flags(store) -> dict[str, int]:
    return {summary: h for summary, h in store._conn.execute(
        "SELECT summary, historical FROM events")}


def _claude_lines(path: Path, sid: str, items: list[tuple[str, float]]) -> None:
    """Append user messages ``(text, event_epoch)`` to a Claude JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for i, (text, epoch) in enumerate(items):
            fh.write(json.dumps({
                "type": "user", "sessionId": sid, "cwd": "/Users/u/work/widget-services",
                "uuid": f"{sid}-{text}-{i}", "timestamp": _iso(epoch),
                "message": {"role": "user", "content": text},
            }) + "\n")


def _harvest(w) -> None:
    w._rescan()
    for path, fp in list(w._watched_files.items()):
        w._harvest_file(path, fp)


class TestIsOlderThan:
    def test_formats(self):
        cutoff = time.time() - 12 * 3600
        assert is_older_than(_iso(time.time() - 2 * DAY), cutoff)
        assert not is_older_than(_iso(time.time() - 60), cutoff)
        assert is_older_than(str(int((time.time() - 2 * DAY) * 1000)), cutoff)  # ms epoch
        assert is_older_than("2026-01-01T00:00:00+00:00", cutoff)

    def test_unreadable_is_never_old(self):
        cutoff = time.time()
        assert not is_older_than("", cutoff)
        assert not is_older_than(None, cutoff)
        assert not is_older_than("ayer a la tarde", cutoff)


class TestLiveWatcherFirstRead:
    def test_old_and_new_lines_in_one_live_file(self, tmp_path):
        base = tmp_path / "claude"
        store = EventStore(db_path=tmp_path / "events.db")
        sse: collections.deque = collections.deque()
        f = base / "-Users-u-work-widget-services" / "s1.jsonl"
        _claude_lines(f, "s1", [("old one", time.time() - 3 * DAY),
                                ("fresh one", time.time() - 60)])
        w = ClaudeWatcher(store, sse, claude_base=base)
        _harvest(w)
        assert _flags(store) == {"old one": 1, "fresh one": 0}
        assert [e["summary"] for e in sse] == ["fresh one"]
        assert all("historical" not in e for e in sse)  # SSE schema unchanged
        assert [e["summary"] for e in store.load_recent(50)] == ["fresh one"]
        store.close()

    def test_opencode_first_read_of_old_history_is_historical(self, tmp_path):
        db = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                model TEXT, cost REAL, project_id TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT);
            INSERT INTO session VALUES ('ses1', '/Users/u/work/widget-services', 't',
                '', 0, NULL);
        """)
        old_ms = int((time.time() - 90 * DAY) * 1000)
        new_ms = int((time.time() - 30) * 1000)
        for i, (ms, text) in enumerate([(old_ms, "months ago"), (new_ms, "just now")]):
            conn.execute("INSERT INTO message VALUES (?, ?, ?)",
                         (f"m{i}", json.dumps({"role": "user"}), ms))
            conn.execute("INSERT INTO part VALUES (?, ?, 'ses1', ?, ?, ?)",
                         (f"p{i}", f"m{i}", ms, ms,
                          json.dumps({"type": "text", "text": text})))
        conn.commit()
        conn.close()
        store = EventStore(db_path=tmp_path / "events.db")
        sse: collections.deque = collections.deque()
        w = OpenCodeWatcher(store, sse_buffer=sse, opencode_db=db)
        _harvest(w)
        assert _flags(store) == {"months ago": 1, "just now": 0}
        assert [e["summary"] for e in sse] == ["just now"]
        store.close()

    def test_rereading_does_not_flip_or_duplicate(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        ev = {"provider": "fake", "project": "p", "event_type": "user",
              "session_id": "s", "summary": "x", "timestamp": _iso(time.time() - 5 * DAY)}
        store.store_with_offset([ev], "fp", "fake", "/f", 10)
        again = store.store_with_offset([ev], "fp", "fake", "/f", 10)
        assert again == []
        assert _flags(store) == {"x": 1}
        store.close()

    def test_explicit_cutoff_wins(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        ev = {"provider": "fake", "project": "p", "event_type": "user",
              "session_id": "s", "summary": "x", "timestamp": _iso(time.time() - 5 * DAY)}
        stored = store.store_with_offset([ev], "fp", "fake", "/f", 10,
                                         live_since=time.time() - 30 * DAY)
        assert "historical" not in stored[0]
        assert _flags(store) == {"x": 0}
        store.close()


class TestResumedSessions18:
    """#18: ``claude --resume`` replays old timestamps; the session is active now."""

    def test_replayed_lines_are_history_and_activity_stays_recent(self, tmp_path):
        base = tmp_path / "claude"
        store = EventStore(db_path=tmp_path / "events.db")
        sse: collections.deque = collections.deque()
        f = base / "-Users-u-work-widget-services" / "resumed.jsonl"
        _claude_lines(f, "resumed", [("replayed 1", time.time() - 5 * DAY),
                                     ("replayed 2", time.time() - 5 * DAY + 60)])
        w = ClaudeWatcher(store, sse, claude_base=base)
        _harvest(w)
        assert set(_flags(store).values()) == {1}
        assert len(sse) == 0  # the replay is not live news

        _claude_lines(f, "resumed", [("new prompt", time.time())])
        _harvest(w)
        assert _flags(store) == {"replayed 1": 1, "replayed 2": 1, "new prompt": 0}
        assert [e["summary"] for e in sse] == ["new prompt"]

        detail = store.get_session_detail("resumed")
        activity = datetime.strptime(detail["last_activity_at"], "%Y-%m-%dT%H:%M:%SZ")
        age = time.time() - activity.replace(tzinfo=timezone.utc).timestamp()
        assert age < 120  # the live row's ingest clock, not the replayed dates
        store.close()

    def test_replay_alone_dates_the_session_by_its_events(self, tmp_path):
        """Before the first new message the session's activity is the replayed time."""
        base = tmp_path / "claude"
        store = EventStore(db_path=tmp_path / "events.db")
        f = base / "-Users-u-work-widget-services" / "resumed.jsonl"
        replay_at = time.time() - 5 * DAY
        _claude_lines(f, "resumed", [("replayed", replay_at)])
        os.utime(f, None)
        _harvest(ClaudeWatcher(store, collections.deque(), claude_base=base))
        detail = store.get_session_detail("resumed")
        activity = datetime.strptime(detail["last_activity_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert abs(activity.replace(tzinfo=timezone.utc).timestamp() - replay_at) < 2
        store.close()
