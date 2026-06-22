"""Tests for Handoff I: chunk-and-tail, truncation, fingerprint, transactional offsets."""

import json
import queue
import hashlib
from pathlib import Path

import pytest


class TestChunkAndTail:
    """Chunk-and-tail: only advance offset to last complete newline."""

    def test_claude_partial_write_preserved(self, tmp_path):
        """Partial line at end should not advance offset past it."""
        from hub.parsers.claude_parser import ClaudeParser

        f = tmp_path / "session.jsonl"
        complete = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hello"}}\n'
        partial = '{"type":"assistant","sessionId":"s1","timesta'
        f.write_text(complete + partial)

        parser = ClaudeParser()
        entries, offset = parser.parse_incremental(f, 0)

        assert len(entries) == 1
        assert entries[0].type == "user"
        assert offset == len(complete.encode())

    def test_claude_partial_write_recovered_on_next_read(self, tmp_path):
        """After partial write completes, next read should pick it up."""
        from hub.parsers.claude_parser import ClaudeParser

        f = tmp_path / "session.jsonl"
        line1 = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hi"}}\n'
        partial = '{"type":"assistant","sessionId":"s1"'
        f.write_text(line1 + partial)

        parser = ClaudeParser()
        entries1, offset1 = parser.parse_incremental(f, 0)
        assert len(entries1) == 1
        assert offset1 == len(line1.encode())

        # Complete the partial line
        rest = ',"timestamp":"2026-04-10T10:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"bye"}]}}\n'
        f.write_text(line1 + partial + rest)

        entries2, offset2 = parser.parse_incremental(f, offset1)
        assert len(entries2) == 1
        assert entries2[0].type == "assistant"

    def test_qwen_partial_write_preserved(self, tmp_path):
        """Qwen parser handles partial writes identically."""
        from hub.parsers.qwen_parser import QwenParser

        f = tmp_path / "chat.jsonl"
        complete = '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","parts":[{"text":"hello"}]}}\n'
        partial = '{"type":"assistant","uuid":"u2","ses'
        f.write_text(complete + partial)

        parser = QwenParser()
        entries, offset = parser.parse_incremental(f, 0)
        assert len(entries) == 1
        assert offset == len(complete.encode())

    def test_codex_partial_write_preserved(self, tmp_path):
        """Codex parser handles partial writes identically."""
        from hub.parsers.codex_parser import CodexParser

        f = tmp_path / "rollout-test.jsonl"
        complete = '{"type":"session_meta","timestamp":"2026-04-10T10:00:00Z","payload":{"id":"s1","cwd":"/tmp"}}\n'
        partial = '{"type":"event_msg","timestamp":"2026-04'
        f.write_text(complete + partial)

        parser = CodexParser()
        entries, offset = parser.parse_incremental(f, 0)
        assert len(entries) == 1
        assert offset == len(complete.encode())

    def test_no_newline_at_all_returns_nothing(self, tmp_path):
        """If there's no complete line, return nothing and don't advance."""
        from hub.parsers.claude_parser import ClaudeParser

        f = tmp_path / "session.jsonl"
        f.write_text('{"partial": true, "no_newline": true')

        parser = ClaudeParser()
        entries, offset = parser.parse_incremental(f, 0)
        assert len(entries) == 0
        assert offset == 0


class TestTruncationDetection:
    """If offset > file_size, file was truncated -- reset to 0."""

    def test_truncation_resets_offset(self, tmp_path):
        """Truncated file should be re-read from beginning."""
        from hub.parsers.claude_parser import ClaudeParser

        f = tmp_path / "session.jsonl"
        line1 = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"first"}}\n'
        line2 = '{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:01Z","message":{"role":"user","content":"second"}}\n'
        f.write_text(line1 + line2)

        parser = ClaudeParser()
        entries, offset = parser.parse_incremental(f, 0)
        assert len(entries) == 2
        old_offset = offset

        # Truncate file to just line1
        f.write_text(line1)
        assert old_offset > f.stat().st_size

        # Parser should detect truncation and reset
        entries2, offset2 = parser.parse_incremental(f, old_offset)
        assert len(entries2) == 1
        assert entries2[0].timestamp == "2026-04-10T10:00:00Z"


class TestFileFingerprint:
    """Content-based file identification."""

    def test_fingerprint_stable_across_reads(self, tmp_path):
        """Same file content produces same fingerprint."""
        from hub.cache.event_store import file_fingerprint

        f = tmp_path / "session.jsonl"
        f.write_text('{"type":"user","sessionId":"abc"}\n')

        fp1 = file_fingerprint(f)
        fp2 = file_fingerprint(f)
        assert fp1 == fp2
        assert len(fp1) == 32

    def test_fingerprint_changes_on_rewrite(self, tmp_path):
        """Different content produces different fingerprint."""
        from hub.cache.event_store import file_fingerprint

        f = tmp_path / "session.jsonl"
        f.write_text('{"type":"user","sessionId":"abc"}\n')
        fp1 = file_fingerprint(f)

        f.write_text('{"type":"user","sessionId":"xyz"}\n')
        fp2 = file_fingerprint(f)
        assert fp1 != fp2

    def test_fingerprint_nonexistent_file(self, tmp_path):
        """Missing file returns empty string."""
        from hub.cache.event_store import file_fingerprint

        fp = file_fingerprint(tmp_path / "nope.jsonl")
        assert fp == ""


class TestTransactionalOffset:
    """Atomic event storage + offset update."""

    def test_store_with_offset_persists_both(self, tmp_path):
        """Events and offset should be committed atomically."""
        from hub.cache.event_store import EventStore

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)

        events = [
            {"provider": "claude", "project": "test", "event_type": "user",
             "timestamp": "2026-04-10T10:00:00Z", "summary": "hello",
             "session_id": "s1"},
        ]
        store.store_with_offset(events, "fp123", "claude", "/tmp/file.jsonl", 500)

        # Verify events stored
        assert store.count() == 1

        # Verify offset stored
        offset = store.get_offset("fp123")
        assert offset == 500

        store.close()

    def test_get_offset_unknown_file(self, tmp_path):
        """Unknown fingerprint returns None."""
        from hub.cache.event_store import EventStore

        db = tmp_path / "test.db"
        store = EventStore(db_path=db)
        assert store.get_offset("unknown") is None
        store.close()

    def test_offset_survives_reopen(self, tmp_path):
        """Offset persists across EventStore instances (survives restart)."""
        from hub.cache.event_store import EventStore

        db = tmp_path / "test.db"
        store1 = EventStore(db_path=db)
        store1.store_with_offset([], "fp456", "claude", "/tmp/f.jsonl", 1234)
        store1.close()

        store2 = EventStore(db_path=db)
        assert store2.get_offset("fp456") == 1234
        store2.close()
