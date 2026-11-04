"""``events.entry_id`` and the fingerprint (issue #67).

Distinct entries with the same ms + summary (parallel tool results) no longer
collapse; rows stored before #67 keep their fingerprint and a re-read never
inserts them a second time.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import time
from pathlib import Path

from hub.cache.event_store import EventStore, _compute_fingerprint
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.pi_watcher import PiWatcher

TS = "2026-05-01T10:00:08.050Z"


def _now_iso(offset: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset))


def _pi_file(base: Path, results: int = 3, ts: str = TS) -> Path:
    """A Pi session with ``results`` toolResult entries in the same ms, same text."""
    d = base / "sessions" / "--home-dev-acme-web--"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    lines = [
        {"type": "session", "version": 3, "id": "sess-67", "timestamp": ts,
         "cwd": "/home/dev/acme-web"},
        {"type": "message", "id": "u001", "parentId": None, "timestamp": ts,
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
    ]
    for i in range(results):
        lines.append({
            "type": "message", "id": f"r00{i}", "parentId": "u001", "timestamp": ts,
            "message": {"role": "toolResult", "toolCallId": f"call-{i}",
                        "toolName": "read", "isError": False,
                        "content": [{"type": "text", "text": "same output\n"}]},
        })
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return f


def _harvest(w, offset_zero: bool = False) -> None:
    w._rescan()
    for path, fp in list(w._watched_files.items()):
        if offset_zero:
            w.harvest_history_file(path, fp, offset=0)
        else:
            w._harvest_file(path, fp)


def _rows(store) -> list[tuple]:
    return store._conn.execute(
        "SELECT id, fingerprint, entry_id, summary FROM events ORDER BY id").fetchall()


class TestFingerprint:
    def test_without_entry_id_is_unchanged(self):
        ev = {"provider": "codex", "session_id": "s", "timestamp": TS,
              "event_type": "tool_result", "summary": "x"}
        import hashlib
        legacy = hashlib.md5(f"codex|s|{TS}|tool_result|x".encode()).hexdigest()
        assert _compute_fingerprint(ev) == legacy

    def test_entry_id_distinguishes(self):
        a = {"provider": "pi", "session_id": "s", "timestamp": TS,
             "event_type": "tool_result", "summary": "x", "entry_id": "r1"}
        b = dict(a, entry_id="r2")
        assert _compute_fingerprint(a) != _compute_fingerprint(b)
        assert _compute_fingerprint(a, legacy=True) == _compute_fingerprint(b, legacy=True)


class TestMigration:
    def test_adds_nullable_column_once_and_keeps_rows(self, tmp_path):
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        store._conn.execute("ALTER TABLE events DROP COLUMN entry_id")  # pre-#67 shape
        store._conn.execute("DELETE FROM schema_migrations WHERE version = 7")
        store._conn.commit()
        store._conn.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " session_id, fingerprint, created_at) VALUES"
            " ('pi', 'p', 'user', ?, 'old row', 's', 'legacy-fp', 1.0)", (TS,))
        store._conn.commit()
        before = [(i, fp, None, summary) for i, fp, summary in store._conn.execute(
            "SELECT id, fingerprint, summary FROM events")]
        store.close()

        store = EventStore(db_path=db)
        cols = {r[1]: r for r in store._conn.execute("PRAGMA table_info(events)")}
        assert "entry_id" in cols and cols["entry_id"][3] == 0  # nullable
        assert _rows(store) == before  # untouched, entry_id NULL
        applied = [r[0] for r in store._conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 7")]
        assert applied == [7]
        store.close()
        store = EventStore(db_path=db)  # idempotent reopen
        assert _rows(store) == before
        store.close()


class TestParallelToolResults:
    def test_same_ms_same_summary_no_longer_collapse(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        _pi_file(base, results=3, ts=_now_iso())
        store = EventStore(db_path=tmp_path / "events.db")
        sse: collections.deque = collections.deque()
        _harvest(PiWatcher(store, sse, pi_base=base))
        rows = _rows(store)
        results = [r for r in rows if r[2] and r[2].startswith("r00")]
        assert sorted(r[2] for r in results) == ["r000", "r001", "r002"]
        assert len({r[1] for r in results}) == 3
        assert all("entry_id" not in e for e in sse)  # SSE payload unchanged
        store.close()

    def test_reread_is_idempotent(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        _pi_file(base, results=3)
        store = EventStore(db_path=tmp_path / "events.db")
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files(since=0)
        for path in list(w._file_projects):
            w.harvest_history_file(path, w.registry_key(path), offset=0)
        first = _rows(store)
        for path in list(w._file_projects):
            w.harvest_history_file(path, w.registry_key(path), offset=0)
        assert _rows(store) == first
        store.close()


class TestPreExistingRowsNeverDuplicate:
    """Mandatory (#67): rows written before entry_id keep their fingerprint and a
    re-read from offset 0 inserts none of them again."""

    def _legacy_ingest(self, store, w, path) -> None:
        """What pre-#67 code stored: the same events, without entry_id."""
        events, new_offset = w._parse_and_adapt(path, 0)
        for e in events:
            e.pop("entry_id", None)
        store.store_with_offset(events, w.registry_key(path), "pi", str(path),
                                new_offset, historical=True)
        w._parser._session_ctx.pop(str(path), None)  # a fresh daemon, like an upgrade

    def test_pi_reread_after_upgrade(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _pi_file(base, results=3)
        store = EventStore(db_path=tmp_path / "events.db")
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files(since=0)
        self._legacy_ingest(store, w, f)
        old = _rows(store)
        assert all(r[2] is None for r in old)
        # The pre-#67 collision: three results collapsed into one row.
        assert sum(r[3] == old[-1][3] for r in old) == 1

        parsed, inserted = w.harvest_history_file(f, w.registry_key(f), offset=0)
        assert parsed > 0 and inserted == 0
        assert _rows(store) == old  # same ids, same fingerprints, no duplicate

        # A NEW entry appended after the upgrade is stored with its entry id.
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "message", "id": "u002", "parentId": "r002", "timestamp": TS,
                "message": {"role": "user", "content": [{"type": "text", "text": "next"}]},
            }) + "\n")
        w.harvest_history_file(f, w.registry_key(f), offset=0)
        new = _rows(store)
        assert new[:len(old)] == old
        assert [r[2] for r in new[len(old):]] == ["u002"]
        store.close()

    def test_claude_reread_after_upgrade(self, tmp_path):
        base = tmp_path / "claude"
        proj = base / "-Users-u-work-widget-services"
        proj.mkdir(parents=True)
        f = proj / "c1.jsonl"
        f.write_text("\n".join(json.dumps({
            "type": "user", "sessionId": "c1", "cwd": "/Users/u/work/widget-services",
            "uuid": f"uuid-{i}", "timestamp": _now_iso(i - 10),
            "message": {"role": "user", "content": f"msg {i}"},
        }) for i in range(3)) + "\n")
        store = EventStore(db_path=tmp_path / "events.db")
        w = ClaudeWatcher(store, None, claude_base=base)
        w._rescan()
        events, off = w._parse_and_adapt(f, 0)
        assert [e["entry_id"] for e in events] == ["uuid-0", "uuid-1", "uuid-2"]
        for e in events:
            e.pop("entry_id")
        store.store_with_offset(events, w.registry_key(f), "claude", str(f), off)
        old = _rows(store)
        _, inserted = w.harvest_history_file(f, w.registry_key(f), offset=0)
        assert inserted == 0 and _rows(store) == old
        store.close()

    def test_store_and_store_batch_honor_legacy_rows(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        ev = {"provider": "pi", "project": "p", "event_type": "user",
              "timestamp": TS, "summary": "x", "session_id": "s"}
        store.store(ev)
        store.store(dict(ev, entry_id="e1"))
        store.store_batch([dict(ev, entry_id="e1")])
        assert len(_rows(store)) == 1
        store.close()


def test_entry_id_column_holds_native_ids(tmp_path):
    db = tmp_path / "events.db"
    base = tmp_path / ".pi" / "agent"
    _pi_file(base, results=1, ts=_now_iso())
    store = EventStore(db_path=db)
    _harvest(PiWatcher(store, None, pi_base=base))
    store.close()
    conn = sqlite3.connect(db)
    ids = {r[0] for r in conn.execute("SELECT entry_id FROM events")}
    conn.close()
    assert {"u001", "r000"} <= ids
