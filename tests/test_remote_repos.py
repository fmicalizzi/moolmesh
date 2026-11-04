"""Remote-only repos (#94): code repos followed through the GitHub API alone,
identity by the numeric GitHub id, ``repo add --owner``. Synthetic fixtures
(Acme), no network."""
import argparse
import sqlite3
from datetime import timezone
from unittest.mock import patch

import pytest

from hub.cache.git_store import GitStore
from hub.config import (
    HubConfig as Config, REMOTE_PATH_PREFIX, RepoConfig, board_repo_config,
    find_repo_by_slug, load_config, normalize_repo_kind, remote_repo_config,
    repo_has_git, save_config,
)


def _code_repo(path="/Users/u/work/widget-services") -> RepoConfig:
    return RepoConfig(path=path, remote_url="github.com/acme/widget-services",
                      owner="acme", repo="widget-services",
                      added_at="2026-10-01T00:00:00+00:00")


def _issue(number, state="open", created="2026-10-01T12:00:00", closed=None,
           is_pr=0, merged=None):
    d = {
        "number": number, "title": f"Item {number}", "state": state,
        "author": "ownerhandle", "assignees": "[]", "labels": "[]",
        "created_at": created, "updated_at": closed or merged or created,
        "closed_at": closed, "is_pull_request": is_pr,
    }
    if is_pr:
        d.update(pr_merged_at=merged, pr_state="merged" if merged else "review",
                 pr_base_branch="", pr_head_branch="", pr_review_decision="")
    return d


@pytest.fixture
def cfg_home(tmp_path):
    """config.toml under tmp_path (never the real ~/.moolmesh)."""
    cfg_path = tmp_path / "config.toml"
    with patch("hub.config.CONFIG_PATH", cfg_path), \
         patch("hub.config.CONFIG_DIR", tmp_path):
        yield tmp_path


def _save_repos(*repos):
    cfg = Config()
    cfg.repos = list(repos)
    save_config(cfg)


class FakeClient:
    """GitHubClient stand-in: ``repos`` maps "owner/name" (any case) → the
    canonical API object ({"id", "full_name"}), as GitHub's redirect would."""

    def __init__(self, repos=None, status=200):
        self.repos = {k.lower(): v for k, v in (repos or {}).items()}
        self.status = status
        self.calls = []

    def get_repo(self, owner, repo, etag=None):
        self.calls.append((owner, repo, etag))
        if self.status != 200:
            return self.status, None, None
        data = self.repos.get(f"{owner}/{repo}".lower())
        if data is None:
            return 404, None, None
        return 200, dict(data), '"etag-1"'


# --- config -----------------------------------------------------------------

class TestConfig:
    def test_remote_kind(self):
        rc = remote_repo_config("acme/widgets")
        assert rc.kind == "remote" == normalize_repo_kind("Remote")
        assert rc.path == f"{REMOTE_PATH_PREFIX}github.com/acme/widgets"
        assert rc.remote_url == "github.com/acme/widgets"
        assert not repo_has_git(rc)
        assert not repo_has_git({"kind": "board"})
        assert repo_has_git(_code_repo()) and repo_has_git({"kind": "code"})
        with pytest.raises(ValueError):
            remote_repo_config("acme")

    def test_round_trip_and_lookup(self, cfg_home):
        _save_repos(_code_repo(), remote_repo_config("acme/widgets"),
                    board_repo_config("acme/backlog"))
        text = (cfg_home / "config.toml").read_text()
        assert text.count('kind = "remote"') == 1
        repos = load_config().repos
        assert [r.kind for r in repos] == ["code", "remote", "board"]
        assert repos[1].path == "remote:github.com/acme/widgets"  # not resolved
        assert find_repo_by_slug(repos, "ACME/Widgets") is repos[1]
        assert find_repo_by_slug(repos, "acme/backlog") is repos[2]
        # A code repo is addressed by its path, not by owner/repo.
        assert find_repo_by_slug(repos, "acme/widget-services") is None


# --- github.db migration 8 ----------------------------------------------------

class TestMigration:
    def test_fresh_db_has_github_id(self, tmp_path):
        store = GitStore(tmp_path / "github.db")
        try:
            rid = store.register_repo(remote_repo_config("acme/widgets"))
            assert store.get_github_id(rid) is None
            store.set_github_identity(rid, 4242, "acme", "widgets")
            assert store.get_github_id(rid) == 4242
            assert store.get_repo_by_github_id(4242)["id"] == rid
            assert store.list_repos()[0]["github_id"] == 4242
            idx = {r[1] for r in store._get_conn().execute(
                "PRAGMA index_list(repos)")}
            assert "idx_repos_github_id" in idx
        finally:
            store.close()

    def test_old_db_gets_column_and_keeps_rows(self, tmp_path):
        db = tmp_path / "github.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE, remote_url TEXT NOT NULL,
                owner TEXT NOT NULL, repo_name TEXT NOT NULL,
                added_at TEXT NOT NULL, last_fetch_at TEXT,
                history_complete INTEGER NOT NULL DEFAULT 0,
                last_github_sync_at TEXT,
                kind TEXT NOT NULL DEFAULT 'code');
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at TEXT NOT NULL);
            INSERT INTO repos (path, remote_url, owner, repo_name, added_at,
                               history_complete, kind)
            VALUES ('board:github.com/acme/backlog', 'github.com/acme/backlog',
                    'acme', 'backlog', '2026-10-01', 1, 'board');
        """)
        for v in range(1, 8):
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                         (v, f"m{v}", "2026-10-01"))
        conn.commit()
        conn.close()
        store = GitStore(db)
        try:
            repos = store.list_repos()
            assert len(repos) == 1
            assert repos[0]["kind"] == "board"
            assert repos[0]["history_complete"] is True
            assert repos[0]["github_id"] is None
            versions = {r[0] for r in store._get_conn().execute(
                "SELECT version FROM schema_migrations")}
            assert 8 in versions
        finally:
            store.close()
        GitStore(db).close()  # idempotent reopen


# --- harvesters ---------------------------------------------------------------

class TestGitHarvester:
    def test_skips_remote(self, tmp_path):
        from hub.harvesters.git_harvester import GitHarvester
        store = GitStore(tmp_path / "github.db")
        try:
            store.register_repo(remote_repo_config("acme/widgets"))
            store.register_repo(_code_repo())
            h = GitHarvester(store)
            h._running = True
            seen = []

            def fake_fetch(repo):
                seen.append(repo["repo_name"])
                h._running = False

            with patch.object(h, "_fetch_and_ingest", side_effect=fake_fetch), \
                 patch.object(h._stop_event, "wait"):
                h._harvest_loop()
            assert seen == ["widget-services"]
        finally:
            store.close()


class TestIdentity:
    """A transfer / rename keeps the series: same row, same repo_id."""

    def _setup(self, tmp_path, rc):
        _save_repos(rc)
        store = GitStore(tmp_path / "github.db")
        rid = store.register_repo(rc)
        store.upsert_issues(rid, [_issue(1), _issue(2, is_pr=1,
                                                    merged="2026-11-01T10:00:00")])
        return store, rid

    def test_synthetic_rename_updates_the_row(self, cfg_home):
        from hub.harvesters.github_harvester import sync_repo_identity
        rc = remote_repo_config("acme/widgets")
        store, rid = self._setup(cfg_home, rc)
        try:
            client = FakeClient({"acme/widgets": {"id": 4242,
                                                  "full_name": "acme/widgets"}})
            status, etag, new = sync_repo_identity(store, client, rid, rc)
            assert (status, etag, new) == (200, '"etag-1"', None)
            assert store.get_github_id(rid) == 4242

            # Transferred to acme-labs and renamed: GitHub's redirect answers
            # the old name with the new full_name, same numeric id.
            client.repos["acme/widgets"] = {"id": 4242,
                                            "full_name": "acme-labs/gadgets"}
            _, _, new = sync_repo_identity(store, client, rid, rc)
            assert new == "remote:github.com/acme-labs/gadgets"
            repos = store.list_repos()
            assert len(repos) == 1  # updated, not duplicated
            row = repos[0]
            assert row["id"] == rid and row["github_id"] == 4242
            assert (row["owner"], row["repo_name"]) == ("acme-labs", "gadgets")
            assert row["path"] == new
            assert row["remote_url"] == "github.com/acme-labs/gadgets"
            n = store._get_conn().execute(
                "SELECT COUNT(*) FROM github_issues WHERE repo_id = ?",
                (rid,)).fetchone()[0]
            assert n == 2  # the series stays attached
            assert store.get_repo_id(new) == rid
            cfg = load_config().repos
            assert len(cfg) == 1
            assert (cfg[0].path, cfg[0].owner, cfg[0].repo, cfg[0].kind) == (
                new, "acme-labs", "gadgets", "remote")
        finally:
            store.close()

    def test_code_repo_rename_keeps_its_path(self, cfg_home):
        from hub.harvesters.github_harvester import sync_repo_identity
        rc = _code_repo()
        store, rid = self._setup(cfg_home, rc)
        try:
            client = FakeClient({"acme/widget-services": {
                "id": 7, "full_name": "acme/widget-platform"}})
            _, _, new = sync_repo_identity(store, client, rid, rc)
            assert new == rc.path
            row = store.list_repos()[0]
            assert row["path"] == rc.path and row["repo_name"] == "widget-platform"
            assert load_config().repos[0].repo == "widget-platform"
        finally:
            store.close()

    def test_duplicate_registration_is_not_merged(self, cfg_home):
        from hub.harvesters.github_harvester import sync_repo_identity
        a = remote_repo_config("acme/widgets")
        b = remote_repo_config("acme/gadgets")
        _save_repos(a, b)
        store = GitStore(cfg_home / "github.db")
        try:
            ra = store.register_repo(a)
            rb = store.register_repo(b)
            store.set_github_identity(ra, 4242, "acme", "widgets")
            client = FakeClient({"acme/gadgets": {"id": 4242,
                                                  "full_name": "acme/widgets"}})
            _, _, new = sync_repo_identity(store, client, rb, b)
            assert new is None
            assert store.get_github_id(rb) is None
            assert len(store.list_repos()) == 2
        finally:
            store.close()

    def test_failed_call_changes_nothing(self, cfg_home):
        from hub.harvesters.github_harvester import sync_repo_identity
        rc = remote_repo_config("acme/widgets")
        store, rid = self._setup(cfg_home, rc)
        try:
            status, _, new = sync_repo_identity(store, FakeClient(status=0),
                                                rid, rc)
            assert (status, new) == (0, None)
            assert store.get_github_id(rid) is None
        finally:
            store.close()

    def test_harvester_throttles_and_reloads_config(self, cfg_home):
        from hub.harvesters.github_harvester import GitHubHarvester
        rc = remote_repo_config("acme/widgets")
        store, rid = self._setup(cfg_home, rc)
        try:
            client = FakeClient({"acme/widgets": {"id": 4242,
                                                  "full_name": "acme/widgets"}})
            h = GitHubHarvester(store, client)
            h._running = True
            h._poll_identities()
            h._poll_identities()  # within IDENTITY_INTERVAL: no second call
            assert len(client.calls) == 1
            # The next check (interval elapsed) sends the ETag and follows a rename.
            h._identity_checked[rid] -= h.IDENTITY_INTERVAL + 1
            client.repos["acme/widgets"] = {"id": 4242,
                                            "full_name": "acme/widgets-v2"}
            h._poll_identities()
            assert client.calls[-1][2] == '"etag-1"'
            assert h._config_cache is None  # renamed → config re-read
            assert store.list_repos()[0]["repo_name"] == "widgets-v2"
        finally:
            store.close()


# --- CLI registration -----------------------------------------------------------

class TestRegister:
    def test_add_then_renamed_readd_is_not_a_new_row(self, cfg_home):
        from hub.cli import _register_remote_repo
        store = GitStore(cfg_home / "github.db")
        try:
            client = FakeClient({"acme/widgets": {"id": 4242,
                                                  "full_name": "acme/Widgets"}})
            assert _register_remote_repo(store, client, "acme", "widgets") == (
                "added", "acme/Widgets")  # canonical casing from the API
            assert _register_remote_repo(store, client, "acme", "widgets")[0] == "exists"
            # Later the repo moves; the owner adds it by its new name.
            client.repos["acme-labs/gadgets"] = {"id": 4242,
                                                 "full_name": "acme-labs/gadgets"}
            outcome, name = _register_remote_repo(store, client, "acme-labs",
                                                  "gadgets")
            assert outcome == "renamed" and name == "acme/Widgets → acme-labs/gadgets"
            repos = store.list_repos()
            assert len(repos) == 1 and repos[0]["path"] == (
                "remote:github.com/acme-labs/gadgets")
            assert [r.path for r in load_config().repos] == [repos[0]["path"]]
        finally:
            store.close()

    def test_without_token_registers_without_id(self, cfg_home):
        from hub.cli import _register_remote_repo
        store = GitStore(cfg_home / "github.db")
        try:
            assert _register_remote_repo(store, None, "acme", "widgets")[0] == "added"
            assert store.list_repos()[0]["github_id"] is None
            assert store.list_repos()[0]["kind"] == "remote"
        finally:
            store.close()

    def test_not_found_is_an_error(self, cfg_home):
        from hub.cli import _register_remote_repo
        store = GitStore(cfg_home / "github.db")
        try:
            outcome, _ = _register_remote_repo(store, FakeClient({}), "acme", "nope")
            assert outcome == "error"
            assert store.list_repos() == [] and load_config().repos == []
        finally:
            store.close()

    def test_code_clone_already_registered_counts_as_exists(self, cfg_home):
        from hub.cli import _register_remote_repo
        _save_repos(_code_repo())
        store = GitStore(cfg_home / "github.db")
        try:
            assert _register_remote_repo(store, None, "acme",
                                         "widget-services")[0] == "exists"
            assert len(load_config().repos) == 1
        finally:
            store.close()


class FakeOwnerClient(FakeClient):
    def __init__(self, listed, status=200, truncated=False):
        super().__init__()
        self.listed = listed
        self.list_status = status
        self.truncated = truncated

    def list_owner_repos(self, owner, max_repos=300):
        return self.list_status, self.listed[:max_repos], self.truncated


def _owner_args(**kw):
    d = dict(owner="acme", include=None, dry_run=False, limit=300,
             path=".", no_github=False, all_history=False)
    d.update(kw)
    return argparse.Namespace(**d)


class TestAddOwner:
    LISTED = [
        {"id": 1, "full_name": "acme/widgets", "archived": False},
        {"id": 2, "full_name": "acme/widget-docs", "archived": True},
        {"id": 3, "full_name": "acme/gadgets", "archived": False},
    ]

    def _run(self, cfg_home, client, capsys, **kw):
        from hub import cli
        with patch.object(cli, "_github_client_for", return_value=client), \
             patch.object(GitStore, "DEFAULT_DB_PATH", cfg_home / "github.db"):
            cli._repo_add_owner(_owner_args(**kw))
        return capsys.readouterr().out

    def test_dry_run_lists_without_registering(self, cfg_home, capsys):
        out = self._run(cfg_home, FakeOwnerClient(self.LISTED), capsys,
                        dry_run=True, include=["widget*"])
        assert "acme/widgets" in out and "acme/widget-docs" in out
        assert "acme/gadgets" not in out
        assert load_config().repos == []

    def test_registers_by_id_and_is_idempotent(self, cfg_home, capsys):
        client = FakeOwnerClient(self.LISTED)
        self._run(cfg_home, client, capsys)
        assert sorted(r.repo for r in load_config().repos) == [
            "gadgets", "widget-docs", "widgets"]
        assert all(r.kind == "remote" for r in load_config().repos)
        assert client.calls == []  # ids came with the listing: no extra call
        store = GitStore(cfg_home / "github.db")
        try:
            assert sorted(r["github_id"] for r in store.list_repos()) == [1, 2, 3]
        finally:
            store.close()
        # A rename on GitHub between two runs: same id, new name → renamed.
        client.listed = [dict(self.LISTED[0]), dict(self.LISTED[1]),
                         {"id": 3, "full_name": "acme/gizmos", "archived": False}]
        out = self._run(cfg_home, client, capsys)
        assert "acme/gadgets → acme/gizmos" in out
        assert sorted(r.repo for r in load_config().repos) == [
            "gizmos", "widget-docs", "widgets"]

    def test_listing_failure_exits(self, cfg_home, capsys):
        with pytest.raises(SystemExit):
            self._run(cfg_home, FakeOwnerClient([], status=404), capsys)


# --- GitHubClient.list_owner_repos / get_repo -----------------------------------

class TestClient:
    def _client(self, pages_by_base, rate=5000):
        from hub.integrations.github_client import GitHubClient
        c = GitHubClient("t")
        c._rate_remaining = rate
        seen = []

        def rest_get(path, params=None, etag=None):
            seen.append((path, dict(params or {})))
            if path not in pages_by_base:
                return 404, None, None
            page = (params or {}).get("page", 1)
            pages = pages_by_base[path]
            return 200, (pages[page - 1] if page <= len(pages) else []), None

        c.rest_get = rest_get
        return c, seen

    @staticmethod
    def _page(start, n):
        return [{"id": i, "full_name": f"acme/r{i}"} for i in range(start, start + n)]

    def test_user_fallback_and_pagination(self):
        c, seen = self._client({"/users/ownerhandle/repos": [
            self._page(0, 2), self._page(2, 1)]})
        status, repos, truncated = c.list_owner_repos("ownerhandle", per_page=2)
        assert status == 200 and not truncated
        assert [r["id"] for r in repos] == [0, 1, 2]
        assert seen[0][0] == "/orgs/ownerhandle/repos"
        assert seen[1][1]["type"] == "owner"

    def test_limit_truncates(self):
        c, seen = self._client({"/orgs/acme/repos": [
            self._page(0, 2), self._page(2, 2), self._page(4, 2)]})
        status, repos, truncated = c.list_owner_repos("acme", max_repos=3,
                                                      per_page=2)
        assert (status, len(repos), truncated) == (200, 3, True)
        assert len(seen) == 2  # the third page was never asked for

    def test_low_rate_budget_stops(self):
        c, seen = self._client({"/orgs/acme/repos": [
            self._page(0, 2), self._page(2, 2)]}, rate=10)
        status, repos, truncated = c.list_owner_repos("acme", per_page=2)
        assert (status, len(repos), truncated) == (200, 2, True)
        assert len(seen) == 1

    def test_get_repo_shape(self):
        from hub.integrations.github_client import GitHubClient
        c = GitHubClient("t")
        with patch.object(c, "rest_get", return_value=(200, [], None)):
            assert c.get_repo("acme", "widgets") == (0, None, None)
        with patch.object(c, "rest_get",
                          return_value=(200, {"id": 1}, '"e"')) as m:
            assert c.get_repo("acme", "widgets", etag='"x"') == (200, {"id": 1}, '"e"')
            m.assert_called_with("/repos/acme/widgets", etag='"x"')


# --- portfolio ------------------------------------------------------------------

class TestPortfolio:
    def _dbs(self, tmp_path, issues):
        from hub.cache.workspace_store import WorkspaceStore
        WorkspaceStore(tmp_path / "workspace.db").close()
        ev = tmp_path / "events.db"
        sqlite3.connect(ev).close()
        gh = tmp_path / "github.db"
        store = GitStore(gh)
        rid = store.register_repo(remote_repo_config("acme/widgets"))
        store.register_repo(remote_repo_config("acme/dormant"))
        store.upsert_issues(rid, issues)
        store.close()
        return str(ev), str(tmp_path / "workspace.db"), str(gh)

    def _run(self, tmp_path, issues):
        from hub.mcp_server import _portfolio_production
        ev, ws, gh = self._dbs(tmp_path, issues)
        return _portfolio_production(ev, ws, days=7, today="2026-11-04",
                                     hide=False, github_db=gh, tz=timezone.utc)

    def test_remote_repo_with_merged_prs_appears_as_no_clone(self, tmp_path):
        data = self._run(tmp_path, [
            _issue(1, is_pr=1, state="closed", created="2026-10-30T10:00:00",
                   merged="2026-11-02T10:00:00"),
            _issue(2, is_pr=1, state="closed", created="2026-08-01T10:00:00",
                   merged="2026-08-02T10:00:00"),
            _issue(3, created="2026-11-01T10:00:00"),
        ])
        rows = {p["project_key"]: p for p in data["projects"]}
        assert set(rows) == {"git_remote:github.com/acme/widgets"}  # dormant: none
        row = rows["git_remote:github.com/acme/widgets"]
        assert row["no_clone"] is True and row["registered"] is True
        assert row["board"] is False
        assert row["project_label"] == "acme/widgets"
        assert (row["merged_prs"], row["merged_prs_window"]) == (2, 1)
        assert row["open_issues"] == 1
        assert row["sessions"] == 0 and row["days"] == {}
        assert row["last_day"] == "2026-11-02"

    def test_no_activity_in_window_no_row(self, tmp_path):
        data = self._run(tmp_path, [
            _issue(1, is_pr=1, state="closed", created="2026-08-01T10:00:00",
                   merged="2026-08-02T10:00:00")])
        assert data["projects"] == []

    def test_keyed_project_key(self):
        from hub.cache.workspace_store import _keyed_repo_project_key
        assert _keyed_repo_project_key("remote:github.com/Acme/Widgets") == (
            "git_remote:github.com/acme/widgets")
        assert _keyed_repo_project_key("board:github.com/acme/backlog") == (
            "git_remote:github.com/acme/backlog")
        assert _keyed_repo_project_key("/Users/u/work/widgets") is None

    def test_annotation_flag(self, tmp_path):
        from hub.mcp_server import (
            _annotate_github_measured_projects, _board_remote_keys,
            _registered_remote_keys, _remote_only_keys,
        )
        db = str(tmp_path / "github.db")
        store = GitStore(tmp_path / "github.db")
        store.register_repo(remote_repo_config("acme/widgets"))
        store.register_repo(_code_repo())
        store.close()
        projects = [{"project_key": "git_remote:github.com/acme/widgets"},
                    {"project_key": "git_remote:github.com/acme/widget-services"}]
        _annotate_github_measured_projects(
            projects, _registered_remote_keys(db), _board_remote_keys(db),
            _remote_only_keys(db))
        assert [p["no_clone"] for p in projects] == [True, False]
        assert _remote_only_keys(str(tmp_path / "missing.db")) == set()
