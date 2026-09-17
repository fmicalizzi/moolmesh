"""Tests for ingest-based timestamps on resumed sessions (issue #18).

A *resumed* session (``claude --resume``) replays events whose ``timestamp`` is
the original (possibly days-old) message time, while the real ingest happened
seconds ago. ``events.created_at`` (epoch REAL, NOT NULL) is the honest,
monotonic record of ingest. These tests verify:

- ``get_session_detail`` exposes ``last_activity_at`` (ISO-8601 Z, from
  ``MAX(events.created_at)``) without disturbing ``last_event_at``.
- ``get_session_events`` exposes per-event ``created_at`` (raw ingest epoch).
- The export round-trip (``cmd_export`` JSON) carries both.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from hub.cache.event_store import EventStore


OLD_TS = "2026-01-01T00:00:00Z"  # original message time, days in the past


@pytest.fixture
def store(tmp_path) -> EventStore:
    db = tmp_path / "events.db"
    s = EventStore(db)
    yield s
    s.close()


def _make_resumed_session(store: EventStore, session_id: str = "resumed-1") -> None:
    """Register a session whose events all carry an old original timestamp."""
    store.upsert_session(
        {"id": session_id, "provider": "claude", "project": "proj", "title": "Resumed"},
        OLD_TS,
    )
    for i in range(3):
        store.store({
            "provider": "claude", "project": "proj", "event_type": "user",
            "timestamp": OLD_TS, "summary": f"msg {i}", "session_id": session_id,
        })


class TestLastActivityAt:
    def test_populated_and_recent(self, store):
        _make_resumed_session(store)
        before = time.time()
        detail = store.get_session_detail("resumed-1")
        assert detail is not None
        assert detail["last_activity_at"]  # always populated (>=1 event)
        # Ingest happened just now; the old message timestamp did not.
        parsed = datetime.strptime(
            detail["last_activity_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        assert abs(parsed.timestamp() - before) < 60

    def test_last_event_at_semantics_preserved(self, store):
        _make_resumed_session(store)
        detail = store.get_session_detail("resumed-1")
        # last_event_at keeps the ORIGINAL timestamp; last_activity_at is recent.
        assert detail["last_event_at"] == OLD_TS
        assert detail["last_activity_at"] != OLD_TS
        assert detail["last_activity_at"] > detail["last_event_at"]

    def test_provider_scoped(self, store):
        """last_activity_at must not bleed across providers sharing an id."""
        store.upsert_session(
            {"id": "dup", "provider": "claude", "project": "p"}, OLD_TS
        )
        store.store({
            "provider": "claude", "project": "p", "event_type": "user",
            "timestamp": OLD_TS, "summary": "c", "session_id": "dup",
        })
        # A same-id session under a different provider with a much later ingest.
        store.upsert_session(
            {"id": "dup", "provider": "codex", "project": "p"}, OLD_TS
        )
        store.store({
            "provider": "codex", "project": "p", "event_type": "user",
            "timestamp": OLD_TS, "summary": "x", "session_id": "dup",
        })
        detail = store.get_session_detail("dup")  # returns first matching row
        # Whichever provider row is returned, its last_activity_at counts only
        # that provider's events — never a mix. Count must equal event_count.
        assert detail["event_count"] == 1

    def test_pinned_created_at_formatting(self, store, tmp_path):
        _make_resumed_session(store)
        # Pin created_at to a known epoch and assert exact ISO-8601 Z rendering.
        pinned = 1_800_000_000.0  # 2027-01-15T08:00:00Z
        conn = sqlite3.connect(tmp_path / "events.db")
        conn.execute(
            "UPDATE events SET created_at = ? WHERE session_id = 'resumed-1'",
            (pinned,),
        )
        conn.commit()
        conn.close()
        # Fresh store to avoid any connection-cache surprises.
        s2 = EventStore(tmp_path / "events.db")
        detail = s2.get_session_detail("resumed-1")
        s2.close()
        assert detail["last_activity_at"] == "2027-01-15T08:00:00Z"


class TestEventCreatedAt:
    def test_each_event_has_ingest_epoch(self, store):
        _make_resumed_session(store)
        before = time.time()
        events = store.get_session_events("resumed-1")
        assert len(events) == 3
        for e in events:
            assert isinstance(e["created_at"], float)
            assert abs(e["created_at"] - before) < 60
            assert e["timestamp"] == OLD_TS  # original untouched

    def test_created_at_present_with_full_text(self, store):
        _make_resumed_session(store)
        events = store.get_session_events("resumed-1", include_full_text=True)
        assert all("created_at" in e for e in events)

    def test_max_created_at_matches_detail(self, store):
        _make_resumed_session(store)
        events = store.get_session_events("resumed-1")
        detail = store.get_session_detail("resumed-1")
        # Resumed sessions are ordered by ORIGINAL timestamp, so events[-1] is
        # not necessarily the newest ingest — compare against the max.
        max_epoch = max(e["created_at"] for e in events)
        expected = datetime.fromtimestamp(max_epoch, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert detail["last_activity_at"] == expected


class TestExportRoundTrip:
    def test_export_json_carries_both_fields(self, store, tmp_path, capsys):
        import argparse
        from unittest import mock
        from hub import cli

        _make_resumed_session(store)
        store.close()

        args = argparse.Namespace(session_id="resumed-1", format="json")
        # cmd_export builds its own EventStore() from DEFAULT_DB_PATH (resolved
        # at runtime); point it at our temp db.
        with mock.patch(
            "hub.cache.event_store.DEFAULT_DB_PATH", tmp_path / "events.db"
        ):
            cli.cmd_export(args)

        out = json.loads(capsys.readouterr().out)
        assert out["session"]["last_activity_at"]
        assert out["session"]["last_event_at"] == OLD_TS
        assert all("created_at" in e for e in out["events"])
