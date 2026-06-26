"""Tests for full-text storage, export, and search (Phase 2 — Issues #5, #8)."""

import json
import time
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore


@pytest.fixture
def store(tmp_path) -> EventStore:
    db = tmp_path / "events.db"
    s = EventStore(db)
    yield s
    s.close()


def _make_event(
    summary="hello world",
    full_text=None,
    provider="claude",
    project="test-proj",
    event_type="user",
    session_id="sess-001",
    timestamp="2026-06-26T10:00:00",
    **kw,
):
    d = {
        "provider": provider,
        "project": project,
        "event_type": event_type,
        "timestamp": timestamp,
        "summary": summary,
        "session_id": session_id,
    }
    if full_text is not None:
        d["full_text"] = full_text
    d.update(kw)
    return d


class TestEventContentTable:
    def test_table_exists(self, store):
        tables = [
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "event_content" in tables

    def test_table_schema(self, store):
        cols = [
            r[1]
            for r in store._conn.execute("PRAGMA table_info(event_content)").fetchall()
        ]
        assert "event_id" in cols
        assert "full_text" in cols


class TestStoreWithOffset:
    def test_stores_full_text(self, store):
        events = [_make_event(full_text="This is the complete message text.")]
        result = store.store_with_offset(events, "fp1", "claude", "/tmp/f.jsonl", 100)
        assert len(result) == 1
        eid = result[0]["id"]
        row = store._conn.execute(
            "SELECT full_text FROM event_content WHERE event_id = ?", (eid,)
        ).fetchone()
        assert row is not None
        assert row[0] == "This is the complete message text."

    def test_strips_full_text_from_result(self, store):
        events = [_make_event(full_text="big content here")]
        result = store.store_with_offset(events, "fp2", "claude", "/tmp/f.jsonl", 200)
        assert len(result) == 1
        assert "full_text" not in result[0]

    def test_no_full_text_no_content_row(self, store):
        events = [_make_event()]
        result = store.store_with_offset(events, "fp3", "claude", "/tmp/f.jsonl", 300)
        assert len(result) == 1
        eid = result[0]["id"]
        row = store._conn.execute(
            "SELECT full_text FROM event_content WHERE event_id = ?", (eid,)
        ).fetchone()
        assert row is None


class TestStoreFullText:
    def test_store_single_with_full_text(self, store):
        ev = _make_event(full_text="complete text for single store")
        store.store(ev)
        row = store._conn.execute(
            "SELECT full_text FROM event_content"
        ).fetchone()
        assert row is not None
        assert row[0] == "complete text for single store"

    def test_store_batch_with_full_text(self, store):
        events = [
            _make_event(
                summary=f"msg{i}",
                full_text=f"full content {i}",
                timestamp=f"2026-06-26T10:00:{i:02d}",
            )
            for i in range(3)
        ]
        store.store_batch(events)
        rows = store._conn.execute(
            "SELECT full_text FROM event_content ORDER BY event_id"
        ).fetchall()
        assert len(rows) == 3
        assert rows[0][0] == "full content 0"
        assert rows[2][0] == "full content 2"


class TestGetSessionEvents:
    def test_without_full_text(self, store):
        events = [
            _make_event(full_text="hidden text", timestamp="2026-06-26T10:00:00"),
            _make_event(
                summary="second",
                full_text="also hidden",
                timestamp="2026-06-26T10:00:01",
                event_type="assistant",
            ),
        ]
        store.store_with_offset(events, "fp4", "claude", "/tmp/f.jsonl", 400)
        results = store.get_session_events("sess-001", include_full_text=False)
        assert len(results) == 2
        for r in results:
            assert "full_text" not in r

    def test_with_full_text(self, store):
        events = [
            _make_event(full_text="visible text", timestamp="2026-06-26T10:00:00"),
        ]
        store.store_with_offset(events, "fp5", "claude", "/tmp/f.jsonl", 500)
        results = store.get_session_events("sess-001", include_full_text=True)
        assert len(results) == 1
        assert results[0]["full_text"] == "visible text"

    def test_backward_compat_no_content(self, store):
        store.store(_make_event(summary="old event without full_text"))
        results = store.get_session_events("sess-001", include_full_text=True)
        assert len(results) == 1
        assert "full_text" not in results[0]


class TestSearchSessionContent:
    def test_basic_search(self, store):
        from hub.mcp_server import _search_session_content

        events = [
            _make_event(
                full_text="The quick brown fox jumps over the lazy dog",
                timestamp="2026-06-26T10:00:00",
            ),
            _make_event(
                summary="unrelated",
                full_text="Nothing interesting here",
                timestamp="2026-06-26T10:00:01",
                event_type="assistant",
            ),
        ]
        store.store_with_offset(events, "fp6", "claude", "/tmp/f.jsonl", 600)
        results = _search_session_content(str(store.db_path), "brown fox")
        assert len(results) == 1
        assert "context" in results[0]

    def test_search_with_provider_filter(self, store):
        from hub.mcp_server import _search_session_content

        events = [
            _make_event(
                full_text="search target text",
                provider="claude",
                timestamp="2026-06-26T10:00:00",
            ),
            _make_event(
                full_text="search target text also here",
                provider="codex",
                timestamp="2026-06-26T10:00:01",
                event_type="assistant",
            ),
        ]
        store.store_with_offset(events, "fp7", "claude", "/tmp/f.jsonl", 700)
        results = _search_session_content(
            str(store.db_path), "target text", provider="claude"
        )
        assert len(results) == 1
        assert results[0]["provider"] == "claude"


class TestExportCommand:
    def test_export_markdown(self, store, capsys):
        store.upsert_session(
            {
                "id": "sess-001",
                "provider": "claude",
                "project": "test-proj",
                "title": "Test Session",
                "model": "claude-opus-4",
            },
            "2026-06-26T10:00:00",
        )
        events = [
            _make_event(
                full_text="Hello, how are you?",
                event_type="user",
                timestamp="2026-06-26T10:00:00",
            ),
            _make_event(
                summary="I'm fine",
                full_text="I'm fine, thanks for asking!",
                event_type="assistant",
                timestamp="2026-06-26T10:00:01",
            ),
        ]
        store.store_with_offset(events, "fp8", "claude", "/tmp/f.jsonl", 800)

        import argparse

        args = argparse.Namespace(
            session_id="sess-001", format="markdown", output=None
        )
        # Monkey-patch EventStore to use our tmp db
        import hub.cli as cli_mod
        original_init = EventStore.__init__

        def patched_init(self_es, db_path=None):
            original_init(self_es, db_path=store.db_path)

        EventStore.__init__ = patched_init
        try:
            cli_mod.cmd_export(args)
        finally:
            EventStore.__init__ = original_init

        output = capsys.readouterr().out
        assert "# Session: Test Session" in output
        assert "User" in output
        assert "Assistant" in output
        assert "Hello, how are you?" in output
        assert "I'm fine, thanks for asking!" in output

    def test_export_json(self, store, capsys):
        store.upsert_session(
            {
                "id": "sess-001",
                "provider": "claude",
                "project": "test-proj",
            },
            "2026-06-26T10:00:00",
        )
        events = [
            _make_event(
                full_text="full content",
                timestamp="2026-06-26T10:00:00",
            ),
        ]
        store.store_with_offset(events, "fp9", "claude", "/tmp/f.jsonl", 900)

        import argparse

        args = argparse.Namespace(
            session_id="sess-001", format="json", output=None
        )
        import hub.cli as cli_mod
        original_init = EventStore.__init__

        def patched_init(self_es, db_path=None):
            original_init(self_es, db_path=store.db_path)

        EventStore.__init__ = patched_init
        try:
            cli_mod.cmd_export(args)
        finally:
            EventStore.__init__ = original_init

        output = capsys.readouterr().out
        data = json.loads(output)
        assert "session" in data
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0].get("full_text") == "full content"

    def test_export_to_file(self, store, tmp_path):
        store.upsert_session(
            {"id": "sess-001", "provider": "claude", "project": "test-proj"},
            "2026-06-26T10:00:00",
        )
        events = [
            _make_event(full_text="file content", timestamp="2026-06-26T10:00:00"),
        ]
        store.store_with_offset(events, "fp10", "claude", "/tmp/f.jsonl", 1000)

        out_file = tmp_path / "export.md"
        import argparse

        args = argparse.Namespace(
            session_id="sess-001", format="markdown", output=str(out_file)
        )
        import hub.cli as cli_mod
        original_init = EventStore.__init__

        def patched_init(self_es, db_path=None):
            original_init(self_es, db_path=store.db_path)

        EventStore.__init__ = patched_init
        try:
            cli_mod.cmd_export(args)
        finally:
            EventStore.__init__ = original_init

        assert out_file.exists()
        content = out_file.read_text()
        assert "file content" in content


class TestAdapterFullText:
    def test_claude_adapter_full_text(self):
        from hub.adapters.claude_adapter import ClaudeAdapter
        from hub.models.claude import ClaudeEntry, ClaudeContentBlock

        adapter = ClaudeAdapter()
        entry = ClaudeEntry(
            uuid="u1",
            parent_uuid=None,
            type="user",
            session_id="s1",
            timestamp="2026-06-26T10:00:00",
            content_blocks=[
                ClaudeContentBlock(type="text", text="Hello this is a long message"),
            ],
            content_text="Hello this is a long message",
        )
        event = adapter.to_event(entry, "proj")
        assert event is not None
        assert event.full_text == "Hello this is a long message"
        assert event.summary == "Hello this is a long message"

    def test_claude_adapter_thinking_full_text(self):
        from hub.adapters.claude_adapter import ClaudeAdapter
        from hub.models.claude import ClaudeEntry, ClaudeContentBlock

        adapter = ClaudeAdapter()
        entry = ClaudeEntry(
            uuid="u2",
            parent_uuid=None,
            type="assistant",
            session_id="s1",
            timestamp="2026-06-26T10:00:00",
            content_blocks=[
                ClaudeContentBlock(type="thinking", thinking="deep thoughts here"),
                ClaudeContentBlock(type="text", text="The answer is 42"),
            ],
            content_text="The answer is 42",
        )
        event = adapter.to_event(entry, "proj")
        assert event is not None
        assert "[thinking]" in event.full_text
        assert "deep thoughts here" in event.full_text
        assert "The answer is 42" in event.full_text

    def test_opencode_adapter_full_text(self):
        from hub.adapters.opencode_adapter import OpenCodeAdapter
        from hub.models.opencode import OpenCodeEntry

        adapter = OpenCodeAdapter()
        entry = OpenCodeEntry(
            session_id="s1",
            message_id="m1",
            part_type="text",
            role="user",
            text="Line one\nLine two\nLine three",
            timestamp="1719388800000",
        )
        event = adapter.to_event(entry, "proj")
        assert event is not None
        assert event.full_text == "Line one\nLine two\nLine three"
        assert "\n" not in event.summary

    def test_qwen_adapter_full_text(self):
        from hub.adapters.qwen_adapter import QwenAdapter
        from hub.models.qwen import QwenEntry

        adapter = QwenAdapter()
        entry = QwenEntry(
            type="user",
            uuid="q1",
            session_id="s1",
            timestamp="2026-06-26T10:00:00",
            text="Multi\nline\nuser\nmessage",
            role="user",
        )
        event = adapter.to_event(entry, "proj")
        assert event is not None
        assert event.full_text == "Multi\nline\nuser\nmessage"
        assert "\n" not in event.summary

    def test_codex_adapter_full_text(self):
        from hub.adapters.codex_adapter import CodexAdapter
        from hub.models.codex import CodexEntry

        adapter = CodexAdapter()
        entry = CodexEntry(
            session_id="s1",
            timestamp="2026-06-26T10:00:00",
            event_type="event_msg",
            event_msg_text="This is the full user prompt text",
        )
        event = adapter.to_event(entry, "proj")
        assert event is not None
        assert event.full_text == "This is the full user prompt text"


class TestMCPGetSessionEvents:
    def test_mcp_get_session_events(self, store):
        from hub.mcp_server import _get_session_events

        events = [
            _make_event(
                full_text="mcp full text",
                timestamp="2026-06-26T10:00:00",
            ),
        ]
        store.store_with_offset(events, "fp11", "claude", "/tmp/f.jsonl", 1100)

        results = _get_session_events(
            str(store.db_path), "sess-001", include_full_text=True
        )
        assert len(results) == 1
        assert results[0]["full_text"] == "mcp full text"

    def test_mcp_get_session_events_no_full_text(self, store):
        from hub.mcp_server import _get_session_events

        events = [
            _make_event(
                full_text="hidden in mcp",
                timestamp="2026-06-26T10:00:00",
            ),
        ]
        store.store_with_offset(events, "fp12", "claude", "/tmp/f.jsonl", 1200)

        results = _get_session_events(
            str(store.db_path), "sess-001", include_full_text=False
        )
        assert len(results) == 1
        assert "full_text" not in results[0]
