"""Registered repos with no activity fold; GitHub remotes that aren't registered
show "no medido" (issue #36 decision 6b, #60).

The grouped payload annotates each project with ``has_remote`` / ``registered``
(booleans, no names). Inside a client, registered projects with zero ingested
activity move to a collapsed ``inactive`` subgroup (out of the client KPIs); a
GitHub remote that isn't registered is a THIRD delivery state, distinct from
"— sin repo" and from a measured "0 entregado".
"""

import sqlite3

from hub.cache.portfolio_clients import group_by_client
from hub.cache.workspace_store import WorkspaceStore
from hub.mcp_server import (
    _annotate_github_measured_projects,
    _get_portfolio_grouped,
    _registered_remote_keys,
)
from hub.correlation.workspace_resolver import WorkspaceIdentity

KEY = "git_remote:github.com/acme/empty"


def _proj(key, *, remote=None, sess=0, fs=0, git=0, registered=False,
          state=None, children=None):
    return {
        "project_key": key, "project_label": key,
        "_remote_url": remote, "_anchor_path": None,
        "session_touches": sess, "fs_touches": fs, "git_touches": git,
        "active_days": 0, "last_activity": None, "_day_set": [],
        "collapsed_harness": 0, "children": children or [],
        "registered": registered, **({"state": state} if state else {}),
    }


class TestGroupByClientFold:
    def test_registered_inactive_member_folds_out(self):
        out = group_by_client(
            [
                _proj("git_remote:github.com/acme/empty", registered=True),
                _proj("git_remote:github.com/acme/live", registered=True,
                      sess=3, state={"state": "activo"}),
            ],
            client_orgs={"acme"}, personal_orgs=set(), overrides={})
        client = out["clients"][0]
        assert [p["project_key"] for p in client["projects"]] == [
            "git_remote:github.com/acme/live"]
        assert [p["project_key"] for p in client["inactive"]] == [KEY]
        # The client state/effort only read the active member.
        assert client["session_touches"] == 3
        assert client["state"]["state"] == "activo"

    def test_all_inactive_client_has_no_state(self):
        out = group_by_client(
            [_proj(KEY, registered=True)],
            client_orgs={"acme"}, personal_orgs=set(), overrides={})
        client = out["clients"][0]
        assert client["projects"] == []
        assert len(client["inactive"]) == 1
        assert "state" not in client

    def test_unregistered_inactive_project_is_not_folded(self):
        """Folding is for REGISTERED repos only — an unregistered gitless empty
        project stays a row (it is not 'registered sin actividad')."""
        out = group_by_client(
            [_proj("git_remote:github.com/acme/x", registered=False)],
            client_orgs={"acme"}, personal_orgs=set(), overrides={})
        client = out["clients"][0]
        assert len(client["projects"]) == 1
        assert "inactive" not in client


class TestRegisteredRemoteKeys:
    def test_reads_owner_and_repo_lowercased(self, tmp_path):
        gh = tmp_path / "github.db"
        c = sqlite3.connect(gh)
        c.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT,"
                  " owner TEXT, repo_name TEXT)")
        c.execute("INSERT INTO repos VALUES (1, '/x/R', 'Acme', 'Empty')")
        c.commit()
        c.close()
        assert _registered_remote_keys(str(gh)) == {"github.com/acme/empty"}

    def test_missing_db_is_empty(self, tmp_path):
        assert _registered_remote_keys(str(tmp_path / "nope.db")) == set()


class TestAnnotate:
    def test_marks_remote_and_registered(self):
        projects = [
            _proj("git_remote:github.com/acme/empty"),
            _proj("git_remote:github.com/acme/nope"),
            _proj("git_remote:gitlab.com/acme/x"),
            _proj("git_root:/x/y"),
        ]
        _annotate_github_measured_projects(projects, {"github.com/acme/empty"})
        assert projects[0]["has_remote"] is True and projects[0]["registered"] is True
        assert projects[1]["has_remote"] is True and projects[1]["registered"] is False
        assert projects[2]["has_remote"] is False and projects[2]["registered"] is False
        assert projects[3]["has_remote"] is False and projects[3]["registered"] is False


def _make_github_db(path, repos):
    """repos: (id, path, owner, repo_name, n_merged_prs)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT,"
              " owner TEXT, repo_name TEXT)")
    c.execute("""CREATE TABLE github_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        number INTEGER, title TEXT, state TEXT, author TEXT, closed_at TEXT,
        is_pull_request INTEGER DEFAULT 0, pr_merged_at TEXT)""")
    for rid, rpath, owner, name, n in repos:
        c.execute("INSERT INTO repos VALUES (?,?,?,?)", (rid, rpath, owner, name))
        for i in range(n):
            c.execute("INSERT INTO github_issues (repo_id, number, title, state,"
                      " is_pull_request, pr_merged_at) VALUES (?,?,?,?,1,?)",
                      (rid, i, "t", "closed", "2026-01-01T00:00:00"))
    c.commit()
    c.close()
    return path


def _classify(s, wid, pk):
    with s._lock:
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_classification
               (workspace_id, category, subtype, role, project_key,
                project_label, resolved_via, classified_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, "A", "project", "project", pk, pk.split("/")[-1], "self",
             "2026-01-01T00:00:00"),
        )
        s._conn.commit()


def _rollup(s, wid, day, session):
    with s._lock:
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_rollup
               (workspace_id, day, session_touches, fs_touches, git_touches,
                last_activity, built_at)
               VALUES (?,?,?,0,0,?,?)""",
            (wid, day, session, f"{day}T10:00:00+00:00", "2026-01-01T00:00:00"),
        )
        s._conn.commit()


class TestGroupedIntegration:
    def test_registered_empty_folds_and_unregistered_remote_is_no_medido(
        self, tmp_path, monkeypatch
    ):
        import hub.config as cfgmod
        from hub.config import HubConfig

        monkeypatch.setattr(cfgmod, "load_config", lambda: HubConfig(
            client_orgs=["acme"], personal_orgs=["me"]))

        wdb = tmp_path / "workspace.db"
        s = WorkspaceStore(wdb)
        try:
            empty = s.upsert_workspace(WorkspaceIdentity(
                key=KEY, kind="git_remote", remote_url="github.com/acme/empty",
                root_path="/x/empty"))
            live = s.upsert_workspace(WorkspaceIdentity(
                key="git_remote:github.com/acme/live", kind="git_remote",
                remote_url="github.com/acme/live", root_path="/x/live"))
            unreg = s.upsert_workspace(WorkspaceIdentity(
                key="git_remote:github.com/acme/unregistered", kind="git_remote",
                remote_url="github.com/acme/unregistered", root_path="/x/unreg"))
            _classify(s, empty, KEY)
            _classify(s, live, "git_remote:github.com/acme/live")
            _classify(s, unreg, "git_remote:github.com/acme/unregistered")
            _rollup(s, live, "2026-09-20", 2)
            _rollup(s, unreg, "2026-09-21", 1)
        finally:
            s.close()

        gh = _make_github_db(tmp_path / "github.db", [
            (1, "/x/empty", "acme", "empty", 0),
            (2, "/x/live", "acme", "live", 3),
        ])

        grouped = _get_portfolio_grouped(
            str(wdb), events_db=str(tmp_path / "absent.db"), github_db=str(gh))
        clients = grouped["clients"]
        assert len(clients) == 1
        c = clients[0]
        # The registered, never-touched repo folded; the two live ones stayed.
        assert sorted(p["project_key"] for p in c["projects"]) == [
            "git_remote:github.com/acme/live",
            "git_remote:github.com/acme/unregistered",
        ]
        assert [p["project_key"] for p in c["inactive"]] == [KEY]
        # A GitHub remote that isn't registered shows as remote+unregistered.
        unreg = next(p for p in c["projects"]
                     if p["project_key"].endswith("unregistered"))
        assert unreg["has_remote"] is True
        assert unreg["registered"] is False
        live = next(p for p in c["projects"] if p["project_key"].endswith("live"))
        assert live["registered"] is True
        # No leaking internal keys after strip_internal.
        assert "_remote_url" not in live and "_day_set" not in live
