"""Tests for OpenCode live watcher — SQLite polling with rowid cursor."""

import collections
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from hub.watchers.opencode_watcher import OpenCodeWatcher
from hub.cache.event_store import EventStore


def _create_test_db(db_path: Path, num_parts: int = 5) -> None:
    """Create a minimal OpenCode DB with test data."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT)")
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, model TEXT, cost REAL, project_id TEXT)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER)")
    conn.execute("""CREATE TABLE part (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL,
        data TEXT NOT NULL
    )""")
    conn.execute("INSERT INTO project VALUES ('proj1', 'test-project', '/tmp/test')")
    conn.execute("INSERT INTO session VALUES ('ses1', '/tmp/test', 'Test Session', '{}', 0.0, 'proj1')")

    for i in range(num_parts):
        role = "user" if i % 3 == 0 else "assistant"
        msg_data = json.dumps({"role": role})
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?)",
            (f"msg{i}", msg_data, 1700000000000 + i * 1000),
        )
        part_data = json.dumps({"type": "text", "content": f"Message {i} content"})
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (f"part{i}", f"msg{i}", "ses1", 1700000000000 + i * 1000, 1700000000000 + i * 1000, part_data),
        )

    conn.commit()
    conn.close()


class TestOpenCodeWatcher:

    def test_discover_existing_db(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_test_db(db_path)
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=db_path)
        files = watcher.discover_files()
        assert files == [db_path]
        store.close()

    def test_discover_missing_db(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=tmp_path / "nonexistent.db")
        assert watcher.discover_files() == []
        store.close()

    def test_provider_name(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=tmp_path / "oc.db")
        assert watcher.provider_name == "opencode"
        store.close()

    def test_parse_and_adapt_from_zero(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_test_db(db_path, num_parts=5)
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=db_path)
        events, new_offset = watcher._parse_and_adapt(db_path, 0)
        assert len(events) > 0
        assert new_offset > 0
        for ev in events:
            assert ev["provider"] == "opencode"
        store.close()

    def test_incremental_no_new_data(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_test_db(db_path, num_parts=3)
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=db_path)
        _, offset1 = watcher._parse_and_adapt(db_path, 0)
        events2, offset2 = watcher._parse_and_adapt(db_path, offset1)
        assert events2 == []
        assert offset2 == offset1
        store.close()

    def test_incremental_picks_up_new(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_test_db(db_path, num_parts=3)
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, opencode_db=db_path)
        _, offset1 = watcher._parse_and_adapt(db_path, 0)

        # Insert more data
        conn = sqlite3.connect(str(db_path))
        msg_data = json.dumps({"role": "user"})
        conn.execute("INSERT INTO message VALUES ('msg_new', ?, 1700001000000)", (msg_data,))
        part_data = json.dumps({"type": "text", "content": "New message after first poll"})
        conn.execute(
            "INSERT INTO part VALUES ('part_new', 'msg_new', 'ses1', 1700001000000, 1700001000000, ?)",
            (part_data,),
        )
        conn.commit()
        conn.close()

        events2, offset2 = watcher._parse_and_adapt(db_path, offset1)
        assert len(events2) == 1
        assert offset2 > offset1
        store.close()

    def test_sse_buffer_receives_events(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_test_db(db_path, num_parts=3)
        sse_buf = collections.deque(maxlen=100)
        store = EventStore(db_path=tmp_path / "events.db")
        watcher = OpenCodeWatcher(store, sse_buffer=sse_buf, opencode_db=db_path)

        # Simulate one harvest cycle
        from hub.cache.event_store import file_fingerprint
        fp = file_fingerprint(db_path)
        watcher._watched_files[db_path] = fp
        watcher._harvest_file(db_path, fp)

        assert len(sse_buf) > 0
        assert all(ev.get("provider") == "opencode" for ev in sse_buf)
        store.close()
