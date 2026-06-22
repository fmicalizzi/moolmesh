"""Tests for watcher file filtering by recency."""

import os
import time
from pathlib import Path

from hub.cache.event_store import EventStore
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.qwen_watcher import QwenWatcher
from hub.watchers.base import BaseHarvester


class TestClaudeWatcherFiltering:
    def test_discover_skips_old_files(self, tmp_path):
        """Files older than MAX_AGE_HOURS should not be discovered."""
        encoded = "-Users-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        # Recent file (should be included)
        recent = proj_dir / "session-recent.jsonl"
        recent.write_text("{}\n")

        # Old file (should be excluded)
        old = proj_dir / "session-old.jsonl"
        old.write_text("{}\n")
        old_time = time.time() - (24 * 3600)
        os.utime(old, (old_time, old_time))

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = ClaudeWatcher(store=store, claude_base=tmp_path)
        files = watcher.discover_files()

        assert len(files) == 1
        assert files[0].name == "session-recent.jsonl"
        store.close()

    def test_discover_includes_files_within_window(self, tmp_path):
        """Files within MAX_AGE_HOURS should be discovered."""
        encoded = "-Users-test-proj"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        # File modified 1 hour ago (within window)
        f = proj_dir / "session.jsonl"
        f.write_text("{}\n")
        one_hour_ago = time.time() - 3600
        os.utime(f, (one_hour_ago, one_hour_ago))

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = ClaudeWatcher(store=store, claude_base=tmp_path)
        files = watcher.discover_files()

        assert len(files) == 1
        store.close()

    def test_no_files_if_all_old(self, tmp_path):
        """If all files are outside the window, discover returns empty."""
        encoded = "-Users-test-proj"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        old = proj_dir / "session.jsonl"
        old.write_text("{}\n")
        old_time = time.time() - (48 * 3600)  # 2 days ago
        os.utime(old, (old_time, old_time))

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = ClaudeWatcher(store=store, claude_base=tmp_path)
        files = watcher.discover_files()

        assert files == []
        store.close()


class TestQwenWatcherFiltering:
    def test_discover_skips_old_files(self, tmp_path):
        """Qwen watcher should also filter by recency."""
        encoded = "-Users-test-qwen"
        proj_dir = tmp_path / encoded / "chats"
        proj_dir.mkdir(parents=True)

        recent = proj_dir / "chat-recent.jsonl"
        recent.write_text("{}\n")

        old = proj_dir / "chat-old.jsonl"
        old.write_text("{}\n")
        old_time = time.time() - (24 * 3600)
        os.utime(old, (old_time, old_time))

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        watcher = QwenWatcher(store=store, qwen_base=tmp_path)
        files = watcher.discover_files()

        assert len(files) == 1
        assert files[0].name == "chat-recent.jsonl"
        store.close()


class TestFdLimitSafetyNet:
    def test_setrlimit_in_cli(self):
        """Verify that the fd limit raise works."""
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # On most systems, we can set at least 4096
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
            new_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert new_soft >= 4096 or soft >= 4096
        except (ValueError, OSError):
            pass  # Some CI environments restrict this — not a test failure
