"""Tests for scheduled session→workspace attribution (issue #39).

Covers the incremental cursor pass (``WorkspaceStore.attribute_incremental``),
the full CLI backfill as the reset path, disk I/O kept outside the store lock,
single-transaction refresh of the derived tables, the ``WorkspaceAttributor``
background thread (via ``run_once`` — no real sleeps), and the dashboard wiring
+ meta endpoint. No test leaves a thread alive.
"""

import json
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import hub.cache.portfolio_classifier as classifier_mod
import hub.cache.workspace_store as store_mod
from hub.cache.workspace_store import WorkspaceStore
from hub.watchers.workspace_attributor import WorkspaceAttributor
from tests.test_workspace_store import _make_github_db, _mkrepo


# --- fixtures / helpers ------------------------------------------------------

def _make_events_db(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
        event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
        tokens_json TEXT, tool_name TEXT, file_path TEXT, model TEXT, cwd TEXT,
        fingerprint TEXT, created_at REAL NOT NULL)""")
    c.execute("""CREATE TABLE sessions (
        id TEXT, provider TEXT, cwd TEXT, last_event_at TEXT, ended_at TEXT)""")
    c.commit()
    c.close()
    return path


def _add_event(db: Path, session: str, file_path: str, cwd: str | None = None,
               provider: str = "claude", timestamp: str = "2026-01-01T00:00:00Z",
               created_at: float | None = None) -> int:
    c = sqlite3.connect(db)
    cur = c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, tool_name, file_path, cwd, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (provider, "p", "tool_use", timestamp, "s", session,
         "Edit", file_path, cwd, time.time() if created_at is None else created_at),
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _count(store: WorkspaceStore, table: str) -> int:
    with store._lock:
        return store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def env(tmp_path):
    """events.db + a git repo + a store. Yields (store, events_db, repo)."""
    events = _make_events_db(tmp_path / "events.db")
    repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s, events, repo
    s.close()


# --- incremental cursor pass -------------------------------------------------

class TestIncrementalAttribution:
    def test_schema_cursor_table(self, env):
        s, _, _ = env
        with s._lock:
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "attribution_cursors" in tables
        assert s.get_attribution_cursor() == 0

    def test_only_ids_above_cursor(self, env):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        id2 = _add_event(events, "s1", str(repo / "b.py"))
        r1 = s.attribute_incremental(events)
        assert r1["attributed"] == 2
        assert (r1["cursor_from"], r1["cursor_to"]) == (0, id2)
        assert s.get_attribution_cursor() == id2

        id3 = _add_event(events, "s2", str(repo / "c.py"))
        r2 = s.attribute_incremental(events)
        assert r2["attributed"] == 1  # only the new row
        assert (r2["cursor_from"], r2["cursor_to"]) == (id2, id3)
        assert _count(s, "path_attributions") == 3

    def test_cursor_persists_across_instances(self, tmp_path, env):
        s, events, repo = env
        hi = _add_event(events, "s1", str(repo / "a.py"))
        s.attribute_incremental(events)
        s2 = WorkspaceStore(tmp_path / "workspace.db")
        try:
            assert s2.get_attribution_cursor() == hi
            assert s2.attribute_incremental(events)["attributed"] == 0
        finally:
            s2.close()

    def test_second_pass_without_new_events_is_zero(self, env):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        assert s.attribute_incremental(events)["attributed"] == 1
        r = s.attribute_incremental(events)
        assert r["attributed"] == 0
        assert r["reset"] is False

    def test_non_absolute_rows_skipped_in_range(self, env):
        s, events, repo = env
        _add_event(events, "s1", "ls -la")  # Bash command string, not a path
        _add_event(events, "s1", str(repo / "a.py"))
        r = s.attribute_incremental(events)
        assert r["attributed"] == 1
        assert r["skipped_non_absolute"] == 1
        _add_event(events, "s1", str(repo / "b.py"))
        # Range-bounded: the old skipped row is not re-counted.
        assert s.attribute_incremental(events)["skipped_non_absolute"] == 0

    def test_row_inserted_mid_pass_is_not_lost(self, env, monkeypatch):
        """hi is read FIRST: a row committed during the pass (id > hi) is left
        for the next cycle instead of being skipped by the cursor."""
        s, events, repo = env
        hi = _add_event(events, "s1", str(repo / "a.py"))
        real = store_mod.resolve_dir
        late: list[int] = []

        def resolve_and_race(path):
            if not late:  # after hi + the range SELECT, before the commit
                late.append(_add_event(events, "s-late", str(repo / "late.py")))
            return real(path)

        monkeypatch.setattr(store_mod, "resolve_dir", resolve_and_race)
        r = s.attribute_incremental(events)
        assert r["attributed"] == 1
        assert s.get_attribution_cursor() == hi  # NOT past the late row
        monkeypatch.setattr(store_mod, "resolve_dir", real)

        r2 = s.attribute_incremental(events)
        assert r2["attributed"] == 1
        assert s.get_attribution_cursor() == late[0]
        assert s.get_session_workspaces("s-late")

    def test_cursor_above_max_id_resets(self, env):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        hi = _add_event(events, "s1", str(repo / "b.py"))
        s.set_attribution_cursor(10_000)  # events.db was replaced/reset
        r = s.attribute_incremental(events)
        assert r["reset"] is True
        assert r["cursor_from"] == 0
        assert r["attributed"] == 2
        assert s.get_attribution_cursor() == hi

    def test_failure_does_not_advance_cursor(self, env, monkeypatch):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))

        def boom(*a, **k):
            raise RuntimeError("disk said no")

        monkeypatch.setattr(s, "_record_attribution_locked", boom)
        with pytest.raises(RuntimeError):
            s.attribute_incremental(events)
        assert s.get_attribution_cursor() == 0
        # The failed chunk was rolled back — nothing half-written left open on
        # the shared connection for the next unrelated commit to persist.
        s.set_attribution_cursor(0)  # an unrelated commit on the same conn
        assert _count(s, "workspaces") == 0
        monkeypatch.undo()
        assert s.attribute_incremental(events)["attributed"] == 1

    def test_missing_events_db_is_noop(self, tmp_path, env):
        s, _, _ = env
        r = s.attribute_incremental(tmp_path / "nope.db")
        assert r["attributed"] == 0
        assert s.get_attribution_cursor() == 0

    def test_events_db_opened_read_only(self, env):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        before = events.read_bytes()
        s.attribute_incremental(events)
        assert events.read_bytes() == before


class TestFullBackfill:
    def test_backfill_ignores_cursor_and_sets_it(self, env):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        hi = _add_event(events, "s2", str(repo / "b.py"))
        s.set_attribution_cursor(hi)  # daemon already past everything
        r = s.backfill_from_events(events)
        assert r["attributed"] == 2  # full pass regardless of the cursor
        assert r["cursor"] == hi
        assert s.get_attribution_cursor() == hi
        # Pre-#39 return keys preserved.
        assert {"attributed", "workspaces", "directories",
                "skipped_non_absolute"} <= set(r)

    def test_cli_backfill_is_full_pass(self, tmp_path, env, monkeypatch, capsys):
        import hub.cache.event_store as es_mod
        from hub.cli import cmd_workspace_backfill

        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        hi = _add_event(events, "s2", str(repo / "b.py"))
        s.set_attribution_cursor(hi)
        s.close()
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", events)
        monkeypatch.setattr(WorkspaceStore, "DEFAULT_DB_PATH", tmp_path / "workspace.db")
        cmd_workspace_backfill(None)
        out = capsys.readouterr().out
        assert "Attributed 2 path-touches" in out
        assert f"cursor set to event id {hi}" in out
        s2 = WorkspaceStore(tmp_path / "workspace.db")
        try:
            assert s2.get_attribution_cursor() == hi
        finally:
            s2.close()


# --- disk I/O outside the lock ----------------------------------------------

class TestIoOutsideLock:
    def test_resolve_dir_never_called_under_lock(self, tmp_path, env, monkeypatch):
        s, events, repo = env
        plain = tmp_path / "Downloads" / "notes"
        plain.mkdir(parents=True)
        _add_event(events, "s1", str(repo / "a.py"))
        _add_event(events, "s1", str(plain / "n.md"))
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "abc", "2026-01-01T00:00:00")])

        calls = {"store": 0, "classifier": 0}

        def guard(real, who):
            def wrapped(path):
                assert not s._lock.locked(), f"resolve_dir({path}) under store lock"
                calls[who] += 1
                return real(path)
            return wrapped

        monkeypatch.setattr(store_mod, "resolve_dir", guard(store_mod.resolve_dir, "store"))
        monkeypatch.setattr(classifier_mod, "resolve_dir",
                            guard(classifier_mod.resolve_dir, "classifier"))

        s.backfill_from_events(events)
        _add_event(events, "s2", str(repo / "b.py"))
        s.attribute_incremental(events)
        s.build_rollup(gh)
        s.detect_delivery_candidates(events, gh)
        s.classify_workspaces(events)
        assert calls["store"] > 0
        assert calls["classifier"] > 0  # the non-git workspace hit classify()


# --- derived tables refresh in ONE transaction -------------------------------

def _reader_count(db: Path, table: str) -> int:
    r = sqlite3.connect(db, isolation_level=None)
    try:
        return r.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        r.close()


class TestAtomicRefresh:
    def test_classify_reader_never_sees_empty(self, tmp_path, env):
        s, events, repo = env
        other = _mkrepo(tmp_path, "S", "git@github.com:acme/S.git")
        _add_event(events, "s1", str(repo / "a.py"))
        _add_event(events, "s1", str(other / "a.py"))
        s.backfill_from_events(events)
        s.classify_workspaces(events)
        n = _count(s, "workspace_classification")
        assert n == 2

        seen: list[int] = []

        def trace(sql):
            if "workspace_classification" in sql and (
                sql.lstrip().upper().startswith(("DELETE", "INSERT"))
            ):
                seen.append(_reader_count(s.db_path, "workspace_classification"))

        s._conn.set_trace_callback(trace)
        try:
            s.classify_workspaces(events)
        finally:
            s._conn.set_trace_callback(None)
        assert seen, "trace saw no classification writes"
        assert all(c == n for c in seen)  # old table visible until the commit
        assert _count(s, "workspace_classification") == n

    def test_delivery_candidates_reader_never_sees_partial(self, tmp_path, env):
        s, events, repo = env
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "abc", "2026-01-01T00:00:00")])
        s.build_rollup(gh)
        s.detect_delivery_candidates(events, gh)
        n = _count(s, "delivery_candidates")
        assert n == 1  # quiescent git workspace closed by its commit

        seen: list[int] = []

        def trace(sql):
            if "delivery_candidates" in sql and (
                sql.lstrip().upper().startswith(("DELETE", "INSERT"))
            ):
                seen.append(_reader_count(s.db_path, "delivery_candidates"))

        s._conn.set_trace_callback(trace)
        try:
            s.detect_delivery_candidates(events, gh)
        finally:
            s._conn.set_trace_callback(None)
        assert seen
        assert all(c == n for c in seen)

    def test_classify_failure_rolls_back(self, env, monkeypatch):
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        s.backfill_from_events(events)
        s.classify_workspaces(events)
        n = _count(s, "workspace_classification")

        class Conn:
            """Proxy that fails the reinsert AFTER the DELETE ran."""
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def executemany(self, *a, **k):
                raise sqlite3.OperationalError("boom")

        real_conn = s._conn
        s._conn = Conn()
        try:
            with pytest.raises(sqlite3.OperationalError):
                s.classify_workspaces(events)
        finally:
            s._conn = real_conn
        s.set_attribution_cursor(1)  # unrelated commit must not persist the DELETE
        assert _count(s, "workspace_classification") == n


# --- WorkspaceAttributor -----------------------------------------------------

class FakeStore:
    def __init__(self):
        self.attributed = 0
        self.refreshes = 0
        self.fail_attr: Exception | None = None
        self.fail_refresh: Exception | None = None

    def attribute_incremental(self, _db):
        if self.fail_attr:
            raise self.fail_attr
        return {"attributed": self.attributed, "cursor_from": 0,
                "cursor_to": self.attributed, "reset": False}

    def build_rollup(self, _gh=None):
        if self.fail_refresh:
            raise self.fail_refresh
        self.refreshes += 1

    def detect_delivery_candidates(self, *_a, **_k):
        pass

    def classify_workspaces(self, *_a, **_k):
        pass


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestAttributorRefreshGate:
    def _make(self):
        store, clock = FakeStore(), Clock()
        a = WorkspaceAttributor(store, "/nope/events.db", clock=clock)
        return a, store, clock

    def test_first_cycle_refreshes(self):
        a, store, _ = self._make()
        assert a.run_once()["refreshed"] is True
        assert store.refreshes == 1

    def test_gate(self):
        a, store, clock = self._make()
        a.run_once()  # initial refresh
        store.attributed = 3
        assert a.run_once()["refreshed"] is True  # attributions → refresh
        store.attributed = 0
        clock.t += 59 * 60
        assert a.run_once()["refreshed"] is False  # none + < 60 min → no
        clock.t += 61
        assert a.run_once()["refreshed"] is True   # none + ≥ 60 min → yes
        assert store.refreshes == 3

    def test_failed_refresh_stays_pending(self):
        a, store, clock = self._make()
        a.run_once()
        store.attributed = 2
        store.fail_refresh = RuntimeError("x")
        r = a.run_once()
        assert r["ok"] is False and r["stage"] == "refresh"
        store.attributed = 0
        store.fail_refresh = None
        clock.t += 10  # well under 60 min, no new attributions…
        assert a.run_once()["refreshed"] is True  # …but the refresh is retried

    def test_error_recorded_type_only_and_cleared(self):
        a, store, _ = self._make()
        store.fail_attr = OSError("/Users/me/Secret-Client/.git: timed out")
        r = a.run_once()  # never raises
        assert r["ok"] is False
        assert a.last_attribution_error == "OSError"  # no path / folder name
        assert a.last_attribution_at is None
        store.fail_attr = None
        assert a.run_once()["ok"] is True
        assert a.last_attribution_error is None
        assert a.last_attribution_at is not None


class TestAttributorEndToEnd:
    def test_run_once_makes_new_project_visible(self, tmp_path, env):
        """The #39 bug: a session captured in events.db shows up in the
        portfolio with no manual backfill."""
        s, events, repo = env
        _add_event(events, "s1", str(repo / "a.py"))
        a = WorkspaceAttributor(s, events, tmp_path / "github.db")
        r = a.run_once()
        assert r["ok"] and r["attributed"] == 1 and r["refreshed"]
        keys = {w["workspace_key"] for w in s.get_portfolio()}
        assert "git_remote:github.com/acme/r" in keys
        assert _count(s, "workspace_classification") == 1


class TestAttributorLifecycle:
    def test_stop_is_fast_during_initial_delay(self):
        a = WorkspaceAttributor(FakeStore(), "/nope", initial_delay=60, interval=60)
        a.start()
        try:
            t0 = time.monotonic()
            a.stop()
            assert time.monotonic() - t0 < 2
            assert not a._thread.is_alive()
        finally:
            a.stop()

    def test_stop_is_fast_during_interval(self):
        store = FakeStore()
        a = WorkspaceAttributor(store, "/nope", initial_delay=0, interval=60)
        a.start()
        try:
            deadline = time.monotonic() + 5
            while store.refreshes == 0 and time.monotonic() < deadline:
                a._stop_event.wait(0.01)
            assert store.refreshes == 1  # first cycle ran, now waiting
            t0 = time.monotonic()
            a.stop()
            assert time.monotonic() - t0 < 2
            assert not a._thread.is_alive()
        finally:
            a.stop()

    def test_attributor_imports_no_event_store(self):
        src = (Path(__file__).parents[1] / "hub" / "watchers"
               / "workspace_attributor.py").read_text(encoding="utf-8")
        assert "EventStore" not in src.replace("``EventStore``", "")
        assert "sse_buffer" not in src


# --- dashboard wiring + meta endpoint ----------------------------------------

@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """Point every default DB at tmp and let each test choose the config."""
    import hub.cache.event_store as es_mod
    import hub.config as config_mod

    monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(WorkspaceStore, "DEFAULT_DB_PATH", tmp_path / "workspace.db")
    holder: dict = {}
    monkeypatch.setattr(config_mod, "load_config", lambda: holder["cfg"])

    def make(**cfg):
        from hub.config import HubConfig
        from hub.dashboard.server import DashboardServer
        holder["cfg"] = HubConfig(**cfg)
        return DashboardServer(host="127.0.0.1", port=0, providers=["claude"])

    return make


class TestServerWiring:
    def test_auto_attribution_off_does_not_create(self, server_env):
        srv = server_env(auto_attribution=False)
        assert srv.workspace_attributor is None
        assert srv.workspace_store is None

    def test_attributor_on_without_folder_monitoring(self, server_env):
        srv = server_env(auto_attribution=True, filesystem_monitoring=False)
        assert srv.workspace_attributor is not None
        assert srv.workspace_watcher is None
        assert srv.workspace_attributor._store is srv.workspace_store
        assert Path(srv.workspace_attributor._events_db_path) == srv.event_store.db_path

    def test_shared_single_store(self, tmp_path, server_env):
        from hub.config import WorkspaceRoot
        srv = server_env(
            auto_attribution=True, filesystem_monitoring=True,
            workspace_roots=[WorkspaceRoot(path=str(tmp_path), max_depth=1)],
        )
        assert srv.workspace_watcher is not None
        assert srv.workspace_attributor is not None
        assert srv.workspace_watcher._store is srv.workspace_store
        assert srv.workspace_attributor._store is srv.workspace_store

    def test_shutdown_stops_workspace_threads_before_closing_stores(
        self, tmp_path, server_env
    ):
        """#44: the watcher was never stopped on shutdown — an in-flight scan
        could be cut mid-write. Both workspace threads stop before any close."""
        from unittest import mock
        from hub.config import WorkspaceRoot
        srv = server_env(
            auto_attribution=True, filesystem_monitoring=True,
            workspace_roots=[WorkspaceRoot(path=str(tmp_path), max_depth=1)],
        )
        calls = mock.Mock()
        srv.workspace_watcher = calls.workspace_watcher
        srv.workspace_attributor = calls.workspace_attributor
        srv.git_store = calls.git_store
        srv.event_store = calls.event_store
        srv.watchers = []

        srv._shutdown(calls.httpd)

        names = [c[0] for c in calls.mock_calls]
        assert names == [
            "workspace_watcher.stop",
            "workspace_attributor.stop",
            "git_store.close",
            "event_store.close",
            "httpd.shutdown",
        ]

    def test_shutdown_without_workspace_threads(self, server_env):
        from unittest import mock
        srv = server_env(auto_attribution=False)
        assert srv.workspace_watcher is None and srv.workspace_attributor is None
        srv.event_store = mock.Mock()
        httpd = mock.Mock()
        srv._shutdown(httpd)
        srv.event_store.close.assert_called_once()
        httpd.shutdown.assert_called_once()


def _serve(srv):
    import http.server
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


class TestMonitoringMeta:
    def test_new_fields_additive(self, server_env):
        srv = server_env(auto_attribution=True, filesystem_monitoring=False)
        srv.workspace_attributor.last_attribution_at = "2026-09-23T10:00:00+00:00"
        httpd, port = _serve(srv)
        try:
            data = _get(port, "/api/workspace/monitoring")
        finally:
            httpd.shutdown()
            httpd.server_close()
        # Pre-#39 fields unchanged.
        assert data["filesystem_monitoring"] is False
        assert data["roots_count"] == 0
        assert data["active"] is False
        # Additive #39 fields.
        assert data["auto_attribution"] is True
        assert data["attribution_active"] is True
        assert data["last_attribution_at"] == "2026-09-23T10:00:00+00:00"
        assert data["last_attribution_error"] is None

    def test_fields_when_disabled(self, server_env):
        srv = server_env(auto_attribution=False)
        httpd, port = _serve(srv)
        try:
            data = _get(port, "/api/workspace/monitoring")
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert data["auto_attribution"] is False
        assert data["attribution_active"] is False
        assert data["last_attribution_at"] is None
        assert data["last_attribution_error"] is None
