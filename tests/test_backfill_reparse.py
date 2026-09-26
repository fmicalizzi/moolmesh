"""Tests for ``mool backfill --reparse codex`` (issue #45)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import hub.cache.event_store as es
from hub.backfill import run_backfill, run_reparse_codex
from hub.cache.event_store import EventStore

DAY = 86400


def _rollout(codex: Path, name: str, sid: str, age: float = 3 * DAY,
             extra_meta_sid: str | None = None) -> Path:
    d = codex / "sessions" / "2026" / "03" / "25"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "timestamp": "2026-03-25T10:00:00Z",
         "payload": {"id": sid, "cwd": "/Users/test/cx", "cli_version": "0.1",
                     "model_provider": "openai", "source": "cli"}},
        {"type": "event_msg", "timestamp": "2026-03-25T10:00:05Z",
         "payload": {"type": "user_message", "message": f"hello {name}"}},
        {"type": "response_item", "timestamp": "2026-03-25T10:00:15Z",
         "payload": {"type": "function_call", "call_id": "fc1", "name": "shell",
                     "arguments": json.dumps({"command": f"cat /Users/test/cx/{name}.py"})}},
        {"type": "response_item", "timestamp": "2026-03-25T10:00:20Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": f"done {name}"}]}},
    ]
    if extra_meta_sid:
        lines.insert(0, {"type": "session_meta", "timestamp": "2026-03-25T09:59:00Z",
                         "payload": {"id": extra_meta_sid, "cwd": "/Users/test/cx"}})
    f = d / f"rollout-2026-03-25T10-00-00-{name}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    t = time.time() - age
    os.utime(f, (t, t))
    return f


@pytest.fixture
def env(tmp_path):
    codex = tmp_path / ".codex"
    db = tmp_path / "moolmesh" / "events.db"
    return {"codex": codex, "db": db, "bases": {"codex": codex},
            "backups": tmp_path / "moolmesh" / "backups"}


def _ingest(env) -> EventStore:
    store = EventStore(db_path=env["db"])
    run_backfill(store, providers=["codex"], bases=env["bases"], db_path=env["db"])
    return store


def _make_legacy(store: EventStore, sid: str) -> int:
    """Turn a session's rows into 'pre-#40' shape plus one stale row."""
    c = store._conn
    c.execute("UPDATE events SET file_path = NULL WHERE session_id = ?", (sid,))
    c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary, "
        "session_id, fingerprint, created_at) VALUES "
        "('codex','cx','tool_use','2026-03-25T10:00:15Z','LEGACY ROW',?,?,0)",
        (sid, f"legacy-{sid}"),
    )
    c.commit()
    return c.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (sid,)).fetchone()[0]


def _rows(store, sid):
    return store._conn.execute(
        "SELECT summary, file_path FROM events WHERE session_id = ? ORDER BY timestamp, summary",
        (sid,),
    ).fetchall()


def _reparse(env, store, **kw):
    kw.setdefault("db_path", env["db"])
    kw.setdefault("bases", env["bases"])
    kw.setdefault("backup_dir", env["backups"])
    return run_reparse_codex(store, **kw)


def _digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


class TestReparse:
    def test_dry_run_is_exact_and_writes_nothing(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        fresh = len(_rows(store, "sess-a"))
        stored = _make_legacy(store, "sess-a")
        store.close()
        before = _digest(env["db"])
        rep = _reparse(env, None, dry_run=True)
        assert (rep.sessions, rep.events_to_delete, rep.events_to_insert) == (1, stored, fresh)
        assert _digest(env["db"]) == before
        assert not env["backups"].exists()

    def test_without_yes_nothing_happens(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        _make_legacy(store, "sess-a")
        before = _rows(store, "sess-a")
        rep = _reparse(env, store, dry_run=False, yes=False)
        assert rep.needs_yes and rep.dry_run
        assert _rows(store, "sess-a") == before
        assert not env["backups"].exists()
        store.close()

    def test_reparse_replaces_old_rows_and_backs_up_first(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        fresh = _rows(store, "sess-a")
        assert any(fp for _, fp in fresh)  # current adapter extracts a path
        _make_legacy(store, "sess-a")

        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.groups == 1 and rep.failed == 0
        assert _rows(store, "sess-a") == fresh  # new adapter's rows, none old
        # Backup was taken before deleting: it still holds the legacy state.
        backup = Path(rep.backup_path)
        assert backup.parent == env["backups"]
        conn = sqlite3.connect(backup)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE summary = 'LEGACY ROW'"
        ).fetchone()[0] == 1
        conn.close()
        # Session metadata upserted, not duplicated; stats refreshed.
        cnt, n = store._conn.execute(
            "SELECT COUNT(*), MAX(event_count) FROM sessions WHERE id = 'sess-a'"
        ).fetchone()
        assert cnt == 1 and n == len(fresh)
        # No orphaned full-text rows.
        assert store._conn.execute(
            "SELECT COUNT(*) FROM event_content WHERE event_id NOT IN (SELECT id FROM events)"
        ).fetchone()[0] == 0
        store.close()

    def test_second_run_is_a_noop(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        _make_legacy(store, "sess-a")
        _reparse(env, store, dry_run=False, yes=True)
        ids = [r[0] for r in store._conn.execute("SELECT id FROM events ORDER BY id")]
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.groups == 0 and rep.up_to_date == 1 and rep.backup_path == ""
        assert [r[0] for r in store._conn.execute("SELECT id FROM events ORDER BY id")] == ids
        store.close()

    def test_freshly_backfilled_sessions_are_already_up_to_date(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        rep = _reparse(env, None, dry_run=True)
        assert rep.groups == 0 and rep.up_to_date == 1
        store.close()

    @pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
    def test_failure_mid_session_rolls_that_session_back(self, env, monkeypatch, exc):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        _make_legacy(store, "sess-a")
        before = _rows(store, "sess-a")
        registry = store._conn.execute("SELECT * FROM file_registry").fetchall()
        real = es._insert_event_row
        n = {"calls": 0}

        def failing(conn, e, now):
            n["calls"] += 1
            if n["calls"] == 2:
                raise exc
            return real(conn, e, now)

        monkeypatch.setattr(es, "_insert_event_row", failing)
        rep = _reparse(env, store, dry_run=False, yes=True)
        if isinstance(exc, KeyboardInterrupt):
            assert rep.interrupted
        else:
            assert rep.failed == 1
        assert not store._conn.in_transaction
        assert _rows(store, "sess-a") == before
        assert store._conn.execute("SELECT * FROM file_registry").fetchall() == registry
        store.close()

    def test_session_without_rollout_is_untouched(self, env):
        _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        store._conn.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary, "
            "session_id, fingerprint, created_at) VALUES "
            "('codex','cx','user','2026-01-01T00:00:00Z','ghost','ghost-sess','g1',0)"
        )
        store._conn.commit()
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.no_rollout == 1
        assert _rows(store, "ghost-sess") == [("ghost", None)]
        store.close()

    def test_rollout_inside_daemon_window_is_untouched(self, env):
        f = _rollout(env["codex"], "a", "sess-a")
        store = _ingest(env)
        _make_legacy(store, "sess-a")
        before = _rows(store, "sess-a")
        os.utime(f, None)  # touched now → the live daemon owns it
        rep = _reparse(env, store, dry_run=False, yes=True)
        assert rep.in_window == 1 and rep.groups == 0
        assert _rows(store, "sess-a") == before
        store.close()

    def test_session_split_across_rollouts_is_replaced_as_one_group(self, env):
        _rollout(env["codex"], "a", "sess-a")
        # A second rollout that also carries sess-a (e.g. a resumed thread).
        _rollout(env["codex"], "b", "sess-b", extra_meta_sid="sess-a")
        store = _ingest(env)
        _make_legacy(store, "sess-a")
        rep = _reparse(env, None, dry_run=True)
        assert rep.groups == 1 and rep.sessions == 2
        store.close()


class TestReparseCli:
    def test_cli_without_yes_only_simulates(self, capsys):
        from hub.cli import cmd_backfill
        args = argparse.Namespace(provider="all", since=None, dry_run=False, limit=None,
                                  verbose=False, reparse="codex", yes=False)
        cmd_backfill(args)
        out = capsys.readouterr().out
        assert "simulación" in out
