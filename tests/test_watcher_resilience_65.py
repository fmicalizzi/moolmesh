"""Tests for issue #65 — Codex sub-agents, store hygiene, watcher resilience.

Everything is synthetic and deterministic: no real provider data, no thread
left running, and the wall-clock guardrail of tests/conftest.py applies.
"""

from __future__ import annotations

import collections
import http.server
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore
from hub.dashboard.server import DashboardServer
from hub.watchers.base import BaseHarvester
from hub.watchers.codex_watcher import CodexWatcher
from hub.watchers.opencode_watcher import OpenCodeWatcher

FIXTURES = Path(__file__).parent / "fixtures"
SUBAGENT_FIXTURE = FIXTURES / "codex_subagent_sample.jsonl"

PARENT = "bbbbbbbb-1111-4222-8333-444444444444"
CHILD = "cccccccc-5555-4666-8777-888888888888"


def _harvest_all(watcher) -> None:
    """Run one live pass: rescan + harvest every watched file (no thread)."""
    watcher._rescan()
    for path, fp in list(watcher._watched_files.items()):
        watcher._harvest_file(path, fp)


class TestCodexSubagent:
    """session_meta with dict source / parent link / defensive scalars (#65)."""

    def test_parser_normalizes_source_and_parent(self):
        from hub.parsers.codex_parser import CodexParser

        entries = CodexParser().parse_file(SUBAGENT_FIXTURE)
        meta = [e for e in entries if e.event_type == "session_meta"]
        assert meta[0].session_id == CHILD
        assert meta[0].source == "subagent:thread_spawn"
        assert meta[0].parent_session_id == PARENT
        assert meta[0].agent_meta == {
            "agent_path": "/root/module-a", "agent_nickname": "Scout", "depth": 1,
        }

    def test_defensive_scalar_coercion(self):
        from hub.parsers.codex_parser import _scalar, _source_label

        assert _scalar({"a": 1}) == '{"a":1}'
        assert _scalar(["x"]) == '["x"]'
        assert _scalar(None) == ""
        assert _scalar(7) == "7"
        assert _source_label({"subagent": {"thread_spawn": {}}}) == "subagent:thread_spawn"
        assert _source_label("cli") == "cli"
        assert _source_label(None) == ""

    def test_first_session_meta_wins_file_identity(self):
        """The replayed parent meta must not steal the sub-agent's events."""
        from hub.parsers.codex_parser import CodexParser

        entries = CodexParser().parse_file(SUBAGENT_FIXTURE)
        non_meta = [e for e in entries if e.event_type != "session_meta"]
        assert non_meta
        assert {e.session_id for e in non_meta} == {CHILD}
        assert {e.source for e in non_meta} == {"subagent:thread_spawn"}

    def test_watcher_ingests_subagent_session_and_links_parent(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            watcher = CodexWatcher(store, collections.deque())
            watcher._file_projects[SUBAGENT_FIXTURE] = "project-alpha"
            events, offset = watcher._parse_and_adapt(SUBAGENT_FIXTURE, 0)
            assert events
            assert offset > 0
            store.store_with_offset(
                events, watcher.registry_key(SUBAGENT_FIXTURE), "codex",
                str(SUBAGENT_FIXTURE), offset,
            )

            detail = store.get_session_detail(CHILD)
            assert detail is not None
            assert detail["source"] == "subagent:thread_spawn"
            assert detail["metadata"]["parent_session_id"] == PARENT
            assert detail["event_count"] >= 8

            rows = store.get_session_events(CHILD)
            assert len(rows) == detail["event_count"]
            assert any(
                r["file_path"] and r["file_path"].endswith("src/module_a.py")
                for r in rows
            ), "patch path must survive the sub-agent format"

            chain = store.get_session_chain(CHILD)
            assert [(c["session_id"], c["link_type"]) for c in chain] == [
                (PARENT, "subagent")
            ]
            assert store._conn.in_transaction is False
        finally:
            store.close()


class TestEventStoreHygiene:
    """Every failed write leaves the shared connection outside a transaction."""

    def test_failed_upsert_rolls_back_and_next_write_works(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            conn = store._conn
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'forced'); END"
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError):
                store.upsert_session(
                    {"id": "s1", "provider": "codex", "project": "p"}, "t"
                )
            assert conn.in_transaction is False

            conn.execute("DROP TRIGGER boom")
            conn.commit()
            stored = store.store_with_offset(
                [{
                    "provider": "codex", "project": "p", "event_type": "user",
                    "timestamp": "t2", "summary": "after failure", "session_id": "s1",
                }],
                "fp1", "codex", "/tmp/x.jsonl", 10,
            )
            assert len(stored) == 1
            assert conn.in_transaction is False
        finally:
            store.close()

    def test_leaked_transaction_is_cleared_by_next_write(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            conn = store._conn
            # Simulate a previous half-done write: implicit transaction open.
            conn.execute(
                "INSERT INTO events (provider, project, event_type, timestamp,"
                " summary, created_at) VALUES ('codex', 'p', 'user', 't', 'leak', 1.0)"
            )
            assert conn.in_transaction is True

            store.upsert_session(
                {"id": "s1", "provider": "codex", "project": "p"}, "t"
            )
            assert conn.in_transaction is False
            assert store.count() == 0  # the leaked insert rolled back
        finally:
            store.close()

    def test_non_scalar_event_fields_are_coerced(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            store.store({
                "provider": "codex", "project": {"name": "p"}, "event_type": "user",
                "timestamp": "t", "summary": ["a", "b"], "session_id": "s1",
                "tool_name": None,
            })
            row = store._conn.execute(
                "SELECT project, summary FROM events WHERE session_id = 's1'"
            ).fetchone()
            assert row == ('{"name":"p"}', '["a","b"]')
        finally:
            store.close()


class _FakeHarvester(BaseHarvester):
    """One poisoned file + one healthy file, parsed without touching disk."""

    POLL_INTERVAL = 0.01
    RESCAN_INTERVAL = 0.05
    QUARANTINE_AFTER = 3
    QUARANTINE_BACKOFF = 30.0

    def __init__(self, store, files):
        super().__init__(store, collections.deque())
        self._files = list(files)
        self.parse_calls: dict[Path, int] = {}

    @property
    def provider_name(self) -> str:
        return "fake"

    def discover_files(self, since=None, skip_dir=None) -> list[Path]:
        return list(self._files)

    def _parse_and_adapt(self, path: Path, offset: int):
        self.parse_calls[path] = self.parse_calls.get(path, 0) + 1
        if path.name == "bad.jsonl":
            raise ValueError("poison record")
        if offset == 0:
            return ([{
                "provider": "fake", "project": "p", "event_type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "summary": f"event from {path.name}", "session_id": "s1",
            }], 10)
        return [], offset


def _fake_files(tmp_path: Path) -> tuple[Path, Path]:
    bad = tmp_path / "bad.jsonl"
    good = tmp_path / "good.jsonl"
    bad.write_text("poison\n")
    good.write_text("healthy\n")
    return bad, good


class TestHarvestFileResilience:
    def test_bad_file_never_kills_the_loop(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            bad, good = _fake_files(tmp_path)
            w = _FakeHarvester(store, [bad, good])
            w.POLL_INTERVAL = 0.01
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and store.count() == 0:
                time.sleep(0.02)
            alive = w.alive
            w.stop()
            assert alive, "the watcher thread must survive a poisoned file"
            assert store.count() >= 1, "the healthy file must still ingest"
            assert w.alive is False  # stop() joined it
        finally:
            store.close()

    def test_error_logged_once_and_file_quarantined(self, tmp_path, caplog):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            bad, good = _fake_files(tmp_path)
            w = _FakeHarvester(store, [bad, good])
            bad_fp = w.registry_key(bad)
            with caplog.at_level(logging.WARNING, logger="moolmesh.watcher"):
                for _ in range(6):
                    w._harvest_file(bad, bad_fp)
            messages = [r.getMessage() for r in caplog.records]
            assert sum("harvest error" in m for m in messages) == 1
            assert any("quarantined" in m for m in messages)
            assert w.last_error == {"type": "ValueError", "file": str(bad)}
            assert w.quarantined_files == 1
            # Quarantined: no further parse attempts while the backoff holds.
            calls = w.parse_calls[bad]
            w._harvest_file(bad, bad_fp)
            assert w.parse_calls[bad] == calls
        finally:
            store.close()

    def test_quarantine_expires_and_retries(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            bad, good = _fake_files(tmp_path)
            w = _FakeHarvester(store, [bad, good])
            bad_fp = w.registry_key(bad)
            for _ in range(w.QUARANTINE_AFTER):
                w._harvest_file(bad, bad_fp)
            assert w.quarantined_files == 1
            w._quarantine_until[bad] = time.time() - 1  # backoff expired
            w._harvest_file(bad, bad_fp)
            assert w.parse_calls[bad] == w.QUARANTINE_AFTER + 1
            assert w.quarantined_files == 1  # re-quarantined on failure
        finally:
            store.close()

    def test_snapshot_shape_and_masking(self, tmp_path, monkeypatch):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            bad, good = _fake_files(tmp_path)
            w = _FakeHarvester(store, [bad, good])
            w._harvest_file(bad, w.registry_key(bad))
            snap = w.status_snapshot()
            assert set(snap) == {
                "alive", "running", "stalled", "last_cycle_at", "last_error",
                "quarantined_files",
            }
            assert snap["last_error"]["file"] == str(bad)
            masked = w.status_snapshot(hide_project_names=True)
            assert masked["last_error"]["file"].startswith("hidden:")
            assert str(bad) not in masked["last_error"]["file"]
        finally:
            store.close()


class _StubWatcher:
    """Supervisor/health stub with a controllable thread state."""

    def __init__(self, alive: bool = False, running: bool = True):
        self.provider_name = "stub"
        self.running = running
        self._alive = alive
        self.starts = 0
        self.death_noted = False

    @property
    def alive(self):
        return self._alive

    def note_thread_death(self):
        self.death_noted = True

    def start(self):
        self.starts += 1
        self._alive = True

    def status_snapshot(self, *, hide_project_names: bool = False):
        return {
            "alive": self._alive, "running": self.running, "stalled": False,
            "last_cycle_at": None, "last_error": None, "quarantined_files": 0,
        }


def _bare_dashboard() -> DashboardServer:
    srv = object.__new__(DashboardServer)
    srv.watchers = []
    srv._supervisor_stop = threading.Event()
    srv._supervisor_thread = None
    srv.SUPERVISOR_INTERVAL = 0.01
    return srv


class TestSupervisor:
    def test_dead_thread_is_restarted_and_recorded(self):
        srv = _bare_dashboard()
        watcher = _StubWatcher(alive=False, running=True)
        srv.watchers = [("Stub", watcher)]
        t = threading.Thread(target=srv._supervise_watchers, daemon=True)
        t.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and watcher.starts == 0:
                time.sleep(0.01)
        finally:
            srv._supervisor_stop.set()
            t.join(timeout=5)
        assert watcher.starts == 1
        assert watcher.death_noted is True

    def test_stopped_watcher_is_not_restarted(self):
        srv = _bare_dashboard()
        watcher = _StubWatcher(alive=False, running=False)
        srv.watchers = [("Stub", watcher)]
        t = threading.Thread(target=srv._supervise_watchers, daemon=True)
        t.start()
        time.sleep(0.05)
        srv._supervisor_stop.set()
        t.join(timeout=5)
        assert watcher.starts == 0

    def test_real_thread_killed_by_base_exception_is_restarted(self, tmp_path):
        """The loop guard catches Exception; a thread that still exits leaves
        ``running`` True with a dead thread — the supervisor must notice and
        start a fresh one (#65)."""

        class _DyingHarvester(_FakeHarvester):
            def __init__(self, store, files):
                super().__init__(store, files)
                self.starts = 0

            def start(self):
                self.starts += 1
                super().start()

            def _harvest_loop(self):  # pragma: no cover - exits at once
                return

        store = EventStore(db_path=tmp_path / "events.db")
        try:
            bad, good = _fake_files(tmp_path)
            watcher = _DyingHarvester(store, [bad, good])
            watcher.start()
            deadline = time.time() + 5
            while time.time() < deadline and watcher.alive:
                time.sleep(0.01)
            assert watcher.alive is False

            srv = _bare_dashboard()
            srv.watchers = [("Dying", watcher)]
            t = threading.Thread(target=srv._supervise_watchers, daemon=True)
            t.start()
            try:
                deadline = time.time() + 5
                while time.time() < deadline and watcher.starts < 2:
                    time.sleep(0.01)
                assert watcher.starts >= 2
                assert watcher.last_error == {"type": "ThreadDied", "file": ""}
                deadline = time.time() + 5
                while time.time() < deadline and not srv.watchers_health()[1]:
                    time.sleep(0.01)
                assert srv.watchers_health()[1] is True  # running + dead
            finally:
                srv._supervisor_stop.set()
                t.join(timeout=5)
                watcher.stop()
        finally:
            store.close()


class TestHealthDegraded:
    def test_healthy_when_all_watchers_alive(self):
        srv = _bare_dashboard()
        srv.watchers = [("Stub", _StubWatcher(alive=True))]
        watchers, degraded = srv.watchers_health()
        assert degraded is False
        assert watchers["stub"]["alive"] is True

    def test_degraded_when_thread_dead(self):
        srv = _bare_dashboard()
        srv.watchers = [("Stub", _StubWatcher(alive=False))]
        _, degraded = srv.watchers_health()
        assert degraded is True

    def test_degraded_when_stalled(self):
        srv = _bare_dashboard()

        class _Stalled(_StubWatcher):
            def status_snapshot(self, *, hide_project_names=False):
                snap = super().status_snapshot(hide_project_names=hide_project_names)
                snap["stalled"] = True
                return snap

        srv.watchers = [("Stub", _Stalled(alive=True))]
        _, degraded = srv.watchers_health()
        assert degraded is True

    def test_stopped_watcher_does_not_degrade(self):
        srv = _bare_dashboard()
        srv.watchers = [("Stub", _StubWatcher(alive=False, running=False))]
        _, degraded = srv.watchers_health()
        assert degraded is False

    def test_http_health_reports_watchers_additively(self):
        srv = _bare_dashboard()
        srv._start_time = time.monotonic()
        srv.stats = {"total_events": 7}
        srv.watchers = [("Stub", _StubWatcher(alive=False))]
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                health = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        assert health["status"] == "degraded"
        assert health["events_count"] == 7
        assert health["watchers"]["stub"]["alive"] is False

    def test_http_health_stays_healthy_normally(self):
        srv = _bare_dashboard()
        srv._start_time = time.monotonic()
        srv.stats = {"total_events": 0}
        srv.watchers = [("Stub", _StubWatcher(alive=True))]
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                health = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        assert health["status"] == "healthy"
        assert set(health) >= {
            "status", "pid", "version", "uptime_seconds", "events_count", "watchers",
        }

    def test_moolmesh_answers_on_accepts_degraded(self, monkeypatch):
        srv = _bare_dashboard()
        srv.host = "127.0.0.1"

        class _Resp:
            def __init__(self, body): self._body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._body

        for status, expected in (("healthy", True), ("degraded", True), ("other", False)):
            monkeypatch.setattr(
                urllib.request, "urlopen",
                lambda *a, s=status, **k: _Resp(json.dumps({"status": s}).encode()),
            )
            assert srv._moolmesh_answers_on(5200) is expected


class TestDaemonStatusExitCode:
    def _patch(self, monkeypatch, status: str):
        from hub import daemon as daemon_mod

        monkeypatch.setattr(
            daemon_mod, "daemon_status",
            lambda: {"pid": 4242, "uptime_seconds": 5, "log_size": 0},
        )

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({
                    "status": status,
                    "events_count": 3,
                    "version": "0.0.0",
                    "watchers": {
                        "codex": {
                            "alive": status == "healthy", "running": True,
                            "stalled": False, "last_cycle_at": None,
                            "last_error": None if status == "healthy" else {
                                "type": "ProgrammingError", "file": "hidden:x",
                            },
                            "quarantined_files": 0,
                        },
                    },
                }).encode()

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    def test_status_exits_nonzero_when_degraded(self, monkeypatch, capsys):
        import hub.cli as cli

        self._patch(monkeypatch, "degraded")
        monkeypatch.setattr("sys.argv", ["mool", "daemon", "status"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "degraded" in out
        assert "codex" in out

    def test_status_exits_zero_when_healthy(self, monkeypatch, capsys):
        import hub.cli as cli

        self._patch(monkeypatch, "healthy")
        monkeypatch.setattr("sys.argv", ["mool", "daemon", "status"])
        cli.main()  # no SystemExit
        out = capsys.readouterr().out
        assert "healthy" in out

    def test_status_json_includes_watchers(self, monkeypatch, capsys):
        import hub.cli as cli

        self._patch(monkeypatch, "degraded")
        monkeypatch.setattr("sys.argv", ["mool", "status", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "degraded"
        assert "codex" in payload["watchers"]


def _codex_rollout_dir(base: Path) -> Path:
    sessions = base / "sessions" / "2026" / "10" / "07"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


class TestRecoveryAfterUpgrade:
    def test_codex_resumes_from_offset_and_ingests_subagent(self, tmp_path):
        base = tmp_path / "codex"
        rollout = _codex_rollout_dir(base) / "rollout-2026-10-07T07-30-06-fixture.jsonl"
        shutil.copyfile(SUBAGENT_FIXTURE, rollout)

        db = tmp_path / "events.db"
        store1 = EventStore(db_path=db)
        w1 = CodexWatcher(store1, collections.deque(), codex_base=base)
        _harvest_all(w1)
        first_count = store1.count()
        assert first_count > 0
        assert store1.get_session_detail(CHILD)["event_count"] == first_count - 1
        store1.close()  # daemon goes down

        # New data lands while the daemon is down.
        tail = json.dumps({
            "timestamp": "2026-10-07T07:40:00.000Z", "ordinal": 99,
            "type": "response_item",
            "payload": {"type": "message", "id": "msg-tail", "role": "user",
                        "content": [{"type": "input_text", "text": "tail message"}]},
        })
        with open(rollout, "a") as fh:
            fh.write(tail + "\n")

        store2 = EventStore(db_path=db)
        try:
            w2 = CodexWatcher(store2, collections.deque(), codex_base=base)
            _harvest_all(w2)
            assert store2.count() == first_count + 1  # exactly the tail, no dupes
            assert store2.get_session_detail(CHILD)["event_count"] == first_count

            # A third pass adds nothing (offset held, no duplication).
            w3 = CodexWatcher(store2, collections.deque(), codex_base=base)
            _harvest_all(w3)
            assert store2.count() == first_count + 1
        finally:
            store2.close()

    def test_opencode_resumes_from_rowid_without_loss_or_dupes(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        _create_opencode_db(db_path, num_parts=3)
        events_db = tmp_path / "events.db"

        store1 = EventStore(db_path=events_db)
        w1 = OpenCodeWatcher(store1, None, opencode_db=db_path)
        _harvest_all(w1)
        first_count = store1.count()
        assert first_count > 0
        key = w1.registry_key(db_path)
        store1.close()

        _create_opencode_db(db_path, num_parts=5)  # append two more parts

        store2 = EventStore(db_path=events_db)
        try:
            w2 = OpenCodeWatcher(store2, None, opencode_db=db_path)
            assert store2.get_offset(key, str(db_path)) is not None
            _harvest_all(w2)
            assert store2.count() > first_count
            after = store2.count()
            w3 = OpenCodeWatcher(store2, None, opencode_db=db_path)
            _harvest_all(w3)
            assert store2.count() == after  # no duplication
        finally:
            store2.close()

    def test_catchup_recovers_files_outside_the_live_window(self, tmp_path):
        base = tmp_path / "codex"
        rollout = _codex_rollout_dir(base) / "rollout-2026-10-05T07-30-06-old.jsonl"
        shutil.copyfile(SUBAGENT_FIXTURE, rollout)
        old = time.time() - 2 * 86400
        os.utime(rollout, (old, old))

        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        try:
            store.set_watcher_cycle("codex", time.time() - 3 * 86400)
            watcher = CodexWatcher(store, collections.deque(), codex_base=base)
            watcher._plan_catchup()
            assert list(watcher._catchup_queue) == [rollout]
            watcher._running = True
            watcher._drain_catchup(10)
            count = store.count()
            assert count > 0
            assert store.get_session_detail(CHILD) is not None

            # The next start has a fresh heartbeat: no re-queue, no dupes.
            watcher2 = CodexWatcher(store, collections.deque(), codex_base=base)
            watcher2._plan_catchup()
            assert not watcher2._catchup_queue
            assert store.count() == count
        finally:
            store.close()


def _create_opencode_db(db_path: Path, num_parts: int) -> None:
    """Create/append a minimal OpenCode DB (mirrors test_opencode_watcher)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);
            CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, model TEXT, cost REAL, project_id TEXT);
            CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER);
            CREATE TABLE IF NOT EXISTS part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
        """)
        conn.execute("INSERT OR IGNORE INTO project VALUES ('proj1', 'test-project', '/tmp/test')")
        conn.execute("INSERT OR IGNORE INTO session VALUES ('ses1', '/tmp/test', 'Test Session', '{}', 0.0, 'proj1')")
        existing = conn.execute("SELECT COUNT(*) FROM part").fetchone()[0]
        for i in range(existing, num_parts):
            role = "user" if i % 3 == 0 else "assistant"
            conn.execute(
                "INSERT OR REPLACE INTO message VALUES (?, ?, ?)",
                (f"msg{i}", json.dumps({"role": role}), 1700000000000 + i * 1000),
            )
            conn.execute(
                "INSERT OR REPLACE INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (f"part{i}", f"msg{i}", "ses1", 1700000000000 + i * 1000,
                 1700000000000 + i * 1000,
                 json.dumps({"type": "text", "content": f"Message {i} content"})),
            )
        conn.commit()
    finally:
        conn.close()
