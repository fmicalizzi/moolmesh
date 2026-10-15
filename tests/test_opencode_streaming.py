"""OpenCode parts read while streaming (#75): completeness-aware rowid cursor.

OpenCode inserts a ``part`` row when a stream starts and UPDATEs it in place.
The incremental cursor must not move past a part that is still streaming
(bug A), and must advance by batch — never to the table's global MAX(rowid),
which skipped every part past the first 500 of a poll (bug B).
"""

from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

import pytest

from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.cache.event_store import EventStore
from hub.parsers.opencode_parser import _STALE_MS, OpenCodeParser, _is_complete
from hub.watchers.opencode_watcher import OpenCodeWatcher

NOW = 1_780_000_000_000  # ms epoch, injected as "now"


def _db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            model TEXT, cost REAL, project_id TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        INSERT INTO session VALUES ('ses_test', '/Users/u/work/proj', 't', '', 0, NULL);
    """)
    c.execute("INSERT INTO message VALUES ('m_user', ?, 1)", (json.dumps({"role": "user"}),))
    c.execute("INSERT INTO message VALUES ('m_asst', ?, 2)", (json.dumps({"role": "assistant"}),))
    c.commit()
    c.close()


def _put(path: Path, pid: str, data: dict, updated: int = NOW, msg: str = "m_asst") -> int:
    c = sqlite3.connect(path)
    cur = c.execute("INSERT INTO part VALUES (?, ?, 'ses_test', ?, ?, ?)",
                    (pid, msg, updated, updated, json.dumps(data)))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _update(path: Path, pid: str, data: dict, updated: int = NOW) -> None:
    c = sqlite3.connect(path)
    c.execute("UPDATE part SET data = ?, time_updated = ? WHERE id = ?",
              (json.dumps(data), updated, pid))
    c.commit()
    c.close()


def _text(text: str = "", end: bool = True) -> dict:
    t = {"start": 1}
    if end:
        t["end"] = 2
    return {"type": "text", "text": text, "time": t}


@pytest.fixture
def oc(tmp_path):
    p = tmp_path / "opencode.db"
    _db(p)
    return p


# ── _is_complete ────────────────────────────────────────────────────────────

FRESH, OLD = NOW - 1_000, NOW - _STALE_MS - 1


@pytest.mark.parametrize("data, role, updated, expected", [
    (_text("x", end=True), "assistant", FRESH, True),
    (_text("", end=False), "assistant", FRESH, False),
    (_text("", end=False), "assistant", OLD, True),            # stale cap
    ({"type": "text", "text": "hi"}, "user", FRESH, True),      # user: no data.time
    (_text("", end=False), "user", FRESH, True),                # user never streams
    ({"type": "text", "text": "x"}, "assistant", FRESH, True),  # no time = no signal
    ({"type": "reasoning", "text": "r", "time": {"start": 1, "end": 2}}, "assistant", FRESH, True),
    ({"type": "reasoning", "text": "", "time": {"start": 1}}, "assistant", FRESH, False),
    ({"type": "reasoning", "text": "", "time": {"start": 1}}, "assistant", OLD, True),
    ({"type": "tool", "state": {"status": "pending"}}, "assistant", FRESH, False),
    ({"type": "tool", "state": {"status": "running"}}, "assistant", FRESH, False),
    ({"type": "tool", "state": {"status": "running"}}, "assistant", OLD, True),
    ({"type": "tool", "state": {"status": "completed"}}, "assistant", FRESH, True),
    ({"type": "tool", "state": {"status": "error"}}, "assistant", FRESH, True),
    ({"type": "tool", "state": {}}, "assistant", FRESH, True),
    ({"type": "step-start"}, "assistant", FRESH, True),
    ({"type": "step-finish", "tokens": {}}, "assistant", FRESH, True),
    ({"type": "patch", "files": []}, "assistant", FRESH, True),
    ({"type": "file", "path": "/Users/u/work/proj/a.py"}, "assistant", FRESH, True),
    ({"type": "compaction"}, "assistant", FRESH, True),
    ({"type": "something-new"}, "assistant", FRESH, True),
    (_text("", end=False), "assistant", None, True),            # unreadable age
    (_text("", end=False), "assistant", "2026-06-01T10:00:00", True),
])
def test_is_complete_table(data, role, updated, expected):
    assert _is_complete(data, role, updated, NOW) is expected


# ── bug B: never jump to the global MAX(rowid) ──────────────────────────────

def _drain(parser: OpenCodeParser, path: Path, offset: int = 0, now_ms: int = NOW):
    entries = []
    while True:
        batch, new = parser.parse_incremental(path, offset, now_ms=now_ms)
        entries.extend(batch)
        if new == offset:
            return entries, offset
        offset = new


def test_more_than_one_batch_of_parts_is_read_whole(oc):
    c = sqlite3.connect(oc)
    for i in range(1200):
        c.execute("INSERT INTO part VALUES (?, 'm_asst', 'ses_test', ?, ?, ?)",
                  (f"p{i}", 1000 + i, 1000 + i, json.dumps(_text(f"x{i}"))))
    c.commit()
    c.close()
    entries, offset = _drain(OpenCodeParser(), oc)
    assert len(entries) == 1200
    assert offset == 1200
    assert [e.text for e in entries[:2]] == ["x0", "x1"]


# ── bug A: streaming parts hold the cursor ──────────────────────────────────

def test_streaming_text_holds_cursor_until_finished(oc):
    parser = OpenCodeParser()
    rid = _put(oc, "p_txt", _text("", end=False))
    entries, offset = parser.parse_incremental(oc, 0, now_ms=NOW)
    assert entries == [] and offset == rid - 1

    _update(oc, "p_txt", _text("the full answer", end=True))
    entries, offset = parser.parse_incremental(oc, offset, now_ms=NOW)
    assert [e.text for e in entries] == ["the full answer"]
    assert offset == rid


def test_streaming_reasoning_holds_cursor_until_finished(oc):
    parser = OpenCodeParser()
    rid = _put(oc, "p_r", {"type": "reasoning", "text": "", "time": {"start": 1}})
    entries, offset = parser.parse_incremental(oc, 0, now_ms=NOW)
    assert entries == [] and offset == rid - 1

    _update(oc, "p_r", {"type": "reasoning", "text": "thinking it through",
                        "time": {"start": 1, "end": 2}})
    entries, offset = parser.parse_incremental(oc, offset, now_ms=NOW)
    assert [(e.part_type, e.text) for e in entries] == [("reasoning", "thinking it through")]
    assert offset == rid


def test_tool_is_emitted_once_completed_with_path_and_output(oc):
    p = OpenCodeParser()
    tool = {"type": "tool", "tool": "read", "callID": "call_1",
            "state": {"status": "pending", "input": {}}}
    rid = _put(oc, "p_tool", tool)
    assert p.parse_incremental(oc, 0, now_ms=NOW) == ([], rid - 1)

    tool["state"] = {"status": "running",
                     "input": {"filePath": "/Users/u/work/proj/src/app.py"}}
    _update(oc, "p_tool", tool)
    assert p.parse_incremental(oc, rid - 1, now_ms=NOW) == ([], rid - 1)

    tool["state"] = {"status": "completed",
                     "input": {"filePath": "/Users/u/work/proj/src/app.py"},
                     "output": "file body"}
    _update(oc, "p_tool", tool)
    entries, offset = p.parse_incremental(oc, rid - 1, now_ms=NOW)
    assert offset == rid and len(entries) == 1

    ev = OpenCodeAdapter().to_event(entries[0], "proj")
    assert ev.event_type == "tool_use"
    assert ev.tool_name == "read"
    assert ev.file_path == "/Users/u/work/proj/src/app.py"
    assert ev.summary == "file body" and ev.full_text == "file body"


def test_user_text_without_time_is_emitted_immediately(oc):
    rid = _put(oc, "p_u", {"type": "text", "text": "please fix it"}, msg="m_user")
    entries, offset = OpenCodeParser().parse_incremental(oc, 0, now_ms=NOW)
    assert [(e.role, e.text) for e in entries] == [("user", "please fix it")]
    assert offset == rid


def test_stale_streaming_part_is_emitted_with_what_it_has(oc):
    rid = _put(oc, "p_cut", _text("half an ans", end=False), updated=NOW - _STALE_MS - 1)
    entries, offset = OpenCodeParser().parse_incremental(oc, 0, now_ms=NOW)
    assert [e.text for e in entries] == ["half an ans"]
    assert offset == rid


def test_step_start_and_bad_json_never_pin_the_cursor(oc):
    _put(oc, "p_ss", {"type": "step-start"})
    c = sqlite3.connect(oc)
    c.execute("INSERT INTO part VALUES ('p_bad', 'm_asst', 'ses_test', ?, ?, '{not json')",
              (NOW, NOW))
    c.commit()
    c.close()
    rid = _put(oc, "p_ok", _text("ok"))
    entries, offset = OpenCodeParser().parse_incremental(oc, 0, now_ms=NOW)
    assert [e.text for e in entries] == ["ok"]
    assert offset == rid


def test_long_paths_are_kept_whole(oc):
    long_path = "/Users/u/work/proj/" + "nested/" * 15 + "module.py"
    _put(oc, "p_w", {"type": "tool", "tool": "write",
                     "state": {"status": "completed",
                               "input": {"filePath": long_path, "content": "x"}}})
    _put(oc, "p_b", {"type": "tool", "tool": "bash",
                     "state": {"status": "completed", "input": {"command": "echo " + "y" * 200},
                               "output": "ok"}})
    entries, _ = OpenCodeParser().parse_incremental(oc, 0, now_ms=NOW)
    events = [OpenCodeAdapter().to_event(e, "proj") for e in entries]
    assert events[0].file_path == long_path
    assert len(events[1].file_path) == 80  # a command keeps its short preview


# ── complete parts after a streaming one: re-read without duplicates ───────

def test_rereads_after_a_streaming_part_do_not_duplicate_rows_or_sse(oc, tmp_path):
    store = EventStore(db_path=tmp_path / "events.db")
    sse: collections.deque = collections.deque()
    watcher = OpenCodeWatcher(store, sse_buffer=sse, opencode_db=oc)
    fp = watcher.registry_key(oc)
    clock = {"now": NOW}
    real = watcher._parser.parse_incremental
    watcher._parser.parse_incremental = (
        lambda path, off: real(path, off, now_ms=clock["now"]))
    try:
        rid = _put(oc, "p_stream", _text("", end=False))
        _put(oc, "p_after1", {"type": "text", "text": "question one"}, msg="m_user")
        _put(oc, "p_after2", {"type": "text", "text": "question two"}, msg="m_user")

        watcher._harvest_file(oc, fp)
        first = store._get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert first == 2 and len(sse) == 2
        assert watcher._stored_offset(fp, oc) == rid - 1

        for _ in range(3):  # re-reads while the stream is still open
            watcher._harvest_file(oc, fp)
        assert store._get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert len(sse) == 2

        _update(oc, "p_stream", _text("now finished", end=True))
        watcher._harvest_file(oc, fp)
        rows = [r[0] for r in store._get_conn().execute(
            "SELECT summary FROM events ORDER BY id")]
        assert rows == ["question one", "question two", "now finished"]
        assert len(sse) == 3
        assert watcher._stored_offset(fp, oc) == rid + 2
    finally:
        store.close()
