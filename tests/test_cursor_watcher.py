"""End-to-end tests for the Cursor watcher."""

import json
import sqlite3
from pathlib import Path

from hub.cache.event_store import EventStore
from hub.watchers.cursor_watcher import CursorWatcher


def _setup_cursor(base: Path) -> Path:
    base.mkdir(parents=True)
    (base / "globalStorage").mkdir()
    gdb = base / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(gdb))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                 ("bubbleId:c1:b1", json.dumps({"type": 1, "text": "do it"})))
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                 ("bubbleId:c1:b2", json.dumps({"type": 2, "text": "done", "tokenCount": 7})))
    conn.commit()
    conn.close()
    ws = base / "workspaceStorage" / "ws1"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///dev/myproj"}))
    wconn = sqlite3.connect(str(ws / "state.vscdb"))
    wconn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    wconn.execute("INSERT INTO ItemTable VALUES (?, ?)",
                  ("composer.composerData",
                   json.dumps({"allComposers": [{"composerId": "c1", "name": "Chat",
                                                 "lastUpdatedAt": 1700000000000}]})))
    wconn.commit()
    wconn.close()
    return gdb


def test_watcher_harvests_events_and_session(tmp_path):
    base = tmp_path / "User"
    gdb = _setup_cursor(base)
    store = EventStore(db_path=tmp_path / "events.db")
    watcher = CursorWatcher(store, cursor_base=base)

    assert watcher.provider_name == "cursor"
    assert watcher.discover_files() == [gdb]

    events, new_offset = watcher._parse_and_adapt(gdb, 0)
    assert len(events) == 2
    assert {e["event_type"] for e in events} == {"user", "assistant"}
    assert all(e["project"] == "myproj" for e in events)
    assert new_offset == 2

    sessions = store.get_sessions(hours=99999)
    assert any(s["id"] == "c1" and s["provider"] == "cursor" for s in sessions)
