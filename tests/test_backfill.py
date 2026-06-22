"""Tests for backfill stub and EventStore methods."""

import json
import os
import time
from pathlib import Path

from hub.backfill import backfill, gap_fill
from hub.cache.event_store import EventStore


class TestBackfillStub:
    """backfill.py is now a no-op stub — harvesters handle it."""

    def test_backfill_returns_zero(self):
        result = backfill()
        assert result["total"] == 0
        assert result["claude"] == 0
        assert result["codex"] == 0
        assert result["qwen"] == 0

    def test_gap_fill_returns_zero(self):
        result = gap_fill()
        assert result["total"] == 0

    def test_backfill_accepts_store_arg(self, tmp_path):
        """Stub should accept store kwarg without error."""
        db = tmp_path / "events.db"
        store = EventStore(db)
        result = backfill(store=store)
        assert result["total"] == 0
        store.close()

    def test_gap_fill_accepts_store_arg(self, tmp_path):
        """Stub should accept store kwarg without error."""
        db = tmp_path / "events.db"
        store = EventStore(db)
        result = gap_fill(store=store)
        assert result["total"] == 0
        store.close()


class TestEventStoreMethods:
    """EventStore methods that backfill tests previously validated."""

    def test_get_last_timestamp_per_provider(self, tmp_path):
        """Should return max timestamp per provider."""
        db = tmp_path / "events.db"
        store = EventStore(db)
        store.store_batch([
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "c1"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T11:00:00", "summary": "c2"},
            {"provider": "codex", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T09:00:00", "summary": "x1"},
        ])

        last = store.get_last_timestamp_per_provider()
        assert last["claude"] == "2026-04-08T11:00:00"
        assert last["codex"] == "2026-04-08T09:00:00"
        assert "qwen" not in last
        store.close()

    def test_has_events_empty(self, tmp_path):
        db = tmp_path / "events.db"
        store = EventStore(db)
        assert not store.has_events()
        store.close()

    def test_has_events_populated(self, tmp_path):
        db = tmp_path / "events.db"
        store = EventStore(db)
        store.store({
            "provider": "claude", "project": "p", "event_type": "user",
            "timestamp": "2026-04-08T10:00:00", "summary": "msg",
        })
        assert store.has_events()
        store.close()

    def test_store_batch_dedup_by_fingerprint(self, tmp_path):
        """store_batch with duplicate fingerprints should silently skip duplicates."""
        db = tmp_path / "events.db"
        store = EventStore(db)

        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "hello"},
        ]
        # Insert same batch twice
        store.store_batch(events)
        store.store_batch(events)
        assert store.count() == 1  # duplicate silently skipped
        store.close()
