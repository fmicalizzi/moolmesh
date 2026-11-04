"""Stale-data flag measured from the data (#76)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from hub import config as config_mod
from hub import freshness, mcp_server
from hub.cache.git_store import GitStore
from hub.config import RepoConfig

NOW = 1_800_000_000.0  # fixed clock
MIN = 60


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@pytest.fixture
def dbs(tmp_path):
    """github.db with two repos + a minimal events.db; returns a setter."""
    github_db = tmp_path / "github.db"
    store = GitStore(github_db)
    ids = []
    for name in ("widget-services", "acme-api"):
        ids.append(store.register_repo(RepoConfig(
            path=f"/home/u/work/{name}", remote_url=f"git@github.com:acme/{name}.git",
            owner="acme", repo=name, added_at=_iso(NOW - 86400))))
    store.close()

    events_db = tmp_path / "events.db"
    conn = sqlite3.connect(events_db)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at REAL NOT NULL)")
    conn.commit()
    conn.close()

    class Dbs:
        events = events_db
        github = github_db

        @staticmethod
        def sync(repo_index: int, git_age: float | None, gh_age: float | None = None):
            c = sqlite3.connect(github_db)
            c.execute("UPDATE repos SET last_fetch_at = ?, last_github_sync_at = ? WHERE id = ?",
                      (None if git_age is None else _iso(NOW - git_age),
                       None if gh_age is None else _iso(NOW - gh_age), ids[repo_index]))
            c.commit()
            c.close()

        @staticmethod
        def ingest(age: float):
            c = sqlite3.connect(events_db)
            c.execute("INSERT INTO events (created_at) VALUES (?)", (NOW - age,))
            c.commit()
            c.close()

    return Dbs


def _fresh(dbs, running=True, threshold=60):
    return freshness.compute_freshness(dbs.events, dbs.github, running=running,
                                       threshold_minutes=threshold, now=NOW)


class TestComputeFreshness:
    def test_everything_current_is_not_stale(self, dbs):
        dbs.sync(0, 5 * MIN, 1 * MIN)
        dbs.sync(1, 10 * MIN, 2 * MIN)
        dbs.ingest(30)
        f = _fresh(dbs)
        assert f["stale"] is False and f["stale_reasons"] == []
        assert f["repos_stale"] == 0
        assert f["data_age_seconds"] == 30
        assert f["last_git_sync_at"] == freshness._iso(NOW - 5 * MIN)
        assert f["last_github_sync_at"] == freshness._iso(NOW - 1 * MIN)
        assert set(f) >= {"daemon_running", "last_git_sync_at", "last_github_sync_at",
                          "repos_stale", "last_ingest_at", "data_age_seconds", "stale"}

    def test_daemon_down_is_stale(self, dbs):
        dbs.sync(0, 1 * MIN, 1 * MIN)
        dbs.sync(1, 1 * MIN, 1 * MIN)
        f = _fresh(dbs, running=False)
        assert f["stale"] is True and "daemon_not_running" in f["stale_reasons"]

    def test_old_git_sync_is_stale(self, dbs):
        dbs.sync(0, 3 * 3600, 1 * MIN)
        dbs.sync(1, 2 * 3600, 1 * MIN)
        f = _fresh(dbs)
        assert f["stale"] is True and f["stale_reasons"] == ["git_sync_old"]

    def test_old_github_api_sync_is_stale(self, dbs):
        dbs.sync(0, 1 * MIN, 8 * 86400)   # the 2026-10-27 shape: API frozen for days
        dbs.sync(1, 1 * MIN, 8 * 86400)
        f = _fresh(dbs)
        assert f["stale"] is True and f["stale_reasons"] == ["github_sync_old"]
        assert f["data_age_seconds"] == 60

    def test_repos_stale_counts_the_one_behind(self, dbs):
        dbs.sync(0, 1 * MIN, 1 * MIN)
        dbs.sync(1, 5 * 3600, 1 * MIN)
        f = _fresh(dbs)
        assert f["repos_stale"] == 1
        assert f["stale"] is False   # the max is current; the count exposes the laggard

    def test_old_ingest_alone_is_not_stale_but_reported(self, dbs):
        dbs.sync(0, 1 * MIN)
        dbs.sync(1, 1 * MIN)
        dbs.ingest(3 * 86400)        # a weekend without agents
        f = _fresh(dbs)
        assert f["stale"] is False
        assert f["last_ingest_at"] == freshness._iso(NOW - 3 * 86400)

    def test_ingest_age_is_the_data_age_when_nothing_else_exists(self, tmp_path, dbs):
        dbs.ingest(3 * 86400)
        f = freshness.compute_freshness(dbs.events, tmp_path / "missing.db", running=True,
                                        threshold_minutes=60, now=NOW)
        assert f["stale"] is False and f["repos_stale"] == 0
        assert f["data_age_seconds"] == 3 * 86400

    def test_never_api_synced_is_unknown_not_stale(self, dbs):
        dbs.sync(0, 1 * MIN)          # no token: last_github_sync_at stays NULL
        dbs.sync(1, 1 * MIN)
        f = _fresh(dbs)
        assert f["last_github_sync_at"] is None and f["stale"] is False

    def test_threshold_is_applied(self, dbs):
        dbs.sync(0, 20 * MIN)
        dbs.sync(1, 20 * MIN)
        assert _fresh(dbs, threshold=60)["stale"] is False
        assert _fresh(dbs, threshold=10)["stale"] is True

    def test_missing_databases_never_raise(self, tmp_path):
        f = freshness.compute_freshness(tmp_path / "a.db", tmp_path / "b.db",
                                        running=True, threshold_minutes=60, now=NOW)
        assert f["stale"] is False and f["data_age_seconds"] is None

    def test_daemon_running_reads_the_pidfile_without_removing_it(self, tmp_path, monkeypatch):
        from hub import daemon
        pid_file = tmp_path / "moolmesh.pid"
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        assert freshness.daemon_running() is False
        pid_file.write_text("4242")
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
        assert freshness.daemon_running() is False
        assert pid_file.exists()
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: pid == 4242)
        assert freshness.daemon_running() is True


class TestThresholdConfig:
    def _write(self, tmp_path, monkeypatch, text):
        path = tmp_path / "config.toml"
        path.write_text(text)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        return path

    def test_default_is_60(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, "")
        assert config_mod.load_config().stale_after_minutes == 60

    def test_parsed_from_daemon_section(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, "[daemon]\nstale_after_minutes = 15\n")
        assert config_mod.load_config().stale_after_minutes == 15
        assert freshness.stale_after_minutes() == 15

    @pytest.mark.parametrize("value", ["0", "-5", '"soon"', "true"])
    def test_invalid_values_fall_back(self, tmp_path, monkeypatch, value):
        self._write(tmp_path, monkeypatch, f"[daemon]\nstale_after_minutes = {value}\n")
        assert config_mod.load_config().stale_after_minutes == 60

    def test_survives_save_config(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, "[daemon]\nstale_after_minutes = 15\n")
        config_mod.save_config(config_mod.load_config())
        assert config_mod.load_config().stale_after_minutes == 15

    def test_default_is_not_written(self, tmp_path, monkeypatch):
        path = self._write(tmp_path, monkeypatch, "")
        config_mod.save_config(config_mod.load_config())
        assert "[daemon]" not in path.read_text()

    def test_compute_uses_the_config_threshold(self, tmp_path, monkeypatch, dbs):
        self._write(tmp_path, monkeypatch, "[daemon]\nstale_after_minutes = 10\n")
        dbs.sync(0, 20 * MIN)
        dbs.sync(1, 20 * MIN)
        f = freshness.compute_freshness(dbs.events, dbs.github, running=True, now=NOW)
        assert f["stale_after_minutes"] == 10 and f["stale"] is True


# ── MCP surface ─────────────────────────────────────────────────────

@pytest.fixture
def mcp(dbs, monkeypatch):
    monkeypatch.setattr(mcp_server, "EVENTS_DB", str(dbs.events))
    monkeypatch.setattr(mcp_server, "GITHUB_DB", str(dbs.github))
    monkeypatch.setattr(mcp_server, "_freshness_cache", None)
    monkeypatch.setattr(freshness, "daemon_running", lambda: False)
    return mcp_server


class TestMcp:
    def test_server_info_always_has_freshness(self, mcp):
        info = mcp._get_server_info()
        assert info["freshness"]["daemon_running"] is False
        assert info["freshness"]["stale"] is True

    def test_dict_tool_gets_freshness_and_top_level_stale(self, mcp):
        tool = mcp._freshness_aware(lambda: {"projects": []})
        result = tool()
        assert result["projects"] == []
        assert result["stale"] is True
        assert result["freshness"]["stale_reasons"][0] == "daemon_not_running"

    def test_fresh_data_has_no_top_level_stale(self, mcp, monkeypatch, dbs):
        monkeypatch.setattr(freshness, "daemon_running", lambda: True)
        now = time.time()
        c = sqlite3.connect(dbs.github)
        c.execute("UPDATE repos SET last_fetch_at = ?", (_iso(now),))
        c.commit()
        c.close()
        result = mcp._freshness_aware(lambda: {"projects": []})()
        assert "stale" not in result and result["freshness"]["stale"] is False

    def test_list_results_keep_their_type(self, mcp):
        rows = [{"a": 1}]
        assert mcp._freshness_aware(lambda: rows)() is rows

    def test_portfolio_dict_tools_are_wrapped_in_source(self):
        """The MCP SDK may be absent here: check the decorators in the source."""
        import inspect
        src = inspect.getsource(mcp_server)
        for name in ("get_portfolio_grouped", "get_portfolio_production"):
            assert f"@_freshness_aware\n    def {name}(" in src
        for name in ("get_portfolio", "get_workspace_activity", "get_sessions"):
            assert f"@_freshness_aware\n    def {name}(" not in src

    def test_instructions_mention_stale_data(self):
        assert "`stale: true`" in mcp_server._SERVER_INSTRUCTIONS

    def test_cached_for_a_few_seconds(self, mcp, monkeypatch):
        calls = []
        real = freshness.compute_freshness
        monkeypatch.setattr(freshness, "compute_freshness",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        mcp._freshness()
        mcp._freshness()
        assert len(calls) == 1

    def test_a_failure_never_breaks_the_tool(self, mcp, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(freshness, "compute_freshness", boom)
        result = mcp._freshness_aware(lambda: {"ok": 1})()
        assert result["ok"] == 1 and result["freshness"]["stale"] is None


# ── Where the sync time is recorded ─────────────────────────────────

class TestSyncRecord:
    def _harvester(self, status):
        from hub.harvesters.github_harvester import GitHubHarvester
        store = MagicMock(spec=GitStore)
        client = MagicMock()
        client.list_issues.return_value = (status, [] if status == 200 else None, None)
        return GitHubHarvester(store, client), store

    @pytest.mark.parametrize("status", [200, 304])
    def test_successful_poll_records_the_sync(self, status):
        h, store = self._harvester(status)
        h._poll_issues_prs(1, "acme", "widget-services")
        store.mark_github_sync.assert_called_once_with(1)

    @pytest.mark.parametrize("status", [0, 401, 403, 404, 500])
    def test_failed_poll_records_nothing(self, status):
        h, store = self._harvester(status)
        h._poll_issues_prs(1, "acme", "widget-services")
        store.mark_github_sync.assert_not_called()

    def test_record_is_throttled(self):
        h, store = self._harvester(304)
        h._poll_issues_prs(1, "acme", "widget-services")
        h._poll_issues_prs(1, "acme", "widget-services")
        assert store.mark_github_sync.call_count == 1

    def test_mark_github_sync_writes_the_column(self, dbs):
        store = GitStore(dbs.github)
        repo_id = store.list_repos()[0]["id"]
        store.mark_github_sync(repo_id)
        store.close()
        c = sqlite3.connect(dbs.github)
        value = c.execute("SELECT last_github_sync_at FROM repos WHERE id = ?",
                          (repo_id,)).fetchone()[0]
        c.close()
        assert value and datetime.fromisoformat(value).tzinfo is not None

    def test_migration_5_adds_the_column_to_an_old_db(self, tmp_path):
        db = tmp_path / "github.db"
        c = sqlite3.connect(db)
        c.execute("""CREATE TABLE repos (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     path TEXT NOT NULL UNIQUE, remote_url TEXT NOT NULL,
                     owner TEXT NOT NULL, repo_name TEXT NOT NULL,
                     added_at TEXT NOT NULL, last_fetch_at TEXT)""")
        c.execute("INSERT INTO repos (path, remote_url, owner, repo_name, added_at) "
                  "VALUES ('/home/u/work/proj', 'x', 'acme', 'proj', '2026-01-01')")
        c.commit()
        c.close()
        GitStore(db).close()
        c = sqlite3.connect(db)
        cols = {r[1] for r in c.execute("PRAGMA table_info(repos)")}
        rows = c.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        c.close()
        assert "last_github_sync_at" in cols and rows == 1


# ── Dashboard ───────────────────────────────────────────────────────

class TestDashboard:
    @pytest.fixture
    def server(self, dbs):
        import http.server
        from hub.cache.event_store import EventStore
        from hub.dashboard.server import DashboardServer
        srv = DashboardServer(host="127.0.0.1", port=0)
        srv.event_store = EventStore(dbs.events.parent / "dash-events.db")
        srv.git_store = GitStore(dbs.github)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield srv, httpd.server_address[1]
        httpd.shutdown()

    def _get(self, port, path) -> bytes:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.read()

    def test_freshness_endpoint(self, server, dbs):
        _srv, port = server
        f = json.loads(self._get(port, "/api/freshness"))
        assert f["daemon_running"] is True
        assert f["stale"] is True and "git_sync_old" in f["stale_reasons"]

    def test_dead_watcher_is_stale(self, server, monkeypatch):
        srv, _port = server
        monkeypatch.setattr(srv, "watchers_health", lambda: ({}, True))
        f = srv.freshness()
        assert f["stale"] is True and "watcher_degraded" in f["stale_reasons"]

    @pytest.mark.parametrize("page", ["/", "/analytics", "/projects", "/timeline", "/portfolio"])
    def test_every_page_carries_the_banner(self, server, page):
        _srv, port = server
        html = self._get(port, page)
        assert b'id="mm-freshness-banner"' in html
        assert b"/api/freshness" in html
