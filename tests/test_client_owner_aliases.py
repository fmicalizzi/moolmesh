"""One client spanning several owners (#95): owner aliases, a shared client
registry, and confidential repos that are never read. Synthetic fixtures
(Acme, Globex), no network."""
import argparse
from unittest.mock import MagicMock, patch

import pytest

from hub.cache.git_store import GitStore
from hub.cache.portfolio_clients import group_by_client, resolve_client
from hub.config import (
    Exclusions, HubConfig, RepoConfig, exclusions_of,
    load_client_registry, load_config, remote_repo_config, save_config,
)


@pytest.fixture
def cfg_home(tmp_path):
    """config.toml under tmp_path (never the real ~/.moolmesh)."""
    with patch("hub.config.CONFIG_PATH", tmp_path / "config.toml"), \
         patch("hub.config.CONFIG_DIR", tmp_path):
        yield tmp_path


def _write_config(home, body: str) -> None:
    (home / "config.toml").write_text(body, encoding="utf-8")


def _code_repo(owner, name, path=None) -> RepoConfig:
    return RepoConfig(path=path or f"/Users/u/work/{name}",
                      remote_url=f"github.com/{owner}/{name}",
                      owner=owner, repo=name,
                      added_at="2026-10-01T00:00:00+00:00")


def _project(owner, name, **kw):
    remote = f"github.com/{owner}/{name}"
    d = {"project_key": f"git_remote:{remote}", "_remote_url": remote,
         "_anchor_path": f"/Users/u/work/{name}", "project_label": name,
         "session_touches": 1, "fs_touches": 0, "git_touches": 0,
         "last_activity": "2026-11-01"}
    d.update(kw)
    return d


def _ladder(project_key=None, remote=None, anchor=None, **kw):
    kw.setdefault("client_orgs", set())
    kw.setdefault("personal_orgs", set())
    kw.setdefault("overrides", {})
    return resolve_client(project_key, remote, anchor, **kw)


# --- config -------------------------------------------------------------------

class TestConfig:
    def test_round_trip(self, cfg_home):
        cfg = HubConfig(client_owner_aliases={"acme-labs": "acme"},
                        client_registry="~/shared/clients.toml",
                        exclude_owners=["initech"],
                        exclude_repos=["globex/secret-plans"])
        save_config(cfg)
        text = (cfg_home / "config.toml").read_text()
        assert 'client_owner_aliases = {"acme-labs" = "acme"}' in text
        loaded = load_config()
        assert loaded.client_owner_aliases == {"acme-labs": "acme"}
        assert loaded.client_registry == "~/shared/clients.toml"
        assert loaded.exclude_owners == ["initech"]
        assert loaded.exclude_repos == ["globex/secret-plans"]

    def test_defaults_write_nothing(self, cfg_home):
        save_config(HubConfig())
        text = (cfg_home / "config.toml").read_text()
        for key in ("client_owner_aliases", "client_registry",
                    "exclude_owners", "exclude_repos"):
            assert key not in text

    def test_registry_is_never_copied_into_config(self, cfg_home):
        (cfg_home / "clients.toml").write_text(
            'version = 1\nexclude_owners = ["initech"]\n'
            '[client_owner_aliases]\n"acme-labs" = "acme"\n')
        _write_config(cfg_home, '[workspace]\nclient_registry = "clients.toml"\n')
        cfg = load_config()
        assert cfg.registry is not None
        save_config(cfg)
        text = (cfg_home / "config.toml").read_text()
        assert "initech" not in text and "acme-labs" not in text
        assert 'client_registry = "clients.toml"' in text


# --- registry -----------------------------------------------------------------

class TestRegistry:
    def test_merge_local_wins_lists_union(self, cfg_home):
        (cfg_home / "clients.toml").write_text(
            'version = 1\n'
            'client_orgs = ["acme"]\n'
            'exclude_repos = ["globex/secret-plans"]\n'
            '[client_owner_aliases]\n"acme-labs" = "acme"\n"globex-dev" = "globex"\n'
            '[client_overrides]\n"git_remote:github.com/x/y" = "acme"\n')
        _write_config(cfg_home, (
            '[workspace]\n'
            'client_registry = "clients.toml"\n'
            'client_orgs = ["globex"]\n'
            'exclude_owners = ["initech"]\n'
            'client_owner_aliases = {"Globex-Dev" = "acme"}\n'))
        cfg = load_config()
        assert cfg.effective_client_orgs() == ["globex", "acme"]
        # Local alias wins on the same owner, regardless of case.
        assert cfg.effective_owner_aliases() == {
            "acme-labs": "acme", "globex-dev": "acme"}
        assert cfg.effective_client_overrides() == {
            "git_remote:github.com/x/y": "acme"}
        ex = cfg.exclusions()
        assert ex.matches("Initech", "anything")
        assert ex.matches("globex", "Secret-Plans")
        assert not ex.matches("globex", "widgets")

    def test_missing_file_warns_once_and_keeps_local(self, cfg_home):
        _write_config(cfg_home, (
            '[workspace]\nclient_registry = "/nonexistent/clients.toml"\n'
            'client_orgs = ["acme"]\n'))
        logger = MagicMock()
        with patch("hub.log.get", return_value=logger), \
             patch("hub.config._registry_warned", set()):
            cfg = load_config()
            load_config()  # the harvester reloads often: warned once only
        assert cfg.registry is None
        assert cfg.effective_client_orgs() == ["acme"]
        assert logger.warning.call_count == 1
        assert "no existe" in logger.warning.call_args.args

    @pytest.mark.parametrize("body", [
        "version = 1\nclient_orgs = [",          # broken TOML
        'version = 2\nclient_orgs = ["acme"]\n',  # unknown version
    ])
    def test_invalid_file_is_ignored(self, tmp_path, body):
        p = tmp_path / "clients.toml"
        p.write_text(body)
        assert load_client_registry(str(p)) is None

    def test_wrong_type_key_skipped_rest_applies(self, tmp_path):
        p = tmp_path / "clients.toml"
        p.write_text('version = 1\nclient_orgs = "acme"\n'
                     'exclude_owners = ["initech"]\n')
        reg = load_client_registry(str(p))
        assert reg is not None
        assert reg.client_orgs == [] and reg.exclude_owners == ["initech"]

    def test_two_machines_same_registry_same_grouping(self, tmp_path):
        """Acceptance: one registry, two machines (different local identity) →
        the same client grouping."""
        reg = tmp_path / "clients.toml"
        reg.write_text('version = 1\nclient_orgs = ["acme", "globex"]\n'
                       '[client_owner_aliases]\n"ownerhandle" = "acme"\n')
        projects = [_project("acme", "widgets"), _project("ownerhandle", "tools"),
                    _project("globex", "widget-services")]
        groupings = []
        for personal in (["ownerhandle"], ["someone-else"]):
            cfg = HubConfig(personal_orgs=personal, client_registry=str(reg),
                            registry=load_client_registry(str(reg)))
            h = group_by_client(
                [dict(p) for p in projects],
                client_orgs=set(cfg.effective_client_orgs()),
                personal_orgs=set(cfg.personal_orgs),
                overrides=cfg.effective_client_overrides(),
                owner_aliases=cfg.effective_owner_aliases())
            groupings.append(sorted(
                (c["client_key"], sorted(p["project_label"] for p in c["projects"]))
                for c in h["clients"]))
        assert groupings[0] == groupings[1] == [
            ("client:acme", ["tools", "widgets"]),
            ("client:globex", ["widget-services"])]


# --- attribution ladder -------------------------------------------------------

class TestLadder:
    ALIASES = {"acme-labs": "Acme"}

    def test_new_repo_in_aliased_owner_lands_in_client(self):
        ref = _ladder(remote="github.com/acme-labs/brand-new-repo",
                      owner_aliases=self.ALIASES)
        assert ref == {"bucket": "client", "client_key": "client:acme",
                       "client_label": "Acme"}

    def test_alias_beats_personal_and_client_orgs(self):
        ref = _ladder(remote="github.com/ownerhandle/side",
                      personal_orgs={"ownerhandle"}, client_orgs={"ownerhandle"},
                      owner_aliases={"ownerhandle": "globex"})
        assert ref["client_key"] == "client:globex"

    def test_override_beats_alias(self):
        key = "git_remote:github.com/acme-labs/odd-one"
        ref = _ladder(project_key=key, remote="github.com/acme-labs/odd-one",
                      overrides={key: "globex"}, owner_aliases=self.ALIASES)
        assert ref["client_key"] == "client:globex"

    def test_client_orgs_still_apply_without_alias(self):
        ref = _ladder(remote="github.com/globex/widgets", client_orgs={"globex"},
                      owner_aliases=self.ALIASES)
        assert ref["client_key"] == "client:globex"
        assert _ladder(remote="github.com/initech/x",
                       owner_aliases=self.ALIASES)["bucket"] == "external"

    def test_gitless_folder_alias(self):
        ref = _ladder(anchor="/Users/u/work/_acme-labs/notes",
                      owner_aliases=self.ALIASES)
        assert ref["client_key"] == "client:acme"

    def test_child_majority_counts_aliased_owner_with_client(self):
        ref = _ladder(anchor="/Users/u/work/SHARED-WORKSPACE",
                      child_orgs=["acme-labs", "Acme", "globex"],
                      owner_aliases=self.ALIASES)
        assert ref["client_key"] == "client:acme"

    def test_alias_and_client_org_share_one_node(self):
        projects = [_project("acme", "widgets"),
                    _project("acme-labs", "gadgets")]
        h = group_by_client(projects, client_orgs={"acme"}, personal_orgs=set(),
                            overrides={}, owner_aliases=self.ALIASES)
        assert len(h["clients"]) == 1
        assert len(h["clients"][0]["projects"]) == 2

    def test_aliases_alone_activate_the_hierarchy(self):
        h = group_by_client([_project("acme-labs", "gadgets")], client_orgs=set(),
                            personal_orgs=set(), overrides={},
                            owner_aliases=self.ALIASES)
        assert [c["client_key"] for c in h["clients"]] == ["client:acme"]


# --- exclusions ---------------------------------------------------------------

class TestExclusions:
    def test_matching(self):
        ex = Exclusions.build(["Initech"], ["github.com/globex/Secret-Plans",
                                            "https://github.com/acme/vault.git",
                                            "not a slug"])
        assert ex.matches("initech", "x") and ex.matches("GLOBEX", "secret-plans")
        assert ex.matches("acme", "vault") and not ex.matches("acme", "widgets")
        assert ex.matches_remote("git_remote:github.com/globex/secret-plans")
        assert ex.matches_remote("github.com/initech/anything")
        assert not ex.matches_remote("github.com/globex/widgets")
        assert not ex.matches_remote(None) and not Exclusions()

    def test_stand_in_config_excludes_nothing(self):
        assert not exclusions_of(MagicMock())
        assert not exclusions_of(object())


def _excluded_config(home, *repos):
    cfg = HubConfig(exclude_owners=["initech"],
                    exclude_repos=["globex/secret-plans"])
    cfg.repos = list(repos)
    save_config(cfg)
    return load_config()


class TestNeverRead:
    """Mandatory: an excluded repo yields no API call and no git read."""

    def test_git_harvester_reads_no_git_for_excluded(self, cfg_home):
        from hub.harvesters import git_harvester as gh
        secret = _code_repo("globex", "secret-plans")
        owner_wide = _code_repo("initech", "payroll")
        public = _code_repo("acme", "widgets")
        _excluded_config(cfg_home, secret, owner_wide, public)
        store = GitStore(cfg_home / "github.db")
        try:
            for r in (secret, owner_wide, public):
                store.register_repo(r)
            h = gh.GitHarvester(store)
            h._running = True
            fetched = []

            def fake_fetch(path):
                fetched.append(path)
                return False

            def one_cycle(_timeout):
                h._running = False

            with patch.object(gh, "git_fetch", side_effect=fake_fetch), \
                 patch.object(gh, "get_remote_refs") as refs, \
                 patch.object(gh, "git_log_range") as log_range, \
                 patch.object(h._stop_event, "wait", side_effect=one_cycle):
                h._harvest_loop()
            assert fetched == [public.path]
            refs.assert_not_called()
            log_range.assert_not_called()
        finally:
            store.close()

    def test_github_harvester_makes_no_api_call_for_excluded(self, cfg_home):
        from hub.harvesters.github_harvester import GitHubHarvester
        repos = [remote_repo_config("globex/secret-plans"),
                 remote_repo_config("initech/payroll")]
        cfg = _excluded_config(cfg_home, *repos)
        store = GitStore(cfg_home / "github.db")
        try:
            for r in repos:
                store.register_repo(r)
            client = MagicMock()
            h = GitHubHarvester(store, github_client=client)
            h._running = True
            with patch.object(h, "_get_cached_config", return_value=cfg):
                h._poll_all_repos(h._poll_issues_prs)
                h._poll_all_repos(h._poll_milestones)
                h._poll_all_repos(h._poll_projects)
                h._poll_identities()
            assert client.mock_calls == []
            # Control: the same harvester does call the API for a normal repo.
            public = remote_repo_config("acme/widgets")
            store.register_repo(public)
            cfg.repos.append(public)
            with patch.object(h, "_get_cached_config", return_value=cfg):
                h._poll_all_repos(h._poll_milestones)
            assert client.mock_calls
        finally:
            store.close()

    def test_previously_registered_data_is_kept(self, cfg_home):
        """Excluding stops reads; it does not delete what is stored."""
        secret = remote_repo_config("globex/secret-plans")
        _excluded_config(cfg_home, secret)
        store = GitStore(cfg_home / "github.db")
        try:
            store.register_repo(secret)
            assert [r["repo_name"] for r in store.list_repos()] == ["secret-plans"]
            assert [r.repo for r in load_config().repos] == ["secret-plans"]
        finally:
            store.close()


class TestRepoAdd:
    def _owner_args(self, **kw):
        d = dict(owner="acme", include=None, dry_run=False, limit=300,
                 path=".", no_github=False, all_history=False)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_add_owner_excluded_makes_no_api_call(self, cfg_home, capsys):
        from hub import cli
        _excluded_config(cfg_home)
        client = MagicMock()
        with patch.object(cli, "_github_client_for", return_value=client) as gc, \
             pytest.raises(SystemExit):
            cli._repo_add_owner(self._owner_args(owner="Initech"))
        gc.assert_not_called()
        assert client.mock_calls == []
        assert "excluido" in capsys.readouterr().out

    def test_add_owner_skips_excluded_repos(self, cfg_home, capsys):
        from hub import cli
        _excluded_config(cfg_home)
        client = MagicMock()
        client.list_owner_repos.return_value = (200, [
            {"id": 1, "full_name": "globex/widgets"},
            {"id": 2, "full_name": "globex/secret-plans"},
        ], False)
        with patch.object(cli, "_github_client_for", return_value=client), \
             patch.object(GitStore, "DEFAULT_DB_PATH", cfg_home / "github.db"):
            cli._repo_add_owner(self._owner_args(owner="globex"))
        assert [r.repo for r in load_config().repos] == ["widgets"]
        assert "1 excluded" in capsys.readouterr().out

    def test_add_remote_excluded_makes_no_api_call(self, cfg_home, capsys):
        from hub import cli
        _excluded_config(cfg_home)
        with patch.object(cli, "_github_client_for") as gc:
            cli._repo_add_remote(argparse.Namespace(
                path="globex/secret-plans", no_github=False, all_history=False))
        gc.assert_not_called()
        assert load_config().repos == []

    def test_register_refuses_excluded_even_after_api_rename(self, cfg_home):
        from hub.cli import _register_remote_repo
        _excluded_config(cfg_home)
        client = MagicMock()
        client.get_repo.return_value = (
            200, {"id": 9, "full_name": "initech/moved-here"}, None)
        store = GitStore(cfg_home / "github.db")
        try:
            outcome, _ = _register_remote_repo(store, client, "acme", "old-name")
            assert outcome == "error"
            outcome, _ = _register_remote_repo(store, client, "initech", "x")
            assert outcome == "error"
            assert store.list_repos() == [] and load_config().repos == []
        finally:
            store.close()


# --- MCP / portfolio surfaces -------------------------------------------------

class TestSurfaces:
    EXCL = Exclusions.build(["initech"], ["globex/secret-plans"])

    def test_grouped_payload_drops_excluded(self):
        from hub.mcp_server import _drop_excluded_grouped
        child_secret = _project("globex", "secret-plans")
        grouped = {
            "projects": [_project("acme", "widgets", children=[child_secret]),
                         _project("initech", "payroll")],
            "unclassified": [], "reference": [_project("globex", "secret-plans")],
            "temporary": [],
            "summary": {"projects": 2, "children": 1, "reference": 1,
                        "unclassified": 0, "temporary": 0},
        }
        _drop_excluded_grouped(grouped, self.EXCL)
        assert [p["project_label"] for p in grouped["projects"]] == ["widgets"]
        assert grouped["projects"][0]["children"] == []
        assert grouped["reference"] == []
        assert grouped["summary"]["projects"] == 1
        assert grouped["summary"]["children"] == 0
        assert grouped["summary"]["reference"] == 0

    def test_board_issues_answers_excluded_as_unknown(self, tmp_path):
        from hub import mcp_server
        db = tmp_path / "github.db"
        store = GitStore(db)
        store.register_repo(remote_repo_config("globex/secret-plans"))
        store.close()
        with patch.object(mcp_server, "_exclusions", return_value=self.EXCL):
            res = mcp_server._get_board_issues(str(db), "globex/secret-plans")
        assert res == {"error": "repo not registered: globex/secret-plans"}

    def test_portfolio_grouped_end_to_end_filters(self, tmp_path):
        from hub import mcp_server
        grouped = {"projects": [_project("acme", "widgets"),
                                _project("globex", "secret-plans")],
                   "unclassified": [], "reference": [], "temporary": [],
                   "summary": {"projects": 2}}
        fake_store = MagicMock()
        fake_store.get_portfolio_grouped.return_value = grouped
        fake_store.derive_project_states.return_value = {}
        wdb = tmp_path / "workspace.db"
        wdb.write_bytes(b"")
        with patch("hub.cache.workspace_store.WorkspaceStore",
                   return_value=fake_store), \
             patch.object(mcp_server, "_exclusions", return_value=self.EXCL), \
             patch("hub.config.load_config", return_value=HubConfig()):
            out = mcp_server._get_portfolio_grouped(
                str(wdb), events_db=str(tmp_path / "e.db"),
                github_db=str(tmp_path / "g.db"))
        labels = [p["project_label"] for p in out["projects"]]
        assert labels == ["widgets"]
