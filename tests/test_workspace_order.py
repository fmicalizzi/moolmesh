"""Order by config — aliases, reference/temporary containers, rung 4d, R4.

Issue #36 decisiones 3/4/5 + #64 parte 2. All keys live in ``[workspace]`` with
an empty default and apply in the READ layer (classification and grouping),
never in the resolver. An alias to a missing destination only warns.
"""

import pytest

from hub.cache.portfolio_clients import _majority_org, resolve_client
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity, resolve_dir
from hub.mcp_server import _get_portfolio_grouped
from hub.config import HubConfig, load_config, save_config
from tests.test_portfolio_classifier import CLAUDE, HOME, _classify as _cls
from tests.test_workspace_store import _mkrepo

OLD = f"{CLAUDE}/old-project"
NEW = f"{CLAUDE}/new-project"


def _ws(store, key, *, kind="path_hash", dir_path=None, root_path=None,
        remote=None):
    return store.upsert_workspace(WorkspaceIdentity(
        key=key, kind=kind, remote_url=remote, root_path=root_path,
        dir_path=dir_path))


def _pk(store, key):
    with store._lock:
        row = store._conn.execute(
            """SELECT c.project_key, c.role, c.subtype
               FROM workspace_classification c JOIN workspaces w
                 ON w.id = c.workspace_id WHERE w.workspace_key = ?""",
            (key,),
        ).fetchone()
    return row


class TestProjectAliases:
    def test_moved_folder_folds_history_into_canonical(self, tmp_path):
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:old", dir_path=OLD)
            _ws(s, "path_hash:old-child", dir_path=f"{OLD}/docs")
            _ws(s, "path_hash:new", dir_path=NEW)
            with s._lock:
                for wkey, day, n in (("path_hash:old", "2026-09-01", 2),
                                     ("path_hash:old-child", "2026-09-01", 1),
                                     ("path_hash:new", "2026-09-02", 1)):
                    wid = s._conn.execute(
                        "SELECT id FROM workspaces WHERE workspace_key=?",
                        (wkey,)).fetchone()[0]
                    s._conn.execute(
                        """INSERT INTO workspace_rollup (workspace_id, day,
                           session_touches, fs_touches, git_touches,
                           last_activity, built_at) VALUES (?,?,?,0,0,?,?)""",
                        (wid, day, n, f"{day}T10:00:00+00:00", "x"))
                s._conn.commit()
            res = s.classify_workspaces(
                str(tmp_path / "absent.db"), project_aliases={OLD: NEW})
            assert res["alias_warnings"] == []
            assert res["projects"] == 1
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        assert len(g["projects"]) == 1
        p = g["projects"][0]
        assert p["project_label"] == "new-project"
        assert p["session_touches"] == 4           # origin + child folded in
        assert p["active_days"] == 2               # 09-01 and 09-02, unioned
        # The origin row no longer exists as its own project (single group).
        assert len(g["projects"]) == 1

    def test_alias_to_key_destination_folds_container_path(self, tmp_path):
        repo = _mkrepo(tmp_path, "proj", "git@github.com:acme/proj.git")
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            key = resolve_dir(str(repo)).key
            _ws(s, key, kind="git_remote", root_path=str(repo),
                remote="github.com/acme/proj")
            _ws(s, "path_hash:internal", dir_path="/app")
            with s._lock:
                wid = s._conn.execute(
                    "SELECT id FROM workspaces WHERE workspace_key='path_hash:internal'"
                ).fetchone()[0]
                s._conn.execute(
                    """INSERT INTO workspace_rollup (workspace_id, day,
                       session_touches, fs_touches, git_touches,
                       last_activity, built_at) VALUES (?,?,5,0,0,?,?)""",
                    (wid, "2026-09-20", "2026-09-20T10:00:00+00:00", "x"))
                s._conn.commit()
            res = s.classify_workspaces(
                str(tmp_path / "absent.db"),
                project_aliases={"/app": key})
            assert res["alias_warnings"] == []
            assert _pk(s, "path_hash:internal")[0] == key
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        # /app is orphan-by-default (container rule); the alias promotes it to a
        # project row so its history folds onto the canonical repo group.
        p = next(p for p in g["projects"] if p["project_key"] == key)
        assert p["session_touches"] == 5
        assert g["unclassified"] == []

    def test_alias_to_missing_destination_warns_and_changes_nothing(
        self, tmp_path
    ):
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:old", dir_path=OLD)
            res = s.classify_workspaces(
                str(tmp_path / "absent.db"),
                project_aliases={OLD: f"{CLAUDE}/does-not-exist"})
            assert any("destino inexistente" in w for w in res["alias_warnings"])
            assert _pk(s, "path_hash:old")[0] == f"path_hash:{_hash(OLD)}"
        finally:
            s.close()

    def test_alias_origin_without_matches_warns_only(self, tmp_path):
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:new", dir_path=NEW)
            res = s.classify_workspaces(
                str(tmp_path / "absent.db"),
                project_aliases={f"{CLAUDE}/ghost": NEW})
            assert any("origen sin coincidencias" in w
                       for w in res["alias_warnings"])
        finally:
            s.close()

    def test_rows_sharing_the_origin_project_key_follow_the_alias(self):
        """Harness/collapse rows carry the origin's project_key but a different
        path — they must move as part of the fold."""
        from hub.cache.portfolio_classifier import Classification
        from hub.cache.workspace_store import _rewrite_project_aliases
        origin_pk = f"path_hash:{_hash(OLD)}"
        new_pk = f"path_hash:{_hash(NEW)}"
        classified = [
            (1, Classification("root", "project", "project", origin_pk,
                               "old", "self")),
            (2, Classification("A", "harness", "collapse", origin_pk,
                               "old", "session_cwd")),
            (3, Classification("root", "project", "project", new_pk,
                               "new", "self")),
        ]
        ws_rows = [
            (1, "path_hash", None, None, OLD, "path_hash:old"),
            (2, "path_hash", None, None, f"{CLAUDE}/scratch", "path_hash:h"),
            (3, "path_hash", None, None, NEW, "path_hash:new"),
        ]
        out, warnings = _rewrite_project_aliases(classified, ws_rows, {OLD: NEW})
        assert warnings == []
        keys = {wid: c.project_key for wid, c in out}
        assert keys == {1: new_pk, 2: new_pk, 3: new_pk}


class TestReferenceContainers:
    def test_container_and_children_leave_the_project_tree(self, tmp_path):
        ref = f"{CLAUDE}/analysis/reference-clones"
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:ref", dir_path=ref)
            _ws(s, "git_remote:github.com/third/dep", kind="git_remote",
                root_path=f"{ref}/dep", remote="github.com/third/dep")
            res = s.classify_workspaces(
                str(tmp_path / "absent.db"), reference_containers=[ref])
            assert _pk(s, "path_hash:ref")[2] == "reference"
            assert _pk(s, "git_remote:github.com/third/dep")[2] == "reference"
            assert _pk(s, "path_hash:ref")[0] is None
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        assert g["projects"] == []
        assert len(g["reference"]) == 2
        assert g["summary"]["reference"] == 2

    def test_parent_no_longer_active_from_reference_child(self, tmp_path):
        parent = f"{CLAUDE}/analysis"
        child = f"{parent}/reference-clones"
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:parent", dir_path=parent)
            _ws(s, "path_hash:child", dir_path=child)
            with s._lock:
                wid = s._conn.execute(
                    "SELECT id FROM workspaces WHERE workspace_key='path_hash:child'"
                ).fetchone()[0]
                s._conn.execute(
                    """INSERT INTO workspace_rollup (workspace_id, day,
                       session_touches, fs_touches, git_touches,
                       last_activity, built_at) VALUES (?,?,3,0,0,?,?)""",
                    (wid, "2026-09-20", "2026-09-20T10:00:00+00:00", "x"))
                s._conn.commit()
            s.classify_workspaces(str(tmp_path / "absent.db"))
            g_before = s.get_portfolio_grouped()
            s.classify_workspaces(
                str(tmp_path / "absent.db"),
                reference_containers=[child])
            g_after = s.get_portfolio_grouped()
        finally:
            s.close()
        # Before: the child folds into the parent (nest under the anchor).
        p_before = next(p for p in g_before["projects"]
                        if p["project_key"].endswith(_hash(parent)))
        assert p_before["session_touches"] == 3
        # After: the child is a reference row; the parent has no activity.
        p_after = next(p for p in g_after["projects"]
                       if p["project_key"].endswith(_hash(parent)))
        assert p_after["session_touches"] == 0
        assert g_after["reference"][0]["session_touches"] == 3

    def test_works_when_folder_no_longer_exists(self, tmp_path):
        """Purely textual matching — the folder is gone from disk."""
        gone = f"{CLAUDE}/Temporal/reference-clones"
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:gone", dir_path=gone)
            s.classify_workspaces(
                str(tmp_path / "absent.db"), reference_containers=[gone])
            assert _pk(s, "path_hash:gone")[2] == "reference"
        finally:
            s.close()


class TestTemporaryContainers:
    def test_direct_children_move_to_temporal_keeping_state(self, tmp_path):
        temp = f"{CLAUDE}/Temporal"
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:job", dir_path=f"{temp}/widget-2027")
            _ws(s, "path_hash:job-child", dir_path=f"{temp}/widget-2027/sub")
            _ws(s, "path_hash:other", dir_path=f"{CLAUDE}/real-project")
            with s._lock:
                wid = s._conn.execute(
                    "SELECT id FROM workspaces WHERE workspace_key='path_hash:job'"
                ).fetchone()[0]
                s._conn.execute(
                    """INSERT INTO workspace_rollup (workspace_id, day,
                       session_touches, fs_touches, git_touches,
                       last_activity, built_at) VALUES (?,?,4,0,0,?,?)""",
                    (wid, "2026-09-20", "2026-09-20T10:00:00+00:00", "x"))
                s._conn.commit()
            s.classify_workspaces(str(tmp_path / "absent.db"))
            g = s.get_portfolio_grouped(temporary_containers=[temp])
        finally:
            s.close()
        assert [p["project_label"] for p in g["temporary"]] == ["widget-2027"]
        assert g["temporary"][0]["session_touches"] == 4   # history kept
        assert [p["project_label"] for p in g["projects"]] == ["real-project"]
        # A deeper grandchild is not a DIRECT child: it still nests under its
        # project (shown inside the Temporal row).
        assert g["temporary"][0]["children"][0]["dir_path"].endswith("/sub")
        assert g["summary"]["temporary"] == 1

    def test_without_container_config_nothing_moves(self, tmp_path):
        temp = f"{CLAUDE}/Temporal"
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:job", dir_path=f"{temp}/job")
            s.classify_workspaces(str(tmp_path / "absent.db"))
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        assert g["temporary"] == []
        assert [p["project_label"] for p in g["projects"]] == ["job"]


class TestRung4dMajority:
    def _ref(self, child_orgs):
        return resolve_client(
            "path_hash:widget", None, f"{CLAUDE}/widget-services",
            client_orgs={"acme", "globex"}, personal_orgs={"ownerhandle"},
            overrides={}, child_orgs=child_orgs)

    def test_strict_majority_attributes_to_client(self):
        ref = self._ref(["acme", "acme", "third"])
        assert ref["bucket"] == "client"
        assert ref["client_key"] == "client:acme"

    def test_tie_changes_nothing(self):
        ref = self._ref(["acme", "globex"])
        assert ref["bucket"] == "shared"

    def test_unknown_majority_changes_nothing(self):
        ref = self._ref(["third", "third", "third"])
        assert ref["bucket"] == "shared"

    def test_single_repo_is_not_a_majority(self):
        assert _majority_org(["acme"]) is None
        ref = self._ref(["acme"])
        assert ref["bucket"] == "shared"

    def test_personal_majority_stays_loose(self):
        ref = self._ref(["ownerhandle", "ownerhandle"])
        assert ref["bucket"] == "personal"

    def test_override_beats_child_orgs(self):
        ref = resolve_client(
            "path_hash:widget", None, f"{CLAUDE}/widget-services",
            client_orgs={"acme"}, personal_orgs=set(),
            overrides={"path_hash:widget": "acme"}, child_orgs=["acme"])
        assert ref["client_key"] == "client:acme"

    def test_folder_name_still_wins_over_children(self):
        # The folder itself names a client → rung 3, children never consulted.
        ref = resolve_client(
            "path_hash:x", None, f"{CLAUDE}/_acme",
            client_orgs={"acme", "globex"}, personal_orgs=set(),
            overrides={}, child_orgs=["globex", "globex"])
        assert ref["client_key"] == "client:acme"


class TestR4SystemRoots:
    @pytest.mark.parametrize("d", [
        "/usr/libexec",
        "/usr/local/share",
        "/var/www/html",
        "/private/var/log/something",
        f"{HOME}/Library/LaunchAgents",
        f"{HOME}/Library/Application Support/Open Design/x",
    ])
    def test_system_roots_are_temporary_orphans(self, d):
        c = _cls("path_hash", dir_path=d)
        assert (c.role, c.subtype) == ("orphan", "temporary"), d

    def test_proyectos_is_a_container_like_projects(self):
        c = _cls("path_hash", dir_path=f"{HOME}/Downloads/Proyectos/LACNOG")
        assert c.role == "project"
        assert c.project_label == "LACNOG"

    def test_home_folders_named_like_roots_stay_projects(self):
        c = _cls("path_hash", dir_path=f"{CLAUDE}/usr-tools")
        assert c.role == "project"
        c = _cls("path_hash", dir_path=f"{CLAUDE}/myproj/var/log")
        assert c.role == "nest"

    def test_system_library_not_home_library(self):
        c = _cls("path_hash", dir_path="/Library/LaunchAgents")
        assert c.role == "project"


class TestPathMissing:
    def test_missing_folder_is_marked(self, tmp_path):
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:gone", dir_path=f"{CLAUDE}/deleted-folder")
            s.classify_workspaces(str(tmp_path / "absent.db"))
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        assert g["projects"][0]["path_missing"] is True

    def test_git_remote_never_marked(self, tmp_path):
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "git_remote:github.com/acme/x", kind="git_remote",
                root_path="/gone/elsewhere", remote="github.com/acme/x")
            s.classify_workspaces(str(tmp_path / "absent.db"))
            g = s.get_portfolio_grouped()
        finally:
            s.close()
        assert g["projects"][0]["path_missing"] is False


class TestMaskingOrderSurfaces:
    def test_alias_reference_temporal_masked(self, tmp_path, monkeypatch):
        import hub.config as cfgmod
        monkeypatch.setattr(cfgmod, "load_config", lambda: HubConfig(
            personal_orgs=["me"], client_orgs=["acme"],
            project_aliases={OLD: NEW},
            reference_containers=[f"{CLAUDE}/reference-clones"],
            temporary_containers=[f"{CLAUDE}/Temporal"],
            hide_project_names=True))
        s = WorkspaceStore(tmp_path / "w.db")
        try:
            _ws(s, "path_hash:old", dir_path=OLD)
            _ws(s, "path_hash:ref", dir_path=f"{CLAUDE}/reference-clones")
            _ws(s, "path_hash:job", dir_path=f"{CLAUDE}/Temporal/job")
            s.classify_workspaces(
                str(tmp_path / "absent.db"),
                project_aliases={OLD: NEW},
                reference_containers=[f"{CLAUDE}/reference-clones"])
        finally:
            s.close()
        g = _get_portfolio_grouped(
            str(tmp_path / "w.db"),
            events_db=str(tmp_path / "absent.db"),
            github_db=str(tmp_path / "absent-gh.db"))
        blob = repr(g)
        assert "new-project" not in blob
        assert "reference-clones" not in blob
        assert "Temporal" not in blob
        # Join handles survive (they are not display names).
        assert g["reference"][0]["workspace_key"]


class TestOrderConfigRoundtrip:
    @pytest.fixture
    def temp_config_path(self, tmp_path, monkeypatch):
        import hub.config as config_module
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir(parents=True, exist_ok=True)
        original_path = config_module.CONFIG_PATH
        original_dir = config_module.CONFIG_DIR
        config_module.CONFIG_PATH = config_dir / "config.toml"
        config_module.CONFIG_DIR = config_dir
        yield config_module.CONFIG_PATH
        config_module.CONFIG_PATH = original_path
        config_module.CONFIG_DIR = original_dir

    def test_roundtrip(self, temp_config_path):
        save_config(HubConfig(
            project_aliases={OLD: NEW, "git_remote:github.com/a/b": "git_root:/x"},
            reference_containers=[f"{CLAUDE}/reference-clones"],
            temporary_containers=[f"{CLAUDE}/Temporal"],
        ))
        loaded = load_config()
        assert loaded.project_aliases == {
            OLD: NEW, "git_remote:github.com/a/b": "git_root:/x"}
        assert loaded.reference_containers == [f"{CLAUDE}/reference-clones"]
        assert loaded.temporary_containers == [f"{CLAUDE}/Temporal"]

    def test_absent_are_empty(self, temp_config_path):
        temp_config_path.write_text("[workspace]\nhide_project_names = false\n")
        loaded = load_config()
        assert loaded.project_aliases == {}
        assert loaded.reference_containers == []
        assert loaded.temporary_containers == []

    def test_windows_separators_normalized(self):
        """A config written with Windows separators still matches stored paths
        (the invariant: config paths normalize cross-platform)."""
        from hub.cache.portfolio_classifier import config_path, is_under
        assert config_path("C:\\Users\\x\\work") == "C:/Users/x/work"
        assert is_under("/Users/tester/work/old", "\\Users\\tester\\work")


def _hash(directory):
    import hashlib
    return hashlib.sha256(directory.encode("utf-8", "surrogatepass")).hexdigest()[:16]
