"""Client/org hierarchy over the portfolio — issue #29 (Unit 3)."""

import sqlite3

from hub.cache.portfolio_clients import (
    _norm_org,
    group_by_client,
    resolve_client,
    strip_internal,
    suggest_client_orgs,
)


def _proj(key, label, *, remote=None, anchor=None, sess=0, fs=0, git=0,
          days=None, last=None, harness=0, state=None):
    return {
        "project_key": key, "project_label": label,
        "_remote_url": remote, "_anchor_path": anchor,
        "session_touches": sess, "fs_touches": fs, "git_touches": git,
        "active_days": len(days or []), "last_activity": last,
        "_day_set": list(days or []), "collapsed_harness": harness,
        "children": [], **({"state": state} if state else {}),
    }


CLIENT_ORGS = {"eventsmx", "ddtyi"}
PERSONAL = {"fmicalizzi"}


class TestResolveClient:
    def test_git_org_groups_into_client(self):
        ref = resolve_client(
            "git_remote:github.com/eventsmx/fiestados",
            "github.com/eventsmx/fiestados", None,
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "client"
        assert ref["client_key"] == "client:eventsmx"

    def test_personal_org_stays_loose(self):
        ref = resolve_client(
            "git_remote:github.com/fmicalizzi/moolmesh",
            "github.com/fmicalizzi/moolmesh", None,
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "personal"
        assert ref["client_key"] is None

    def test_unknown_org_goes_external(self):
        ref = resolve_client(
            "git_remote:github.com/homebrew/brew",
            "github.com/homebrew/brew", None,
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "external"

    def test_gitless_parent_folder_matches_client(self):
        # /Downloads/Claude/ddtyi/inter-areas → parent folder ddtyi → client.
        ref = resolve_client(
            "path_hash:abc", None,
            "/Users/u/Downloads/Claude/ddtyi/inter-areas",
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "client"
        assert ref["client_key"] == "client:ddtyi"

    def test_gitless_materials_folder_reconciles_onto_git_client(self):
        # The non-git _eventsmx materials folder normalizes onto client eventsmx.
        ref = resolve_client(
            "path_hash:xyz", None,
            "/Users/u/Downloads/Claude/_eventsmx",
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "client"
        assert ref["client_key"] == "client:eventsmx"

    def test_producciones_is_shared_not_client(self):
        ref = resolve_client(
            "git_root:/Users/u/Downloads/Claude/PRODUCCIONES", None,
            "/Users/u/Downloads/Claude/PRODUCCIONES",
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["bucket"] == "shared"
        assert ref["client_key"] is None

    def test_case_insensitive_git_org(self):
        # A key/remote that survived un-normalized still matches lowercased org.
        ref = resolve_client(
            "git_remote:github.com/EventsMX/fiestados",
            "github.com/EventsMX/fiestados", None,
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL, overrides={})
        assert ref["client_key"] == "client:eventsmx"

    def test_manual_override_wins(self):
        ref = resolve_client(
            "path_hash:weird", None, "/tmp/whatever",
            client_orgs=CLIENT_ORGS, personal_orgs=PERSONAL,
            overrides={"path_hash:weird": "acme-corp"})
        assert ref["bucket"] == "client"
        assert ref["client_key"] == "client:acme-corp"
        assert ref["client_label"] == "acme-corp"


class TestGroupByClient:
    def test_flat_when_no_config(self):
        projects = [_proj("git_remote:github.com/eventsmx/x",
                          "x", remote="github.com/eventsmx/x")]
        h = group_by_client(projects, client_orgs=set(), personal_orgs=set(),
                            overrides={})
        assert h["clients"] == []
        assert h["external"] == []
        assert len(h["projects"]) == 1

    def test_groups_client_loose_and_external(self):
        projects = [
            _proj("git_remote:github.com/eventsmx/a", "a",
                  remote="github.com/eventsmx/a", sess=3, last="2026-01-02"),
            _proj("git_remote:github.com/eventsmx/b", "b",
                  remote="github.com/eventsmx/b", sess=2, last="2026-01-03"),
            _proj("git_remote:github.com/fmicalizzi/m", "m",
                  remote="github.com/fmicalizzi/m", sess=1),
            _proj("git_remote:github.com/homebrew/brew", "brew",
                  remote="github.com/homebrew/brew"),
        ]
        h = group_by_client(projects, client_orgs=CLIENT_ORGS,
                            personal_orgs=PERSONAL, overrides={})
        assert len(h["clients"]) == 1
        c = h["clients"][0]
        assert c["client_key"] == "client:eventsmx"
        assert len(c["projects"]) == 2
        assert c["session_touches"] == 5
        assert len(h["projects"]) == 1        # fmicalizzi loose
        assert h["projects"][0]["client_bucket"] == "personal"
        assert len(h["external"]) == 1        # homebrew

    def test_active_days_unioned_not_summed(self):
        projects = [
            _proj("git_remote:github.com/eventsmx/a", "a",
                  remote="github.com/eventsmx/a", days=["2026-01-01", "2026-01-02"]),
            _proj("git_remote:github.com/eventsmx/b", "b",
                  remote="github.com/eventsmx/b", days=["2026-01-02", "2026-01-03"]),
        ]
        h = group_by_client(projects, client_orgs=CLIENT_ORGS,
                            personal_orgs=PERSONAL, overrides={})
        # Union {01,02,03} = 3, NOT 2+2=4.
        assert h["clients"][0]["active_days"] == 3

    def test_client_inherits_hottest_state_and_sums_outcome(self):
        projects = [
            _proj("git_remote:github.com/eventsmx/a", "a",
                  remote="github.com/eventsmx/a",
                  state={"state": "pausado", "last_activity": "2026-01-01",
                         "merged_prs": 2, "closed_issues": 1, "open_issues": 0,
                         "outcome_measurable": True}),
            _proj("git_remote:github.com/eventsmx/b", "b",
                  remote="github.com/eventsmx/b",
                  state={"state": "activo", "last_activity": "2026-02-01",
                         "merged_prs": 3, "closed_issues": 0, "open_issues": 5,
                         "outcome_measurable": True}),
        ]
        h = group_by_client(projects, client_orgs=CLIENT_ORGS,
                            personal_orgs=PERSONAL, overrides={})
        st = h["clients"][0]["state"]
        assert st["state"] == "activo"        # hottest wins
        assert st["basis"] == "client_hottest"
        assert h["clients"][0]["outcome"] == {
            "merged_prs": 5, "closed_issues": 1, "open_issues": 5}

    def test_producciones_stays_top_level_own_node(self):
        projects = [
            _proj("git_root:/x/Claude/PRODUCCIONES", "PRODUCCIONES",
                  anchor="/x/Downloads/Claude/PRODUCCIONES"),
            _proj("git_remote:github.com/eventsmx/a", "a",
                  remote="github.com/eventsmx/a"),
        ]
        h = group_by_client(projects, client_orgs=CLIENT_ORGS,
                            personal_orgs=PERSONAL, overrides={})
        loose_labels = {p["project_label"] for p in h["projects"]}
        assert "PRODUCCIONES" in loose_labels
        # Never absorbed into a client node.
        for c in h["clients"]:
            assert all(p["project_label"] != "PRODUCCIONES"
                       for p in c["projects"])


class TestSuggestClientOrgs:
    def _github_db(self, path, owners):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, owner TEXT)")
        conn.executemany("INSERT INTO repos (owner) VALUES (?)",
                         [(o,) for o in owners])
        conn.commit()
        conn.close()

    def _workspace_db(self, path, remotes):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, remote_url TEXT)")
        conn.executemany("INSERT INTO workspaces (remote_url) VALUES (?)",
                         [(r,) for r in remotes])
        conn.commit()
        conn.close()

    def test_union_of_github_owners_and_multi_project_orgs(self, tmp_path):
        gdb = tmp_path / "github.db"
        wdb = tmp_path / "workspace.db"
        # github.db owners: EventsMX, avillegas (case-normalized).
        self._github_db(str(gdb), ["EventsMX", "avillegas"])
        # workspace orgs: eventsmx×2, ddtyi×2, homebrew×1.
        self._workspace_db(str(wdb), [
            "github.com/eventsmx/a", "github.com/eventsmx/b",
            "github.com/ddtyi/x", "github.com/ddtyi/y",
            "github.com/homebrew/brew",
        ])
        seed = suggest_client_orgs(str(wdb), str(gdb))
        assert seed == ["avillegas", "ddtyi", "eventsmx"]  # homebrew excluded (1)

    def test_empty_when_dbs_absent(self, tmp_path):
        assert suggest_client_orgs(str(tmp_path / "no.db"),
                                   str(tmp_path / "no.db")) == []


class TestStripInternal:
    def test_removes_join_only_fields(self):
        payload = {
            "clients": [{"client_key": "client:x", "client_label": "x",
                         "projects": [_proj("k", "l", remote="r")]}],
            "projects": [_proj("k2", "l2", anchor="/a")],
            "external": [_proj("k3", "l3")],
            "unclassified": [],
        }
        strip_internal(payload)
        for row in (payload["clients"][0]["projects"]
                    + payload["projects"] + payload["external"]):
            assert "_day_set" not in row
            assert "_remote_url" not in row
            assert "_anchor_path" not in row


class TestMcpIntegration:
    """The read wrapper: config resolution, masking, internal-field stripping."""

    def _grouped(self):
        return {
            "projects": [
                _proj("git_remote:github.com/eventsmx/a", "eventsmx/a",
                      remote="github.com/eventsmx/a", sess=3, days=["2026-01-01"]),
                _proj("git_remote:github.com/fmicalizzi/m", "fmicalizzi/m",
                      remote="github.com/fmicalizzi/m", sess=1),
                _proj("git_remote:github.com/homebrew/brew", "homebrew/brew",
                      remote="github.com/homebrew/brew"),
            ],
            "unclassified": [],
            "summary": {},
        }

    def _patch_config(self, monkeypatch, **kw):
        import hub.config as cfgmod
        from hub.config import HubConfig
        monkeypatch.setattr(cfgmod, "load_config", lambda: HubConfig(**kw))

    def test_apply_hierarchy_uses_explicit_config(self, monkeypatch):
        from hub.mcp_server import _apply_client_hierarchy
        self._patch_config(monkeypatch, personal_orgs=["fmicalizzi"],
                          client_orgs=["eventsmx"])
        out = _apply_client_hierarchy(self._grouped(), "/no/ws.db", "/no/gh.db")
        assert [c["client_key"] for c in out["clients"]] == ["client:eventsmx"]
        assert len(out["projects"]) == 1                       # fmicalizzi loose
        assert len(out["external"]) == 1                       # homebrew
        assert out["summary"]["clients"] == 1
        assert out["summary"]["external"] == 1
        # Internal join-only fields stripped.
        assert "_remote_url" not in out["clients"][0]["projects"][0]

    def test_flat_without_owner_identity(self, monkeypatch):
        from hub.mcp_server import _apply_client_hierarchy
        self._patch_config(monkeypatch)  # no personal_orgs, no client_orgs
        out = _apply_client_hierarchy(self._grouped(), "/no/ws.db", "/no/gh.db")
        assert out["clients"] == []
        assert out["external"] == []
        assert len(out["projects"]) == 3

    def test_client_label_masked(self, monkeypatch):
        from hub.mcp_server import _apply_client_hierarchy, _mask_grouped
        self._patch_config(monkeypatch, personal_orgs=["fmicalizzi"],
                          client_orgs=["eventsmx"])
        out = _apply_client_hierarchy(self._grouped(), "/no/ws.db", "/no/gh.db")
        masked = _mask_grouped(out, hide=True)
        c = masked["clients"][0]
        assert c["client_label"].startswith("hidden:")   # NAME masked
        assert c["client_key"] == "client:eventsmx"       # JOIN HANDLE intact
        # Nested project labels masked too; keys survive.
        assert masked["clients"][0]["projects"][0]["project_label"].startswith(
            "hidden:")


def test_norm_org_strips_underscore_and_case():
    assert _norm_org("_EventsMX") == "eventsmx"
    assert _norm_org("DDTYI") == "ddtyi"
    assert _norm_org(None) == ""
