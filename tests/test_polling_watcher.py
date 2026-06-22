"""Tests for PollingWatcher (cross-platform file watcher)."""

import time
from pathlib import Path

from hub.watchers.polling_watcher import PollingWatcher


class TestPollingWatcher:
    def test_register_file(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        assert pw.register(f) is True
        assert pw.watched_count == 1
        pw.close()

    def test_register_nonexistent(self, tmp_path):
        pw = PollingWatcher()
        assert pw.register(tmp_path / "nope.jsonl") is False
        assert pw.watched_count == 0
        pw.close()

    def test_register_duplicate(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        pw.register(f)
        pw.register(f)  # duplicate
        assert pw.watched_count == 1
        pw.close()

    def test_unregister(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        pw.register(f)
        pw.unregister(f)
        assert pw.watched_count == 0
        pw.close()

    def test_poll_detects_change(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        pw.register(f)

        # Modify the file
        time.sleep(0.05)  # ensure mtime changes
        f.write_text("{}\n{}\n")

        changed = pw.poll(timeout=0.01)
        assert f in changed
        pw.close()

    def test_poll_no_changes(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        pw.register(f)
        changed = pw.poll(timeout=0.01)
        assert changed == []
        pw.close()

    def test_register_directory_contents(self, tmp_path):
        (tmp_path / "a.jsonl").write_text("{}\n")
        (tmp_path / "b.jsonl").write_text("{}\n")
        (tmp_path / "c.txt").write_text("nope\n")
        pw = PollingWatcher()
        count = pw.register_directory_contents(tmp_path, "*.jsonl")
        assert count == 2
        assert pw.watched_count == 2
        pw.close()

    def test_close_clears(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}\n")
        pw = PollingWatcher()
        pw.register(f)
        pw.close()
        assert pw.watched_count == 0
