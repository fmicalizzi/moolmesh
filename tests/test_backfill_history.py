"""Tests for the real historical backfill — ``mool backfill`` (issue #45)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hub import backfill as backfill_mod
from hub.backfill import run_backfill
from hub.cache.event_store import EventStore
from hub.watchers.claude_watcher import ClaudeWatcher

DAY = 86400


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _claude_file(base: Path, project: str, sid: str, n_msgs: int = 3,
                 day: str = "2026-03-15", age: float = 3 * DAY) -> Path:
    proj = base / f"-Users-test-{project}"
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(n_msgs):
        lines.append(json.dumps({
            "type": "user", "sessionId": sid, "cwd": f"/Users/test/{project}",
            "uuid": f"{sid}-{i}", "timestamp": f"{day}T10:{i:02d}:00.000Z",
            "message": {"role": "user", "content": f"message {i} of {sid}"},
        }))
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(lines) + "\n")
    _age(f, age)
    return f


def _codex_file(codex: Path, sid: str, age: float = 3 * DAY) -> Path:
    d = codex / "sessions" / "2026" / "03" / "25"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "timestamp": "2026-03-25T10:00:00Z",
         "payload": {"id": sid, "cwd": "/Users/test/cx", "cli_version": "0.1",
                     "model_provider": "openai", "source": "cli"}},
        {"type": "event_msg", "timestamp": "2026-03-25T10:00:05Z",
         "payload": {"type": "user_message", "message": f"hello from {sid}"}},
        {"type": "response_item", "timestamp": "2026-03-25T10:00:10Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": f"answer {sid}"}]}},
    ]
    f = d / f"rollout-2026-03-25T10-00-00-{sid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    _age(f, age)
    return f


@pytest.fixture
def env(tmp_path):
    claude = tmp_path / "claude"
    codex = tmp_path / ".codex"
    qwen = tmp_path / "qwen"
    claude.mkdir()
    qwen.mkdir()
    db = tmp_path / "moolmesh" / "events.db"
    return {
        "tmp": tmp_path, "claude": claude, "codex": codex, "qwen": qwen, "db": db,
        "bases": {"claude": claude, "codex": codex, "qwen": qwen},
    }


def _run(env, store, **kw):
    kw.setdefault("bases", env["bases"])
    kw.setdefault("db_path", env["db"])
    return run_backfill(store, **kw)


def _count(db: Path, sql: str, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _by(report, provider):
    return next(r for r in report.providers if r.provider == provider)


class TestBackfillIngestsHistory:
    def test_processes_old_files_the_watcher_ignores(self, env):
        _claude_file(env["claude"], "alpha", "s-old-1")
        store = EventStore(db_path=env["db"])
        live = ClaudeWatcher(store, claude_base=env["claude"])
        assert live.discover_files() == []  # outside the 12 h window

        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.processed == 1
        assert rep.events_inserted == 3
        assert rep.new_sessions == 1
        row = store._conn.execute(
            "SELECT event_count, first_event_at, last_event_at FROM sessions "
            "WHERE id='s-old-1'"
        ).fetchone()
        # Stats refreshed after the one-pass ingest (not the pre-insert count).
        assert row[0] == 3
        assert row[1] == "2026-03-15T10:00:00.000Z"
        assert row[2] == "2026-03-15T10:02:00.000Z"
        store.close()

    def test_rerun_inserts_nothing(self, env):
        _claude_file(env["claude"], "alpha", "s1")
        _codex_file(env["codex"], "cx-1")
        store = EventStore(db_path=env["db"])
        first = _run(env, store)
        assert sum(r.events_inserted for r in first.providers) > 0
        second = _run(env, store)
        assert sum(r.events_inserted for r in second.providers) == 0
        assert sum(r.processed for r in second.providers) == 0
        assert _by(second, "claude").up_to_date == 1
        assert _by(second, "codex").up_to_date == 1
        store.close()

    def test_limit_counts_only_files_with_new_data_and_resumes(self, env):
        for i in range(3):
            _claude_file(env["claude"], "alpha", f"s{i}", age=(5 - i) * DAY)
        store = EventStore(db_path=env["db"])
        totals = []
        for _ in range(3):
            rep = _by(_run(env, store, providers=["claude"], limit=1), "claude")
            totals.append(rep.processed)
        assert totals == [1, 1, 1]
        last = _by(_run(env, store, providers=["claude"], limit=1), "claude")
        assert last.processed == 0 and last.up_to_date == 3
        assert _count(env["db"], "SELECT COUNT(*) FROM events") == 9
        store.close()

    def test_since_and_provider_filter(self, env):
        _claude_file(env["claude"], "alpha", "recentish", age=2 * DAY)
        _claude_file(env["claude"], "alpha", "ancient", age=40 * DAY)
        _codex_file(env["codex"], "cx-1", age=2 * DAY)
        store = EventStore(db_path=env["db"])
        since = time.time() - 10 * DAY
        report = _run(env, store, providers=["claude"], since=since)
        assert [r.provider for r in report.providers] == ["claude"]
        assert _by(report, "claude").seen == 1
        sids = {r[0] for r in store._conn.execute("SELECT DISTINCT session_id FROM events")}
        assert sids == {"recentish"}
        store.close()

    def test_files_inside_daemon_window_are_left_to_the_daemon(self, env):
        _claude_file(env["claude"], "alpha", "live", age=3600)  # 1 h old
        _claude_file(env["claude"], "alpha", "old")
        store = EventStore(db_path=env["db"])
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.in_window == 1 and rep.processed == 1
        sids = {r[0] for r in store._conn.execute("SELECT DISTINCT session_id FROM events")}
        assert sids == {"old"}
        store.close()

    def test_bad_file_never_aborts_the_run(self, env, monkeypatch):
        _claude_file(env["claude"], "alpha", "a-good", age=5 * DAY)
        bad = _claude_file(env["claude"], "alpha", "b-bad", age=4 * DAY)
        _claude_file(env["claude"], "alpha", "c-good", age=3 * DAY)
        garbage = env["claude"] / "-Users-test-alpha" / "d-garbage.jsonl"
        garbage.write_bytes(b"\x00\xffnot json\n{broken\n")
        _age(garbage, 2 * DAY)

        real = ClaudeWatcher._parse_and_adapt

        def flaky(self, path, offset):
            if path == bad:
                raise ValueError("corrupt")
            return real(self, path, offset)

        monkeypatch.setattr(ClaudeWatcher, "_parse_and_adapt", flaky)
        store = EventStore(db_path=env["db"])
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.skipped_error == 1
        sids = {r[0] for r in store._conn.execute("SELECT DISTINCT session_id FROM events")}
        assert sids == {"a-good", "c-good"}
        store.close()

    def test_cloud_placeholders_are_skipped_without_opening(self, env, monkeypatch):
        cloud = _claude_file(env["claude"], "alpha", "in-cloud")
        _claude_file(env["claude"], "alpha", "local")
        monkeypatch.setattr(backfill_mod, "is_cloud_placeholder", lambda p: Path(p) == cloud)
        real_fp = backfill_mod.file_fingerprint

        def guarded_fp(p):
            assert Path(p) != cloud, "placeholder was opened"
            return real_fp(p)

        monkeypatch.setattr(backfill_mod, "file_fingerprint", guarded_fp)
        store = EventStore(db_path=env["db"])
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.skipped_cloud == 1 and rep.processed == 1
        store.close()

    def test_empty_files_are_skipped(self, env):
        proj = env["claude"] / "-Users-test-alpha"
        proj.mkdir(parents=True)
        empty = proj / "empty.jsonl"
        empty.write_text("")
        _age(empty, 3 * DAY)
        store = EventStore(db_path=env["db"])
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.skipped_empty == 1 and rep.processed == 0
        store.close()


class TestBackfillChunkingAndInterrupts:
    def test_large_file_is_written_in_bounded_chunks(self, env, monkeypatch):
        _claude_file(env["claude"], "alpha", "big", n_msgs=12)
        monkeypatch.setattr(ClaudeWatcher, "HISTORY_CHUNK_EVENTS", 5)
        store = EventStore(db_path=env["db"])
        calls = []
        real = store.store_with_offset

        def spy(events, fingerprint, *a, **kw):
            calls.append((len(events), bool(fingerprint)))
            return real(events, fingerprint, *a, **kw)

        monkeypatch.setattr(store, "store_with_offset", spy)
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.events_inserted == 12
        # Offset (fingerprint) only on the last chunk.
        assert calls == [(5, False), (5, False), (2, True)]
        store.close()

    def test_interrupt_mid_chunk_rolls_back_and_rerun_completes(self, env, monkeypatch):
        f = _claude_file(env["claude"], "alpha", "big", n_msgs=12)
        monkeypatch.setattr(ClaudeWatcher, "HISTORY_CHUNK_EVENTS", 5)
        store = EventStore(db_path=env["db"])
        real = store.store_with_offset
        n = {"calls": 0}

        def interrupting(events, fingerprint, *a, **kw):
            n["calls"] += 1
            if n["calls"] == 2:
                # Simulate Ctrl-C landing inside the second chunk's transaction.
                store._conn.execute("BEGIN IMMEDIATE")
                store._conn.execute(
                    "INSERT INTO events (provider, project, event_type, timestamp, "
                    "summary, created_at) VALUES ('claude','x','user','t','half',0)"
                )
                raise KeyboardInterrupt
            return real(events, fingerprint, *a, **kw)

        monkeypatch.setattr(store, "store_with_offset", interrupting)
        report = _run(env, store, providers=["claude"])
        assert report.interrupted
        assert not store._conn.in_transaction
        assert _count(env["db"], "SELECT COUNT(*) FROM events WHERE summary='half'") == 0
        assert _count(env["db"], "SELECT COUNT(*) FROM file_registry") == 0  # offset not saved
        assert _count(env["db"], "SELECT COUNT(*) FROM events") == 5  # first chunk kept

        monkeypatch.setattr(store, "store_with_offset", real)
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.events_inserted == 7  # the rest; the first 5 deduped
        assert _count(env["db"], "SELECT COUNT(*) FROM events") == 12
        assert _count(env["db"], "SELECT last_offset FROM file_registry") == f.stat().st_size
        store.close()

    def test_interrupt_between_files_resumes(self, env, monkeypatch):
        for i in range(3):
            _claude_file(env["claude"], "alpha", f"s{i}", age=(5 - i) * DAY)
        real = ClaudeWatcher.harvest_history_file
        n = {"calls": 0}

        def interrupting(self, path, fp):
            n["calls"] += 1
            if n["calls"] == 2:
                raise KeyboardInterrupt
            return real(self, path, fp)

        monkeypatch.setattr(ClaudeWatcher, "harvest_history_file", interrupting)
        store = EventStore(db_path=env["db"])
        assert _run(env, store, providers=["claude"]).interrupted
        monkeypatch.setattr(ClaudeWatcher, "harvest_history_file", real)
        rep = _by(_run(env, store, providers=["claude"]), "claude")
        assert rep.processed == 2 and rep.up_to_date == 1
        assert _count(env["db"], "SELECT COUNT(*) FROM events") == 9
        store.close()


class TestBackfillDryRun:
    def _digest(self, db: Path) -> str:
        return hashlib.sha256(db.read_bytes()).hexdigest()

    def test_dry_run_writes_nothing(self, env):
        _claude_file(env["claude"], "alpha", "s1")
        store = EventStore(db_path=env["db"])
        store.close()
        before = self._digest(env["db"])
        migrations = _count(env["db"], "SELECT COUNT(*) FROM schema_migrations")
        report = _run(env, None, dry_run=True)
        rep = _by(report, "claude")
        assert rep.processed == 1 and rep.pending_bytes > 0
        assert self._digest(env["db"]) == before
        assert _count(env["db"], "SELECT COUNT(*) FROM schema_migrations") == migrations
        assert _count(env["db"], "SELECT COUNT(*) FROM events") == 0

    def test_dry_run_without_db_does_not_create_it(self, env):
        _claude_file(env["claude"], "alpha", "s1")
        _run(env, None, dry_run=True)
        assert not env["db"].exists()

    def test_dry_run_respects_existing_offsets(self, env):
        _claude_file(env["claude"], "alpha", "s1")
        store = EventStore(db_path=env["db"])
        _run(env, store)
        store.close()
        rep = _by(_run(env, None, dry_run=True), "claude")
        assert rep.processed == 0 and rep.up_to_date == 1


class TestBackfillCli:
    def test_cli_dry_run_prints_summary(self, capsys):
        from hub.cli import cmd_backfill
        from hub.cache.event_store import DEFAULT_DB_PATH
        existed = DEFAULT_DB_PATH.exists()
        args = argparse.Namespace(provider="all", since=None, dry_run=True, limit=None,
                                  verbose=False)
        cmd_backfill(args)
        out = capsys.readouterr().out
        assert "simulación" in out and "CLAUDE" in out and "CODEX" in out
        assert DEFAULT_DB_PATH.exists() == existed

    def test_cli_rejects_bad_since(self):
        from hub.cli import _parse_since
        with pytest.raises(SystemExit):
            _parse_since("25/03/2026")


class TestUnreadableDb:
    def test_dry_run_fails_loudly_when_db_cannot_be_opened(self, env, monkeypatch):
        _claude_file(env["claude"], "alpha", "s1")
        EventStore(db_path=env["db"]).close()

        def broken(*a, **kw):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(backfill_mod.sqlite3, "connect", broken)
        with pytest.raises(backfill_mod.EventsDbUnreadable):
            _run(env, None, dry_run=True)
        with pytest.raises(backfill_mod.EventsDbUnreadable):
            backfill_mod.run_reparse_codex(None, dry_run=True, db_path=env["db"],
                                           bases=env["bases"])
