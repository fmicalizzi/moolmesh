"""``events.parent_id`` and the active-branch transcript (issue #67).

Synthetic Acme fixtures only. A Pi session where the user went back with
``/tree`` exports only the active branch, in order; the abandoned branch stays
in events.db. Sessions without ``parent_id`` read exactly as before.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path

from hub.cache.event_store import EventStore, _compute_fingerprint
from hub.linearize import active_branch
from hub.watchers.pi_watcher import PiWatcher

SID = "sess-acme-tree"


def _ts(i: int) -> str:
    return f"2026-05-01T10:00:{i:02d}.000Z"


def _msg(eid, parent, i, role, text):
    return {"type": "message", "id": eid, "parentId": parent, "timestamp": _ts(i),
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _pi_tree_file(base: Path) -> Path:
    """u1 → a1 → {u2 → a2 (abandoned), u3 → a3 (active, appended last)}."""
    d = base / "sessions" / "--home-dev-acme-web--"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "tree.jsonl"
    lines = [
        {"type": "session", "version": 3, "id": SID, "timestamp": _ts(0),
         "cwd": "/home/dev/acme-web"},
        _msg("u1", None, 1, "user", "acme: add the login form"),
        _msg("a1", "u1", 2, "assistant", "acme: form added"),
        _msg("u2", "a1", 3, "user", "acme: use library X"),
        _msg("a2", "u2", 4, "assistant", "acme: switched to X"),
        _msg("u3", "a1", 5, "user", "acme: keep it plain instead"),
        _msg("a3", "u3", 6, "assistant", "acme: kept plain"),
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return f


def _store_with_tree(tmp_path: Path) -> EventStore:
    base = tmp_path / ".pi" / "agent"
    _pi_tree_file(base)
    store = EventStore(db_path=tmp_path / "events.db")
    w = PiWatcher(store, collections.deque(), pi_base=base)
    w.discover_files(since=0)
    for path in list(w._file_projects):
        w.harvest_history_file(path, w.registry_key(path), offset=0)
    return store


def _summaries(events) -> list[str]:
    # The Pi session header is an event without an entry id: always kept.
    return [e["summary"] for e in events if not e["summary"].startswith("[session")]


class TestHelper:
    def test_without_parent_id_is_identity(self):
        evs = [{"entry_id": "x", "summary": "1"}, {"summary": "2"},
               {"entry_id": "y", "summary": "3"}]
        assert active_branch(evs) == evs

    def test_drops_only_the_abandoned_subtree(self):
        evs = [
            {"entry_id": "u1", "parent_id": None},
            {"entry_id": "a1", "parent_id": "u1"},
            {"entry_id": "u2", "parent_id": "a1"},
            {"entry_id": "a2", "parent_id": "u2"},
            {"summary": "legacy row, no id"},
            {"entry_id": "u3", "parent_id": "a1"},
            {"entry_id": "a3", "parent_id": "u3"},
        ]
        out = active_branch(evs)
        assert [e.get("entry_id") for e in out] == ["u1", "a1", None, "u3", "a3"]

    def test_gap_in_the_chain_hides_nothing_above_it(self):
        # u3's parent was never stored (a skipped bookkeeping line): what hangs
        # above the gap cannot be proven off-branch, so it is kept.
        evs = [
            {"entry_id": "u1", "parent_id": None},
            {"entry_id": "a1", "parent_id": "u1"},
            {"entry_id": "u3", "parent_id": "missing"},
            {"entry_id": "a3", "parent_id": "u3"},
        ]
        assert active_branch(evs) == evs


class TestStoredSession:
    def test_parent_id_stored_and_fingerprint_unchanged(self, tmp_path):
        store = _store_with_tree(tmp_path)
        rows = store._conn.execute(
            "SELECT entry_id, parent_id, fingerprint, provider, session_id,"
            " timestamp, event_type, summary FROM events ORDER BY id").fetchall()
        by_id = {r[0]: r for r in rows}
        assert by_id["a2"][1] == "u2" and by_id["u3"][1] == "a1"
        assert by_id["u1"][1] is None
        r = by_id["a3"]
        ev = {"provider": r[3], "session_id": r[4], "timestamp": r[5],
              "event_type": r[6], "summary": r[7], "entry_id": r[0]}
        assert r[2] == _compute_fingerprint(ev)
        assert _compute_fingerprint(dict(ev, parent_id="zzz")) == r[2]
        store.close()

    def test_active_branch_in_order_and_abandoned_kept_in_db(self, tmp_path):
        store = _store_with_tree(tmp_path)
        all_events = store.get_session_events(SID)
        assert len(_summaries(all_events)) == 6
        assert all("parent_id" not in e for e in all_events)
        branch = store.get_session_events(SID, active_branch=True)
        assert _summaries(branch) == [
            "acme: add the login form", "acme: form added",
            "acme: keep it plain instead", "acme: kept plain"]
        assert all("parent_id" not in e and "entry_id" not in e for e in branch)
        assert store._conn.execute("SELECT COUNT(*) FROM events WHERE summary LIKE 'acme:%'"
        ).fetchone()[0] == 6
        store.close()

    def test_export_markdown_shows_only_active_branch(self, tmp_path, capsys,
                                                      monkeypatch):
        store = _store_with_tree(tmp_path)
        db = store.db_path
        store.close()
        import hub.cli as cli_mod
        original_init = EventStore.__init__
        monkeypatch.setattr(EventStore, "__init__",
                            lambda self, db_path=None: original_init(self, db_path=db))
        cli_mod.cmd_export(argparse.Namespace(session_id=SID, format="markdown",
                                              output=None))
        out = capsys.readouterr().out
        assert "library X" not in out and "switched to X" not in out
        assert out.index("add the login form") < out.index("form added") \
            < out.index("keep it plain instead") < out.index("kept plain")

    def test_mcp_session_events_active_branch(self, tmp_path):
        from hub.mcp_server import _get_session_events
        store = _store_with_tree(tmp_path)
        db = str(store.db_path)
        store.close()
        asc = _get_session_events(db, SID, text_mode="full")
        assert _summaries(asc) == [
            "acme: add the login form", "acme: form added",
            "acme: keep it plain instead", "acme: kept plain"]
        assert all("parent_id" not in e and "entry_id" not in e for e in asc)
        desc = _get_session_events(db, SID, order="desc", limit=2, offset=1)
        assert _summaries(desc) == ["acme: keep it plain instead", "acme: form added"]

    def test_session_without_parent_id_unchanged(self, tmp_path):
        from hub.mcp_server import _get_session_events
        store = EventStore(db_path=tmp_path / "events.db")
        for i, s in enumerate(["one", "two", "three"]):
            store.store({"provider": "codex", "project": "acme", "session_id": "plain",
                         "event_type": "user", "timestamp": _ts(i), "summary": s})
        assert _summaries(store.get_session_events("plain", active_branch=True)) \
            == _summaries(store.get_session_events("plain")) == ["one", "two", "three"]
        db = str(store.db_path)
        store.close()
        assert _summaries(_get_session_events(db, "plain", order="desc", limit=2)) \
            == ["three", "two"]


class TestMigration9:
    def test_adds_nullable_column_once_and_keeps_rows(self, tmp_path):
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        store._conn.execute("ALTER TABLE events DROP COLUMN parent_id")  # pre-#67b
        store._conn.execute("DELETE FROM schema_migrations WHERE version = 9")
        store._conn.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " session_id, fingerprint, created_at, entry_id) VALUES"
            " ('pi', 'acme', 'user', ?, 'old row', 's', 'old-fp', 1.0, 'e1')",
            (_ts(1),))
        store._conn.commit()
        before = store._conn.execute(
            "SELECT id, fingerprint, entry_id, summary FROM events").fetchall()
        store.close()

        for _ in range(2):  # second open: idempotent
            store = EventStore(db_path=db)
            cols = {r[1]: r for r in store._conn.execute("PRAGMA table_info(events)")}
            assert "parent_id" in cols and cols["parent_id"][3] == 0  # nullable
            assert store._conn.execute(
                "SELECT id, fingerprint, entry_id, summary FROM events").fetchall() \
                == before
            assert store._conn.execute(
                "SELECT parent_id FROM events").fetchall() == [(None,)]
            assert [r[0] for r in store._conn.execute(
                "SELECT version FROM schema_migrations WHERE version = 9")] == [9]
            store.close()

    def test_mcp_on_db_without_column_reads_as_before(self, tmp_path):
        from hub.mcp_server import _get_session_events
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        store.store({"provider": "pi", "project": "acme", "session_id": "s",
                     "event_type": "user", "timestamp": _ts(1), "summary": "hi"})
        store.close()
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE events DROP COLUMN parent_id")
        conn.commit()
        conn.close()
        assert _summaries(_get_session_events(str(db), "s")) == ["hi"]
