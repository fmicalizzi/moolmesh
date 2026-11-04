"""OpenCode clocks independent of the machine's zone (#79, points 1 and 2).

1. The event timestamp — part of the fingerprint — is the UTC instant, so a
   re-parse after a DST or zone change is a no-op.
2. Session ``first_event_at`` / ``last_event_at`` are ISO; migration 6 rewrites
   the ms-epoch values already stored, after a backup.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.backfill import run_reparse_opencode
from hub.cache.event_store import EventStore, _compute_fingerprint
from hub.models.opencode import OpenCodeEntry
from tests.test_reparse_opencode import NOW, OLD_MS, _oc_db, _session

# 2026-03-08T06:30:00Z — inside New York's spring-forward night.
DST_MS = 1_772_951_400_000


@pytest.fixture
def system_tz(monkeypatch):
    prior = os.environ.get("TZ")

    def _set(posix: str) -> None:
        monkeypatch.setenv("TZ", posix)
        time.tzset()

    yield _set
    if prior is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prior
    time.tzset()


def _entry(ts=DST_MS) -> OpenCodeEntry:
    return OpenCodeEntry(session_id="ses_a", message_id="m1", part_type="text",
                         role="user", text="hello", timestamp=ts)


class TestAdapterClock:
    def test_event_timestamp_is_utc_instant(self):
        ev = OpenCodeAdapter().to_event(_entry(), "proj")
        assert ev.timestamp == "2026-03-08T06:30:00.000+00:00"

    def test_fingerprint_stable_across_zones_and_dst(self, system_tz):
        seen = set()
        for posix in ("UTC", "UTC+04", "UTC+05", "EST5EDT,M3.2.0,M11.1.0",
                      "IST-5:30", "CET-1CEST,M3.5.0,M10.5.0/3"):
            system_tz(posix)
            ev = OpenCodeAdapter().to_event(_entry(), "proj").to_dict()
            seen.add((ev["timestamp"], _compute_fingerprint(ev)))
        assert len(seen) == 1

    def test_parse_timestamp_is_aware_utc(self):
        dt = OpenCodeAdapter._parse_timestamp(DST_MS)
        assert dt.utcoffset().total_seconds() == 0

    def test_iso_input_keeps_its_offset(self):
        assert OpenCodeAdapter.iso_timestamp("2026-03-08T01:30:00-05:00") == \
            "2026-03-08T01:30:00-05:00"
        assert OpenCodeAdapter.iso_timestamp(0) is None
        assert OpenCodeAdapter.iso_timestamp("") is None

    def test_session_meta_carries_iso(self):
        meta = OpenCodeAdapter().to_session_meta(_entry(), "proj")
        assert meta.first_event_at == meta.last_event_at == "2026-03-08T06:30:00.000+00:00"
        assert "first_event_at" not in meta.to_dict()   # dict shape unchanged


@pytest.fixture
def env(tmp_path):
    oc = tmp_path / "opencode" / "opencode.db"
    oc.parent.mkdir()
    _oc_db(oc)
    db = tmp_path / "moolmesh" / "events.db"
    db.parent.mkdir()
    return {"oc": oc, "db": db, "backups": tmp_path / "moolmesh" / "backups"}


def _reparse(env, store, **kw):
    return run_reparse_opencode(store, db_path=env["db"], backup_dir=env["backups"],
                                opencode_db=env["oc"], now=NOW, **kw)


class TestReparseNoOpAcrossZones:
    @pytest.mark.parametrize("ingest_tz,reparse_tz", [
        ("UTC+04", "UTC+05"),                    # the same machine across DST
        ("EST5EDT,M3.2.0,M11.1.0", "IST-5:30"),  # a zone change
    ])
    def test_reparse_after_zone_change_is_noop(self, env, system_tz, ingest_tz,
                                               reparse_tz):
        _session(env["oc"], "ses_a", updated=OLD_MS)
        _session(env["oc"], "ses_b", updated=DST_MS)
        system_tz(ingest_tz)
        store = EventStore(db_path=env["db"])
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.failed == 0 and rep.new_sessions == 2
        sessions = store._get_conn().execute(
            "SELECT id, first_event_at, last_event_at FROM sessions ORDER BY id"
        ).fetchall()

        system_tz(reparse_tz)
        rep = _reparse(env, store, dry_run=True)
        assert rep.up_to_date == 2 and rep.groups == 0 and rep.events_to_insert == 0
        assert store._get_conn().execute(
            "SELECT id, first_event_at, last_event_at FROM sessions ORDER BY id"
        ).fetchall() == sessions
        store.close()

    def test_session_times_are_iso_after_ingest(self, env):
        _session(env["oc"], "ses_a", updated=OLD_MS)
        store = EventStore(db_path=env["db"])
        _reparse(env, store, dry_run=False, yes=True)
        first, last = store._get_conn().execute(
            "SELECT first_event_at, last_event_at FROM sessions WHERE id = 'ses_a'"
        ).fetchone()
        assert not first.isdigit() and not last.isdigit()
        assert first.endswith("+00:00") and first <= last
        store.close()
