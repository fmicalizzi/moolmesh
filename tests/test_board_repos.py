"""Board repos — issues-only backlogs (#93). Synthetic fixtures, no network."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from hub.cache.git_store import GitStore
from hub.config import (
    BOARD_PATH_PREFIX, HubConfig as Config, RepoConfig, board_repo_config, load_config,
    normalize_repo_kind, parse_board_slug, save_config,
)


def _code_repo(path="/Users/u/work/widget-services") -> RepoConfig:
    return RepoConfig(path=path, remote_url="github.com/acme/widget-services",
                      owner="acme", repo="widget-services",
                      added_at="2026-10-01T00:00:00+00:00")


def _issue(number, state="open", labels=(), assignees=(), created="2026-10-01T12:00:00",
           closed=None, is_pr=0):
    return {
        "number": number, "title": f"Item {number}", "state": state,
        "author": "ownerhandle", "assignees": json.dumps(list(assignees)),
        "labels": json.dumps(list(labels)), "created_at": created,
        "updated_at": closed or created, "closed_at": closed,
        "is_pull_request": is_pr,
    }


# --- config -----------------------------------------------------------------

class TestConfig:
    def test_default_kind_is_code(self):
        assert _code_repo().kind == "code"
        assert normalize_repo_kind(None) == "code"
        assert normalize_repo_kind("BOARD") == "board"
        assert normalize_repo_kind("weird") == "code"

    def test_parse_board_slug(self):
        assert parse_board_slug("acme/backlog") == ("acme", "backlog")
        assert parse_board_slug("github.com/acme/backlog") == ("acme", "backlog")
        assert parse_board_slug("https://github.com/acme/backlog.git") == ("acme", "backlog")
        assert parse_board_slug("board:github.com/acme/backlog") == ("acme", "backlog")
        assert parse_board_slug("acme") is None
        assert parse_board_slug("a/b/c") is None

    def test_board_repo_config(self):
        rc = board_repo_config("acme/backlog")
        assert rc.kind == "board"
        assert rc.path == f"{BOARD_PATH_PREFIX}github.com/acme/backlog"
        assert rc.remote_url == "github.com/acme/backlog"
        assert rc.github_enabled is True
        with pytest.raises(ValueError):
            board_repo_config("not-a-slug")

    def test_kind_round_trips_and_code_is_not_written(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg = Config()
        cfg.repos = [_code_repo(), board_repo_config("acme/backlog")]
        with patch("hub.config.CONFIG_PATH", cfg_path), \
             patch("hub.config.CONFIG_DIR", tmp_path):
            save_config(cfg)
            text = cfg_path.read_text()
            assert text.count('kind = "board"') == 1
            assert 'kind = "code"' not in text  # default stays implicit
            loaded = load_config()
        assert [r.kind for r in loaded.repos] == ["code", "board"]

    def test_remove_board_repo_by_key(self, tmp_path):
        from hub.config import remove_repo
        cfg_path = tmp_path / "config.toml"
        cfg = Config()
        cfg.repos = [board_repo_config("acme/backlog")]
        with patch("hub.config.CONFIG_PATH", cfg_path), \
             patch("hub.config.CONFIG_DIR", tmp_path):
            save_config(cfg)
            assert remove_repo("board:github.com/acme/backlog") is True
            assert load_config().repos == []


# --- github.db migration 7 ----------------------------------------------------

class TestMigration:
    def test_fresh_db_has_kind(self, tmp_path):
        store = GitStore(tmp_path / "github.db")
        try:
            rid = store.register_repo(board_repo_config("acme/backlog"))
            store.register_repo(_code_repo())
            kinds = {r["repo_name"]: r["kind"] for r in store.list_repos()}
            assert kinds == {"backlog": "board", "widget-services": "code"}
            assert rid
        finally:
            store.close()

    def test_old_db_gets_column_and_keeps_rows(self, tmp_path):
        # A pre-#93 github.db: repos without ``kind``, migrations 1-6 applied.
        db = tmp_path / "github.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE, remote_url TEXT NOT NULL,
                owner TEXT NOT NULL, repo_name TEXT NOT NULL,
                added_at TEXT NOT NULL, last_fetch_at TEXT,
                history_complete INTEGER NOT NULL DEFAULT 0,
                last_github_sync_at TEXT);
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at TEXT NOT NULL);
            INSERT INTO repos (path, remote_url, owner, repo_name, added_at,
                               history_complete)
            VALUES ('/Users/u/work/widget-services',
                    'github.com/acme/widget-services', 'acme',
                    'widget-services', '2026-10-01', 1);
        """)
        for v in range(1, 7):
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                         (v, f"m{v}", "2026-10-01"))
        conn.commit()
        conn.close()

        store = GitStore(db)
        try:
            repos = store.list_repos()
            assert len(repos) == 1
            assert repos[0]["kind"] == "code"
            assert repos[0]["history_complete"] is True  # untouched
            versions = {r[0] for r in store._get_conn().execute(
                "SELECT version FROM schema_migrations")}
            assert 7 in versions
        finally:
            store.close()
        # Idempotent: a third open applies nothing new.
        GitStore(db).close()

    def test_sync_repo_kinds_mirrors_config(self, tmp_path):
        store = GitStore(tmp_path / "github.db")
        try:
            rc = _code_repo()
            store.register_repo(rc)
            rc.kind = "board"  # owner edits config.toml by hand
            assert store.sync_repo_kinds([rc]) == 1
            assert store.sync_repo_kinds([rc]) == 0  # nothing left to change
            assert store.list_repos()[0]["kind"] == "board"
        finally:
            store.close()


# --- harvesters ---------------------------------------------------------------

class TestHarvesters:
    def test_git_harvester_skips_board(self, tmp_path):
        from hub.harvesters.git_harvester import GitHarvester
        store = GitStore(tmp_path / "github.db")
        try:
            store.register_repo(board_repo_config("acme/backlog"))
            store.register_repo(_code_repo())
            h = GitHarvester(store)
            h._running = True
            seen = []

            def fake_fetch(repo):
                seen.append(repo["repo_name"])
                h._running = False  # one cycle only

            with patch.object(h, "_fetch_and_ingest", side_effect=fake_fetch), \
                 patch.object(h._stop_event, "wait"):
                h._harvest_loop()
            assert seen == ["widget-services"]
        finally:
            store.close()

    def test_github_harvester_still_polls_board(self, tmp_path):
        from hub.harvesters.github_harvester import GitHubHarvester
        store = GitStore(tmp_path / "github.db")
        try:
            board = board_repo_config("acme/backlog")
            store.register_repo(board)
            cfg = Config()
            cfg.repos = [board]
            h = GitHubHarvester(store, github_client=None)
            h._running = True
            calls = []
            with patch.object(h, "_get_cached_config", return_value=cfg):
                def poll(repo_id, owner, repo):
                    calls.append((owner, repo))
                h._poll_all_repos(poll)
            assert calls == [("acme", "backlog")]
        finally:
            store.close()


# --- portfolio annotation -------------------------------------------------------

class TestPortfolioAnnotation:
    def test_board_flag(self, tmp_path):
        from hub.mcp_server import (
            _annotate_github_measured_projects, _board_remote_keys,
            _registered_remote_keys,
        )
        db = tmp_path / "github.db"
        store = GitStore(db)
        store.register_repo(board_repo_config("acme/backlog"))
        store.register_repo(_code_repo())
        store.close()
        projects = [
            {"project_key": "git_remote:github.com/acme/backlog"},
            {"project_key": "git_remote:github.com/acme/widget-services"},
            {"project_key": "dir:/Users/u/work/_acme"},
        ]
        _annotate_github_measured_projects(
            projects, _registered_remote_keys(str(db)), _board_remote_keys(str(db)))
        assert [p["board"] for p in projects] == [True, False, False]
        assert [p["registered"] for p in projects] == [True, True, False]

    def test_board_keys_tolerate_unmigrated_db(self, tmp_path):
        from hub.mcp_server import _board_remote_keys
        db = tmp_path / "github.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE repos (owner TEXT, repo_name TEXT)")
        conn.commit()
        conn.close()
        assert _board_remote_keys(str(db)) == set()
        assert _board_remote_keys(str(tmp_path / "missing.db")) == set()


# --- get_board_issues -------------------------------------------------------------

@pytest.fixture
def board_db(tmp_path):
    db = tmp_path / "github.db"
    store = GitStore(db)
    rid = store.register_repo(board_repo_config("acme/backlog"))
    store.upsert_issues(rid, [
        _issue(1, labels=["bug"], assignees=["ownerhandle"], created="2026-10-01T12:00:00"),
        _issue(2, labels=["bug", "ux"], created="2026-10-30T12:00:00"),
        _issue(3, labels=["ux"], created="2026-11-03T12:00:00"),
        _issue(4, state="closed", labels=["bug"], created="2026-09-01T12:00:00",
               closed="2026-11-02T12:00:00"),
        _issue(5, created="2026-10-31T12:00:00"),
        _issue(6, labels=["bug"], created="2026-11-01T12:00:00", is_pr=1),  # PR: ignored
    ])
    store.close()
    return str(db)


class TestBoardIssues:
    TODAY = "2026-11-04"

    def _get(self, db, **kw):
        from hub.mcp_server import _get_board_issues
        kw.setdefault("today", self.TODAY)
        return _get_board_issues(db, "Acme/Backlog", **kw)

    def test_summary_and_default_open(self, board_db):
        res = self._get(board_db, days=7)
        assert res["repo"] == "acme/backlog" and res["kind"] == "board"
        assert res["window_start"] == "2026-10-29" and res["window_end"] == self.TODAY
        assert [i["number"] for i in res["issues"]] == [3, 5, 2, 1]
        assert res["open_total"] == 4
        assert res["opened_window"] == 3   # #2, #3, #5 (PR #6 excluded)
        assert res["closed_window"] == 1   # #4
        assert res["open_by_label"] == {"bug": 2, "ux": 2, "(sin label)": 1}

    def test_filters(self, board_db):
        assert [i["number"] for i in self._get(board_db, label="BUG")["issues"]] == [2, 1]
        assert [i["number"] for i in self._get(board_db, assignee="OwnerHandle")["issues"]] == [1]
        old = self._get(board_db, min_age_days=30)["issues"]
        assert [i["number"] for i in old] == [1] and old[0]["age_days"] == 34
        assert [i["number"] for i in self._get(board_db, max_age_days=3)["issues"]] == [3]
        assert [i["number"] for i in self._get(board_db, state="closed")["issues"]] == [4]
        assert len(self._get(board_db, state="all")["issues"]) == 5
        assert len(self._get(board_db, state="all", limit=2)["issues"]) == 2

    def test_errors(self, board_db, tmp_path):
        from hub.mcp_server import _get_board_issues
        assert "error" in self._get(board_db, state="merged")
        assert "error" in _get_board_issues(board_db, "acme", today=self.TODAY)
        assert "error" in _get_board_issues(board_db, "acme/other", today=self.TODAY)
        assert "error" in _get_board_issues(str(tmp_path / "none.db"), "acme/backlog")
