"""#122: explicit marker for Codex/Pi per-directory extras; Qwen entry/parent ids."""

from __future__ import annotations

import sqlite3

from hub.adapters.codex_adapter import CodexAdapter, is_dir_extra
from hub.adapters.pi_adapter import PiAdapter
from hub.adapters.qwen_adapter import QwenAdapter
from hub.cache.event_store import EventStore, _compute_fingerprint, _normalize_event
from hub.dashboard.server import SessionTracker
from hub.linearize import active_branch
from hub.mcp_server import _get_tool_stats
from hub.models.codex import CodexEntry, CodexFunctionCall
from hub.models.pi import PiEntry, PiToolCall
from hub.models.qwen import QwenEntry


def _pi_entry(eid="e1", ts="2026-09-21T10:00:05Z"):
    return PiEntry(
        event_type="message", entry_id=eid, timestamp=ts, session_id="pi-s",
        cwd="/r/a", role="assistant",
        tool_calls=[PiToolCall(call_id="c1", name="read", arguments={"path": "/r/a/1.md"}),
                    PiToolCall(call_id="c2", name="read", arguments={"path": "/r/b/2.md"})],
    )


def _codex_entry():
    body = "".join(f"*** Update File: {p}\n@@\n-x\n+y\n" for p in ("/r/a/1.md", "/r/b/2.md"))
    return CodexEntry(
        event_type="response_item", timestamp="2026-09-21T10:00:05Z",
        payload_type="custom_tool_call",
        function_call=CodexFunctionCall(call_id="c1", name="apply_patch",
                                        arguments=f"*** Begin Patch\n{body}*** End Patch\n"),
        session_id="cx-s", cwd="/r/a",
    )


def _dir_extra_col(db):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT dir_extra FROM events ORDER BY id")]
    finally:
        conn.close()


def test_pi_extra_marked_and_excluded_from_tool_stats(tmp_path):
    evs = PiAdapter().to_events(_pi_entry(), "proj")
    assert len(evs) == 2 and not evs[0].dir_extra and evs[1].dir_extra
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    store.store_batch([e.to_dict() for e in evs])
    store.close()
    assert _dir_extra_col(db) == [None, 1]
    assert _get_tool_stats(str(db)) == [{"tool_name": "read", "count": 1, "projects": 1}]
    tracker = SessionTracker()
    for e in evs:
        tracker.track(e.to_dict())
    assert tracker.get_tool_stats() == [{"name": "read", "count": 1}]


def test_codex_new_extra_is_marked(tmp_path):
    evs = CodexAdapter().to_events(_codex_entry(), "proj")
    assert [e.dir_extra for e in evs] == [False, True]
    assert is_dir_extra(evs[1].to_dict()) and not is_dir_extra(evs[0].to_dict())
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    store.store_batch([e.to_dict() for e in evs])
    store.close()
    assert _dir_extra_col(db) == [None, 1]


def test_old_codex_row_still_excluded_by_shape(tmp_path):
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    old = [e.to_dict() for e in CodexAdapter().to_events(_codex_entry(), "proj")]
    for d in old:
        d.pop("dir_extra", None)  # as written before #122
    store.store_batch(old)
    store.close()
    assert _dir_extra_col(db) == [None, None]
    assert _get_tool_stats(str(db)) == [{"tool_name": "apply_patch", "count": 1, "projects": 1}]


def test_migration_10_is_idempotent(tmp_path):
    db = tmp_path / "events.db"
    EventStore(db_path=db).close()
    EventStore(db_path=db).close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 10").fetchone()[0] == 1
    finally:
        conn.close()


def _q(uuid, parent, text, ts):
    return QwenEntry(type="user", uuid=uuid, parent_uuid=parent, session_id="q-s",
                     timestamp=ts, cwd="/r/a", role="user", text=text)


def test_qwen_fills_ids_and_export_shows_active_branch(tmp_path):
    entries = [_q("u1", None, "root", "2026-09-21T10:00:01Z"),
               _q("u2", "u1", "abandoned", "2026-09-21T10:00:02Z"),
               _q("u3", "u1", "active", "2026-09-21T10:00:03Z")]
    evs = [QwenAdapter().to_event(e, "proj").to_dict() for e in entries]
    assert evs[1]["entry_id"] == "u2" and evs[1]["parent_id"] == "u1"
    branch = active_branch(evs)
    assert [e["entry_id"] for e in branch] == ["u1", "u3"]


def test_qwen_reread_does_not_duplicate_legacy_rows(tmp_path):
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    ev = QwenAdapter().to_event(_q("u1", None, "hello", "2026-09-21T10:00:01Z"), "proj").to_dict()
    legacy = {k: v for k, v in ev.items() if k not in ("entry_id", "parent_id")}
    store.store_batch([legacy])  # a row stored before #122 (no entry_id)
    store.store_batch([ev])      # the same line re-read after #122
    store.close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        conn.close()
    assert _compute_fingerprint(_normalize_event(legacy)) == \
        _compute_fingerprint(_normalize_event(ev), legacy=True)
