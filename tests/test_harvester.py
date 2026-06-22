"""Tests for Handoff II: unified harvester pattern."""

import collections
import json
import time
from pathlib import Path

import pytest


class TestBaseHarvester:
    """Core harvester loop logic."""

    def test_harvest_new_file_reads_from_zero(self, tmp_path):
        """New file (no offset in DB) should be read from beginning."""
        from hub.cache.event_store import EventStore, file_fingerprint
        from hub.parsers.claude_parser import ClaudeParser
        from hub.adapters.claude_adapter import ClaudeAdapter

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)

        f = tmp_path / "session.jsonl"
        line = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hello"}}\n'
        f.write_text(line)

        fp = file_fingerprint(f)
        assert store.get_offset(fp) is None  # No prior offset

        parser = ClaudeParser()
        adapter = ClaudeAdapter()
        entries, new_offset = parser.parse_incremental(f, 0)
        events = []
        for entry in entries:
            ev = adapter.to_event(entry, "test")
            if ev:
                events.append(ev.to_dict())

        store.store_with_offset(events, fp, "claude", str(f), new_offset)

        assert store.get_offset(fp) == new_offset
        assert store.count() >= 1
        store.close()

    def test_harvest_resumes_from_stored_offset(self, tmp_path):
        """After restart, harvester should resume from stored offset."""
        from hub.cache.event_store import EventStore, file_fingerprint
        from hub.parsers.claude_parser import ClaudeParser
        from hub.adapters.claude_adapter import ClaudeAdapter

        db = tmp_path / "test.db"
        f = tmp_path / "session.jsonl"
        line1 = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"first"}}\n'
        line2 = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:01Z","message":{"role":"user","content":"second"}}\n'
        f.write_text(line1)

        parser = ClaudeParser()
        adapter = ClaudeAdapter()
        fp = file_fingerprint(f)

        # First harvest
        store1 = EventStore(db_path=db)
        entries, offset = parser.parse_incremental(f, 0)
        events = [adapter.to_event(e, "test").to_dict() for e in entries if adapter.to_event(e, "test")]
        store1.store_with_offset(events, fp, "claude", str(f), offset)
        first_count = store1.count()
        store1.close()

        # Append new data
        f.write_text(line1 + line2)

        # Second harvest (simulates restart)
        store2 = EventStore(db_path=db)
        resumed_offset = store2.get_offset(fp)
        assert resumed_offset == offset  # Should resume from where we left off

        entries2, offset2 = parser.parse_incremental(f, resumed_offset)
        events2 = [adapter.to_event(e, "test").to_dict() for e in entries2 if adapter.to_event(e, "test")]
        store2.store_with_offset(events2, fp, "claude", str(f), offset2)

        assert store2.count() > first_count  # New events added
        store2.close()

    def test_sse_buffer_receives_events(self, tmp_path):
        """Harvested events should appear in the SSE buffer."""
        from hub.cache.event_store import EventStore, file_fingerprint
        from hub.parsers.claude_parser import ClaudeParser
        from hub.adapters.claude_adapter import ClaudeAdapter

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)
        sse_buffer = collections.deque(maxlen=100)

        f = tmp_path / "session.jsonl"
        line = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hello"}}\n'
        f.write_text(line)

        parser = ClaudeParser()
        adapter = ClaudeAdapter()
        fp = file_fingerprint(f)

        entries, new_offset = parser.parse_incremental(f, 0)
        events = []
        for entry in entries:
            ev = adapter.to_event(entry, "test")
            if ev:
                events.append(ev.to_dict())

        store.store_with_offset(events, fp, "claude", str(f), new_offset)
        for ev in events:
            sse_buffer.append(ev)

        assert len(sse_buffer) >= 1
        assert sse_buffer[0]["provider"] == "claude"
        store.close()

    def test_sse_buffer_maxlen_discards_oldest(self):
        """SSE buffer with maxlen should auto-discard oldest events."""
        buf = collections.deque(maxlen=3)
        buf.append({"id": 1})
        buf.append({"id": 2})
        buf.append({"id": 3})
        buf.append({"id": 4})  # Should push out id=1

        assert len(buf) == 3
        assert buf[0]["id"] == 2

    def test_no_queue_no_dispatcher(self):
        """Verify that DashboardServer no longer uses queue.Queue."""
        import inspect
        from hub.dashboard.server import DashboardServer
        source = inspect.getsource(DashboardServer)
        assert "queue.Queue" not in source, "queue.Queue should be eliminated"
        assert "_dispatch_events" not in source, "_dispatch_events should be eliminated"


class TestBackfillStub:
    """backfill.py should be a no-op stub."""

    def test_backfill_returns_zero(self):
        from hub.backfill import backfill
        result = backfill()
        assert result["total"] == 0

    def test_gap_fill_returns_zero(self):
        from hub.backfill import gap_fill
        result = gap_fill()
        assert result["total"] == 0


class TestCodexHarvester:
    """Codex-specific harvester tests."""

    def test_codex_harvest_session_meta(self, tmp_path):
        """Codex harvester should parse session_meta + event_msg."""
        from hub.cache.event_store import EventStore, file_fingerprint
        from hub.parsers.codex_parser import CodexParser
        from hub.adapters.codex_adapter import CodexAdapter

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)

        f = tmp_path / "rollout-test.jsonl"
        lines = [
            '{"type":"session_meta","timestamp":"2026-04-10T10:00:00Z","payload":{"id":"s1","cwd":"/tmp"}}\n',
            '{"type":"event_msg","timestamp":"2026-04-10T10:00:01Z","payload":{"role":"user","content":"hello"}}\n',
        ]
        f.write_text("".join(lines))

        parser = CodexParser()
        adapter = CodexAdapter()
        fp = file_fingerprint(f)

        entries, new_offset = parser.parse_incremental(f, 0)
        events = []
        for entry in entries:
            ev = adapter.to_event(entry, "test-codex")
            if ev:
                events.append(ev.to_dict())

        store.store_with_offset(events, fp, "codex", str(f), new_offset)
        assert new_offset == sum(len(l.encode()) for l in lines)
        store.close()


class TestQwenHarvester:
    """Qwen-specific harvester tests."""

    def test_qwen_harvest_basic(self, tmp_path):
        """Qwen harvester should parse and store events."""
        from hub.cache.event_store import EventStore, file_fingerprint
        from hub.parsers.qwen_parser import QwenParser
        from hub.adapters.qwen_adapter import QwenAdapter

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)

        f = tmp_path / "chat.jsonl"
        line = '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","parts":[{"text":"hello"}]}}\n'
        f.write_text(line)

        parser = QwenParser()
        adapter = QwenAdapter()
        fp = file_fingerprint(f)

        entries, new_offset = parser.parse_incremental(f, 0)
        events = []
        for entry in entries:
            ev = adapter.to_event(entry, "test-qwen")
            if ev:
                events.append(ev.to_dict())

        store.store_with_offset(events, fp, "qwen", str(f), new_offset)
        assert new_offset == len(line.encode())
        store.close()
