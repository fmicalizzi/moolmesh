"""Tests for Handoff III: SSE Robusto — reconnection without event loss."""

import collections
import json
import time
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore, _compute_fingerprint


def _make_event(provider="claude", project="proj", event_type="user",
                ts="2026-04-10T10:00:00", summary="hello", session_id="s1"):
    return {
        "provider": provider, "project": project,
        "event_type": event_type, "timestamp": ts,
        "summary": summary, "session_id": session_id,
    }


class TestGetMaxId:
    def test_empty_store_returns_zero(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        assert store.get_max_id() == 0
        store.close()

    def test_returns_highest_id(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="one"))
        store.store(_make_event(summary="two"))
        store.store(_make_event(summary="three"))
        assert store.get_max_id() >= 3
        store.close()


class TestLoadRecentWithIds:
    def test_events_have_id_field(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="ev1"))
        recent = store.load_recent(10)
        assert len(recent) == 1
        assert "id" in recent[0]
        assert isinstance(recent[0]["id"], int)
        store.close()

    def test_ids_are_monotonically_increasing(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        for i in range(5):
            store.store(_make_event(summary=f"event-{i}", ts=f"2026-04-10T10:00:0{i}"))
        recent = store.load_recent(10)
        ids = [ev["id"] for ev in recent]
        assert ids == sorted(ids), "IDs should be in ascending order"
        store.close()


class TestLoadSinceId:
    def test_returns_events_after_id(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="first", ts="2026-04-10T10:00:00"))
        store.store(_make_event(summary="second", ts="2026-04-10T10:00:01"))
        store.store(_make_event(summary="third", ts="2026-04-10T10:00:02"))
        max_id = store.get_max_id()
        # Get events after the first one
        first_id = store.load_recent(10)[0]["id"]
        events = store.load_since_id(first_id)
        assert all(ev["id"] > first_id for ev in events)
        store.close()

    def test_returns_empty_if_caught_up(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="only"))
        max_id = store.get_max_id()
        events = store.load_since_id(max_id)
        assert events == []
        store.close()

    def test_returns_all_if_last_id_zero(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="a", ts="2026-04-10T10:00:00"))
        store.store(_make_event(summary="b", ts="2026-04-10T10:00:01"))
        events = store.load_since_id(0)
        assert len(events) == 2
        assert events[0]["id"] < events[1]["id"]  # ASC order
        store.close()

    def test_respects_limit(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        for i in range(10):
            store.store(_make_event(summary=f"ev-{i}", ts=f"2026-04-10T10:00:{i:02d}"))
        events = store.load_since_id(0, limit=3)
        assert len(events) == 3
        store.close()

    def test_events_have_all_fields(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        store.store({
            "provider": "claude", "project": "myproj",
            "event_type": "tool_use", "timestamp": "2026-04-10T10:00:00",
            "summary": "Read file.py", "session_id": "s1",
            "tokens": {"input": 100, "output": 50},
            "tool_name": "Read", "model": "opus-4",
        })
        events = store.load_since_id(0)
        ev = events[0]
        assert ev["provider"] == "claude"
        assert ev["project"] == "myproj"
        assert ev["tool_name"] == "Read"
        assert ev["tokens"]["input"] == 100
        assert ev["model"] == "opus-4"
        assert "id" in ev
        store.close()


class TestStoreWithOffsetReturnsIds:
    def test_returns_events_with_ids(self, tmp_path):
        from hub.cache.event_store import file_fingerprint
        store = EventStore(db_path=tmp_path / "test.db")
        events = [
            _make_event(summary="a", ts="2026-04-10T10:00:00"),
            _make_event(summary="b", ts="2026-04-10T10:00:01"),
        ]
        # Use a fake fingerprint
        stored = store.store_with_offset(events, "fp001", "claude", "/tmp/f.jsonl", 100)
        assert len(stored) == 2
        assert all("id" in ev for ev in stored)
        assert stored[0]["id"] < stored[1]["id"]
        store.close()

    def test_duplicates_not_returned(self, tmp_path):
        store = EventStore(db_path=tmp_path / "test.db")
        events = [_make_event(summary="dup", ts="2026-04-10T10:00:00")]
        # First store
        stored1 = store.store_with_offset(events, "fp002", "claude", "/tmp/f.jsonl", 50)
        assert len(stored1) == 1
        # Same event again
        stored2 = store.store_with_offset(events, "fp002", "claude", "/tmp/f.jsonl", 50)
        assert len(stored2) == 0  # Duplicate — not inserted
        store.close()


class TestSSEReplayIntegration:
    """Integration test: simulate a client disconnect and reconnect."""

    def test_snapshot_then_replay(self, tmp_path):
        """Snapshot+stream: load recent gets max_id, replay gets new events."""
        store = EventStore(db_path=tmp_path / "test.db")

        # Phase 1: initial events (client loads snapshot)
        for i in range(3):
            store.store(_make_event(summary=f"init-{i}", ts=f"2026-04-10T10:00:0{i}"))

        recent = store.load_recent(500)
        snapshot_max_id = store.get_max_id()
        assert len(recent) == 3
        assert snapshot_max_id >= 3

        # Phase 2: new events arrive while client was disconnected
        for i in range(3, 6):
            store.store(_make_event(summary=f"new-{i}", ts=f"2026-04-10T10:00:0{i}"))

        # Phase 3: client reconnects with last known max_id
        missed = store.load_since_id(snapshot_max_id)
        assert len(missed) == 3
        assert all(ev["id"] > snapshot_max_id for ev in missed)
        assert [ev["summary"] for ev in missed] == ["new-3", "new-4", "new-5"]
        store.close()

    def test_no_gap_between_snapshot_and_stream(self, tmp_path):
        """max_id from snapshot should seamlessly connect to load_since_id."""
        store = EventStore(db_path=tmp_path / "test.db")
        store.store(_make_event(summary="only-event"))

        recent = store.load_recent(500)
        max_id = store.get_max_id()
        assert recent[-1]["id"] == max_id

        # No new events — replay returns empty
        missed = store.load_since_id(max_id)
        assert missed == []
        store.close()
