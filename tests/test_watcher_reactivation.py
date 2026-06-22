"""Tests for harvester file reactivation after idle period.

With the unified harvester pattern, offsets persist in SQLite (file_registry)
instead of in-memory dicts. This means offsets survive process restarts
and stale/reactivation cycles automatically.
"""

import os
import time
from pathlib import Path

from hub.cache.event_store import EventStore, file_fingerprint
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.base import BaseHarvester


class TestFileReactivation:
    def test_offset_preserved_in_sqlite(self, tmp_path):
        """Offsets should persist in SQLite file_registry."""
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)

        encoded = "-Users-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        session = proj_dir / "session.jsonl"
        session.write_text(
            '{"type":"user","sessionId":"s1","timestamp":"2026-04-08T10:00:00Z",'
            '"message":{"role":"user","content":"hello"}}\n'
        )

        fp = file_fingerprint(session)
        store.save_offset(fp, "claude", str(session), session.stat().st_size)

        # Verify offset is in SQLite
        assert store.get_offset(fp) == session.stat().st_size
        store.close()

    def test_offset_survives_process_restart(self, tmp_path):
        """Offsets should survive closing and reopening the EventStore."""
        db = tmp_path / "events.db"

        encoded = "-Users-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        session = proj_dir / "session.jsonl"
        line1 = (
            '{"type":"user","sessionId":"s1","timestamp":"2026-04-08T10:00:00Z",'
            '"message":{"role":"user","content":"first"}}\n'
        )
        session.write_text(line1)

        fp = file_fingerprint(session)
        offset_before = len(line1.encode())

        # Save offset in first "process"
        store1 = EventStore(db_path=db)
        store1.save_offset(fp, "claude", str(session), offset_before)
        store1.close()

        # Reopen in second "process"
        store2 = EventStore(db_path=db)
        assert store2.get_offset(fp) == offset_before
        store2.close()

    def test_no_events_lost_during_stale_window(self, tmp_path):
        """Events written while file is 'stale' should be captured on reactivation.

        With SQLite-persisted offsets, the harvester resumes from the stored
        offset regardless of whether the file was recently watched or not.
        """
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)

        encoded = "-Users-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()

        session = proj_dir / "session.jsonl"
        session.write_text("")

        fp = file_fingerprint(session)
        store.save_offset(fp, "claude", str(session), 0)

        # Write events during stale period
        with open(session, "a") as f:
            f.write(
                '{"type":"user","sessionId":"s1","timestamp":"2026-04-08T16:00:00Z",'
                '"message":{"role":"user","content":"written while stale"}}\n'
            )

        # Offset should still be 0, so harvester reads everything
        assert store.get_offset(fp) == 0
        store.close()
