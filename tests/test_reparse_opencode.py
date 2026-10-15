"""Tests for ``mool backfill --reparse opencode`` (#75).

Events stored before v1.27.1 hold placeholders ("[text]", "[tool]") for parts
read while still streaming, and sessions whose parts were skipped never made
it into events.db. The re-parse re-reads opencode.db per session and swaps
each session's events atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import hub.cache.event_store as es
import hub.watchers.opencode_watcher as ow
from hub.backfill import run_reparse_opencode
from hub.cache.event_store import EventStore

NOW = 1_780_000_000.0          # s epoch, injected
OLD_MS = int(NOW * 1000) - 3 * 86_400_000
LIVE_MS = int(NOW * 1000) - 60_000


def _oc_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            model TEXT, cost REAL, project_id TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE INDEX part_session_idx ON part(session_id);
        INSERT INTO project VALUES ('prj', 'proj', '/Users/u/work/proj');
    """)
    c.commit()
    c.close()


def _session(path: Path, sid: str, updated: int = OLD_MS) -> None:
    """One session: user prompt, reasoning, assistant text and a read tool."""
    c = sqlite3.connect(path)
    c.execute("INSERT INTO session VALUES (?, '/Users/u/work/proj', 'title', '', 0, 'prj')",
              (sid,))
    c.execute("INSERT INTO message VALUES (?, ?, ?)",
              (f"{sid}_mu", json.dumps({"role": "user"}), updated))
    c.execute("INSERT INTO message VALUES (?, ?, ?)",
              (f"{sid}_ma", json.dumps({"role": "assistant"}), updated + 1))
    parts = [
        (f"{sid}_mu", {"type": "text", "text": "please review the module"}),
        (f"{sid}_ma", {"type": "reasoning", "text": "reading the module first",
                       "time": {"start": 1, "end": 2}}),
        (f"{sid}_ma", {"type": "tool", "tool": "read",
                       "state": {"status": "completed",
                                 "input": {"filePath": "/Users/u/work/proj/src/mod.py"},
                                 "output": "module body"}}),
        (f"{sid}_ma", {"type": "text", "text": "the module looks fine",
                       "time": {"start": 1, "end": 2}}),
    ]
    for i, (mid, data) in enumerate(parts):
        c.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                  (f"{sid}_p{i}", mid, sid, updated + 10 + i, updated + 10 + i,
                   json.dumps(data)))
    c.commit()
    c.close()


def _placeholder_rows(path: Path, sid: str) -> list[dict]:
    """What the pre-#75 cursor stored for a session: streamed parts as placeholders."""
    from hub.adapters.opencode_adapter import OpenCodeAdapter
    from hub.parsers.opencode_parser import OpenCodeParser

    entries = OpenCodeParser().parse_session(path, sid)
    adapter = OpenCodeAdapter()
    events = []
    for e in entries:
        if e.part_type in ("text", "reasoning") and e.role != "user":
            e.text = ""
        if e.part_type == "tool":
            e.text = ""
            e.tool_call.input_data = {}
        events.append(adapter.to_event(e, "proj").to_dict())
    return events


@pytest.fixture
def env(tmp_path):
    oc = tmp_path / "opencode" / "opencode.db"
    oc.parent.mkdir()
    _oc_db(oc)
    db = tmp_path / "moolmesh" / "events.db"
    db.parent.mkdir()
    return {"oc": oc, "db": db, "backups": tmp_path / "moolmesh" / "backups"}


def _store_legacy(env, sids: list[str]) -> EventStore:
    store = EventStore(db_path=env["db"])
    for sid in sids:
        events = _placeholder_rows(env["oc"], sid)
        store.store_with_offset(events, "", "opencode", str(env["oc"]), 0)
        store.upsert_session({"id": sid, "provider": "opencode", "project": "proj"},
                             events[0]["timestamp"])
    return store


def _reparse(env, store, **kw):
    return run_reparse_opencode(store, db_path=env["db"], backup_dir=env["backups"],
                                opencode_db=env["oc"], now=NOW, **kw)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rows(store, sid):
    return sorted(tuple(r) for r in store._get_conn().execute(
        "SELECT event_type, summary, file_path FROM events "
        "WHERE provider = 'opencode' AND session_id = ?", (sid,)))


class TestReparseOpencode:
    def test_dry_run_counts_exactly_and_writes_nothing(self, env):
        _session(env["oc"], "ses_old")
        _session(env["oc"], "ses_new")
        _store_legacy(env, ["ses_old"]).close()
        digest = _sha(env["db"])

        rep = _reparse(env, None, dry_run=True)
        assert rep.dry_run and rep.source_sessions == 2 and rep.stored_sessions == 1
        assert rep.sessions == 2 and rep.new_sessions == 1
        assert rep.events_to_delete == 4      # ses_old's 4 stored rows
        assert rep.events_to_insert == 8      # 4 per session
        assert rep.backup_path == ""
        assert _sha(env["db"]) == digest
        assert not env["backups"].exists()

    def test_without_yes_nothing_is_written(self, env):
        _session(env["oc"], "ses_old")
        store = _store_legacy(env, ["ses_old"])
        before = _rows(store, "ses_old")
        rep = _reparse(env, store, dry_run=False, yes=False)
        assert rep.needs_yes and rep.dry_run
        assert _rows(store, "ses_old") == before
        assert not env["backups"].exists()
        store.close()

    def test_real_run_restores_text_backs_up_and_recovers_missing_session(self, env):
        _session(env["oc"], "ses_old")
        _session(env["oc"], "ses_new")
        store = _store_legacy(env, ["ses_old"])
        assert ("assistant", "[text]", None) in _rows(store, "ses_old")

        rep = _reparse(env, store, dry_run=False, yes=True)
        assert not rep.dry_run and rep.failed == 0
        assert rep.backup_path and Path(rep.backup_path).exists()
        backup = sqlite3.connect(rep.backup_path)
        assert backup.execute(
            "SELECT COUNT(*) FROM events WHERE summary = '[text]'").fetchone()[0] == 1
        backup.close()

        expected = sorted([
            ("user", "please review the module", None),
            ("thinking", "reading the module first", None),
            ("tool_use", "module body", "/Users/u/work/proj/src/mod.py"),
            ("assistant", "the module looks fine", None),
        ])
        assert _rows(store, "ses_old") == expected
        assert _rows(store, "ses_new") == expected
        assert rep.new_sessions == 1 and rep.events_inserted == 8

        conn = store._get_conn()
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE provider = 'opencode' AND historical = 0"
        ).fetchone()[0] == 0
        full = conn.execute(
            "SELECT c.full_text FROM events e JOIN event_content c ON c.event_id = e.id "
            "WHERE e.session_id = 'ses_new' AND e.event_type = 'assistant'").fetchone()[0]
        assert full == "the module looks fine"
        sess = conn.execute(
            "SELECT event_count FROM sessions WHERE id = 'ses_new' AND provider = 'opencode'"
        ).fetchone()
        assert sess == (4,)
        store.close()

    def test_second_run_changes_nothing(self, env):
        _session(env["oc"], "ses_old")
        _session(env["oc"], "ses_new")
        store = _store_legacy(env, ["ses_old"])
        _reparse(env, store, dry_run=False, yes=True)
        snapshot = sorted(tuple(r) for r in store._get_conn().execute(
            "SELECT id, fingerprint FROM events"))
        sessions = store._get_conn().execute(
            "SELECT * FROM sessions ORDER BY id").fetchall()

        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.sessions == 0 and rep.up_to_date == 2
        assert rep.events_to_delete == 0 and rep.events_inserted == 0
        assert rep.backup_path == ""
        assert sorted(tuple(r) for r in store._get_conn().execute(
            "SELECT id, fingerprint FROM events")) == snapshot
        assert store._get_conn().execute(
            "SELECT * FROM sessions ORDER BY id").fetchall() == sessions
        store.close()

    def test_session_with_recent_activity_is_left_to_the_daemon(self, env):
        _session(env["oc"], "ses_live", updated=LIVE_MS)
        store = _store_legacy(env, ["ses_live"])
        before = _rows(store, "ses_live")
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.in_window == 1 and rep.sessions == 0
        assert _rows(store, "ses_live") == before
        store.close()

    def test_streaming_part_in_an_old_session_is_reparsed_with_its_content(self, env):
        _session(env["oc"], "ses_cut")
        c = sqlite3.connect(env["oc"])
        c.execute("INSERT INTO part VALUES ('ses_cut_px', 'ses_cut_ma', 'ses_cut', ?, ?, ?)",
                  (OLD_MS + 50, OLD_MS + 50,
                   json.dumps({"type": "text", "text": "cut short",
                               "time": {"start": 1}})))
        c.commit()
        c.close()
        EventStore(db_path=env["db"]).close()
        rep = _reparse(env, None, dry_run=True)
        assert rep.new_sessions == 1
        assert rep.events_to_insert == 5  # the stale streaming part counts

    def test_session_gone_from_opencode_db_is_untouched(self, env):
        _session(env["oc"], "ses_kept")
        store = _store_legacy(env, ["ses_kept"])
        store.store_with_offset(
            [{"provider": "opencode", "project": "proj", "event_type": "user",
              "timestamp": "2026-01-01T00:00:00+00:00", "summary": "old prompt",
              "session_id": "ses_gone"}], "", "opencode", str(env["oc"]), 0)
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.no_rollout == 1
        assert _rows(store, "ses_gone") == [("user", "old prompt", None)]
        store.close()

    def test_daemon_cursor_is_not_touched(self, env):
        _session(env["oc"], "ses_old")
        store = _store_legacy(env, ["ses_old"])
        store.store_with_offset([], "opencode:key", "opencode", str(env["oc"]), 3)
        _reparse(env, store, dry_run=False, yes=True)
        assert store._get_conn().execute(
            "SELECT last_offset FROM file_registry WHERE fingerprint = 'opencode:key'"
        ).fetchone() == (3,)
        store.close()


class TestReparseOpencodeCli:
    def test_cli_without_yes_only_simulates(self, env, monkeypatch, capsys):
        from hub.cli import cmd_backfill
        _session(env["oc"], "ses_old")
        _store_legacy(env, ["ses_old"]).close()
        digest = _sha(env["db"])
        monkeypatch.setattr(es, "DEFAULT_DB_PATH", env["db"])
        monkeypatch.setattr(ow, "_DEFAULT_DB", env["oc"])
        args = argparse.Namespace(provider="all", since=None, dry_run=False, limit=None,
                                  verbose=False, reparse="opencode", yes=False)
        cmd_backfill(args)
        out = capsys.readouterr().out
        assert "Re-parseo de OpenCode" in out and "simulación" in out
        assert "--reparse opencode --yes" in out
        assert _sha(env["db"]) == digest
