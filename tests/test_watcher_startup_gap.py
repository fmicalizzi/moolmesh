"""Tests for harvester startup offset behavior.

Replaces the old watcher startup gap tests. The harvester pattern uses
SQLite-persisted offsets instead of timestamp-based offset calculation.
"""

import json
from pathlib import Path

from hub.cache.event_store import EventStore, file_fingerprint
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.qwen_watcher import QwenWatcher


class TestHarvesterNewFileOffset:
    """New files (no offset in DB) should be read from byte 0."""

    def test_new_file_starts_from_zero(self, tmp_path):
        """A file not in file_registry should be harvested from offset 0."""
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)

        f = tmp_path / "session.jsonl"
        f.write_text('{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hello"}}\n')

        fp = file_fingerprint(f)
        assert store.get_offset(fp) is None  # Not in registry
        store.close()

    def test_known_file_resumes_from_stored_offset(self, tmp_path):
        """A file with stored offset should resume from that offset."""
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)

        f = tmp_path / "session.jsonl"
        f.write_text('{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z","message":{"role":"user","content":"hello"}}\n')

        fp = file_fingerprint(f)
        store.save_offset(fp, "claude", str(f), 500)

        assert store.get_offset(fp) == 500
        store.close()


class TestHarvesterOffsetPerFile:
    """Different files get independent offsets via fingerprint."""

    def test_different_files_get_different_offsets(self, tmp_path):
        """Two files should have independent offsets in file_registry."""
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)

        file_a = tmp_path / "session-a.jsonl"
        file_a.write_text('{"type":"user","sessionId":"sa","timestamp":"2026-04-08T10:00:00Z"}\n')

        file_b = tmp_path / "session-b.jsonl"
        file_b.write_text('{"type":"user","sessionId":"sb","timestamp":"2026-04-08T12:00:00Z"}\n')

        fp_a = file_fingerprint(file_a)
        fp_b = file_fingerprint(file_b)

        store.save_offset(fp_a, "claude", str(file_a), 100)
        store.save_offset(fp_b, "claude", str(file_b), 200)

        assert store.get_offset(fp_a) == 100
        assert store.get_offset(fp_b) == 200
        store.close()

    def test_unknown_file_returns_none(self, tmp_path):
        """A file not in file_registry returns None offset."""
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        assert store.get_offset("nonexistent-fingerprint") is None
        store.close()


class TestHarvesterDiscoverFiles:
    """Harvester discover_files still works with new constructor."""

    def test_claude_discovers_files(self, tmp_path):
        """ClaudeWatcher should discover JSONL files."""
        encoded = "-Users-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()
        session = proj_dir / "session.jsonl"
        session.write_text('{"type":"user","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z"}\n')

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = ClaudeWatcher(store=store, claude_base=tmp_path)
        files = watcher.discover_files()

        assert len(files) == 1
        assert files[0].name == "session.jsonl"
        store.close()

    def test_qwen_discovers_files(self, tmp_path):
        """QwenWatcher should discover JSONL files."""
        encoded = "-Users-test-qwen"
        proj_dir = tmp_path / encoded / "chats"
        proj_dir.mkdir(parents=True)
        session = proj_dir / "chat.jsonl"
        session.write_text('{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-10T10:00:00Z"}\n')

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = QwenWatcher(store=store, qwen_base=tmp_path)
        files = watcher.discover_files()

        assert len(files) == 1
        assert files[0].name == "chat.jsonl"
        store.close()
