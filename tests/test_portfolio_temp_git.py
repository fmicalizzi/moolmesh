"""#161 B and C: agent scratchpads on Linux and throwaway git repos.

* C — the Linux scratchpad ``/tmp/claude-<uid>/<encoded>/<uuid>/…`` (or the
  same shape under any other temp root, ``$TMPDIR``) collapses into the
  session's project exactly like macOS ``/private/tmp/claude-…``; the
  harness-tooling rule (``<tmp>/claude-<uid>/…`` without a session) works on
  Linux too.
* B — a remote-less git repo (``git init``) in a harness scratchpad collapses
  into the session's project; one under an OS temp root is ``temporary``.
  Neither is listed as a project.

Synthetic paths only (``acme/widget-services``); nothing here exists on disk
except the grouped-read fixtures.
"""

from __future__ import annotations

import sqlite3

import pytest

from hub.cache.portfolio_classifier import classify, is_harness_tool_path
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity, resolve_path

HOME = "/home/u"
PROJ = f"{HOME}/work/widget-services"
ENC = "-home-u-work-widget-services"
UUID = "005784b6-3bf5-423a-a467-6b7d1d86b7a1"
LINUX_PAD = f"/tmp/claude-1000/{ENC}/{UUID}/scratchpad"
MAC_PAD = f"/private/tmp/claude-501/{ENC}/{UUID}/scratchpad"
OTHER_PAD = f"/var/tmp/claude-1000/{ENC}/{UUID}/scratchpad"
CUSTOM_TMP = f"{HOME}/.cache/tmp"
CUSTOM_PAD = f"{CUSTOM_TMP}/claude-1000/{ENC}/{UUID}/scratchpad"
PROJ_KEY = "git_remote:github.com/acme/widget-services"


def _classify(kind, path, session_cwds=None, enc_index=None):
    git = kind in ("git_remote", "git_root")
    return classify(
        kind, "github.com/acme/x" if kind == "git_remote" else None,
        path if git else None, None if git else path,
        session_cwds=session_cwds or {}, enc_index=enc_index or {}, home=HOME,
        exists=lambda _p: False,
    )


@pytest.fixture
def project_cwd(monkeypatch):
    """The session's real cwd resolves to the acme repo (no disk access)."""
    from hub.cache import portfolio_classifier as pc

    def _anchor(realpath, resolve=None):
        if realpath == PROJ or realpath.startswith(PROJ + "/"):
            return pc.Anchor(PROJ_KEY, "github.com/acme/widget-services", PROJ)
        return None
    monkeypatch.setattr(pc, "anchor_of_realpath", _anchor)
    return {UUID: PROJ}


# ── C: the scratchpad shape on any temp root ─────────────────────────────

class TestLinuxScratchpad:
    @pytest.mark.parametrize("pad", [LINUX_PAD, MAC_PAD, OTHER_PAD, CUSTOM_PAD])
    def test_gitless_scratchpad_collapses_into_session_project(self, pad, project_cwd):
        c = _classify("path_hash", pad + "/x", session_cwds=project_cwd)
        assert (c.category, c.role, c.resolved_via) == ("A", "collapse", "session_cwd")
        assert c.project_key == PROJ_KEY

    def test_linux_scratchpad_without_session_still_collapses(self):
        # No cwd for the session: the encoded name keeps it on a synthetic
        # project (never "unclassified"), as on macOS.
        c = _classify("path_hash", LINUX_PAD + "/x")
        assert (c.category, c.role) == ("A", "collapse")
        assert c.project_key == f"encoded:{ENC}"

    def test_linux_harness_tooling_is_not_a_project(self):
        d = "/tmp/claude-1000/bundled-skills/2.1.220/abc/dataviz"
        c = _classify("path_hash", d)
        assert (c.subtype, c.role, c.resolved_via) == ("home_config", "orphan", "tool")

    def test_tooling_under_tmpdir(self, monkeypatch):
        monkeypatch.setenv("TMPDIR", CUSTOM_TMP)
        d = f"{CUSTOM_TMP}/claude-1000/bundled-skills/x"
        assert is_harness_tool_path(d)
        c = _classify("path_hash", d)
        assert (c.subtype, c.role) == ("home_config", "orphan")
        # Outside a claude-<uid> dir the custom temp root is an ordinary dir.
        assert not is_harness_tool_path(f"{CUSTOM_TMP}/notes")
        assert not is_harness_tool_path(f"{CUSTOM_TMP}/claude-")

    def test_macos_tooling_unchanged(self):
        d = "/private/tmp/claude-501/bundled-skills/2.1.220/abc/dataviz/references"
        c = _classify("path_hash", d)
        assert (c.subtype, c.role) == ("home_config", "orphan")

    def test_project_folders_named_like_the_harness_are_untouched(self):
        # A real project called "claude-tools" is not a scratchpad: no
        # encoded segment + session uuid below it, not under a temp root.
        c = _classify("path_hash", f"{HOME}/work/claude-tools/src")
        assert (c.category, c.role) == ("B", "nest")


# ── B: throwaway git repos ───────────────────────────────────────────────

class TestThrowawayGit:
    @pytest.mark.parametrize("pad", [LINUX_PAD, MAC_PAD, OTHER_PAD, CUSTOM_PAD])
    def test_git_init_in_scratchpad_collapses(self, pad, project_cwd):
        c = _classify("git_root", pad + "/case/c3-before", session_cwds=project_cwd)
        assert (c.category, c.role) == ("A", "collapse")
        assert c.project_key == PROJ_KEY

    @pytest.mark.parametrize("root", ["/tmp/x", "/private/tmp/x", "/var/tmp/x",
                                      "/private/var/folders/ab/T/x"])
    def test_git_init_under_temp_root_is_temporary(self, root):
        c = _classify("git_root", root)
        assert (c.subtype, c.role, c.project_key) == ("temporary", "orphan", None)

    def test_git_init_in_harness_tooling_is_not_a_project(self):
        c = _classify("git_root", "/tmp/claude-1000/some-tool/repo")
        assert (c.subtype, c.role) == ("home_config", "orphan")

    def test_git_remote_keeps_its_project_wherever_seen(self, project_cwd):
        # Keyed by the remote: the row's root_path is only its last checkout.
        c = _classify("git_remote", "/tmp/verify-wt", session_cwds=project_cwd)
        assert c.role == "project"
        c = _classify("git_remote", LINUX_PAD + "/clone", session_cwds=project_cwd)
        assert c.role == "project"

    def test_git_repo_outside_temp_still_a_project(self):
        c = _classify("git_root", f"{HOME}/work/widget-services")
        assert c.role == "project"


# ── Grouped read on a Linux fixture ──────────────────────────────────────

def _sessions_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sessions (id TEXT, provider TEXT, cwd TEXT)")
    c.executemany("INSERT INTO sessions VALUES (?, 'claude', ?)", rows)
    c.commit()
    c.close()
    return path


def test_grouped_portfolio_on_linux_lists_only_the_real_project(tmp_path):
    repo = tmp_path / "widget-services"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[core]\n\tbare = false\n[remote "origin"]\n'
        '\turl = git@github.com:acme/widget-services.git\n')
    pad = f"/tmp/claude-1000/{ENC}/{UUID}/scratchpad"
    events = _sessions_db(tmp_path / "events.db", [(UUID, str(repo))])
    store = WorkspaceStore(tmp_path / "workspace.db")
    try:
        store.record_attribution("s1", "claude", str(repo / "main.py"),
                                 resolve_path(str(repo / "main.py")))
        # A gitless scratchpad file, and a throwaway ``git init`` beside it.
        store.record_attribution("s1", "claude", pad + "/x/note.md",
                                 resolve_path(pad + "/x/note.md"))
        throwaway = f"{pad}/case/c3-before"
        store.record_attribution(
            "s1", "claude", throwaway + "/a.py",
            WorkspaceIdentity(key=f"git_root:{throwaway}", kind="git_root",
                              root_path=throwaway))
        tmp_repo = "/tmp/x"
        store.record_attribution(
            "s2", "claude", tmp_repo + "/a.py",
            WorkspaceIdentity(key=f"git_root:{tmp_repo}", kind="git_root",
                              root_path=tmp_repo))
        store.build_rollup()
        store.classify_workspaces(events)
        g = store.get_portfolio_grouped()
        keys = [p["project_key"] for p in g["projects"]]
        assert keys == [PROJ_KEY]
        proj = g["projects"][0]
        assert proj["collapsed_harness"] == 2   # the scratchpad and its repo
        # /tmp/x is the only row left out of the projects: shown as a
        # temporary dir (the #36 drawer), never as a project.
        assert [(o["workspace_key"], o["subtype"]) for o in g["unclassified"]] \
            == [("git_root:/tmp/x", "temporary")]
    finally:
        store.close()


def test_macos_paths_classify_as_before():
    # The pre-#161 macOS shapes keep their exact classification.
    c = _classify("path_hash", f"/private/tmp/claude-501/-some-encoded-name/{UUID}/scratchpad")
    assert (c.category, c.role, c.resolved_via) == ("A", "collapse", "unresolved")


# ── Harness of a session whose cwd is gone or throwaway (#161) ──────────

UUID2 = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"


def _resolver(table):
    """A ``resolve`` stand-in: path → identity, else path_hash."""
    def _resolve(path):
        for root, ident in table.items():
            if path == root or path.startswith(root + "/"):
                return ident
        return WorkspaceIdentity(key=f"path_hash:{path}", kind="path_hash",
                                 dir_path=path)
    return _resolve


def test_harness_of_a_session_in_a_removed_worktree_folds_into_its_repo():
    # The worktree is gone from disk; the store's identity memory (#149)
    # still knows it — the classifier uses it for the session's cwd.
    wt = f"{HOME}/work/widget-services-wt-12"
    resolve = _resolver({wt: WorkspaceIdentity(
        key=PROJ_KEY, kind="git_remote",
        remote_url="github.com/acme/widget-services", root_path=PROJ)})
    c = classify("path_hash", None, None, LINUX_PAD + "/x",
                 session_cwds={UUID: wt}, enc_index={}, home=HOME,
                 exists=lambda _p: False, resolve=resolve)
    assert (c.role, c.project_key) == ("collapse", PROJ_KEY)


def test_harness_of_a_session_run_inside_a_throwaway_repo():
    # Session 2 ran inside a repo session 1 created in its scratchpad: its
    # harness follows that repo's rule (into session 1's project), never
    # minting the throwaway repo as a project.
    repo = f"{LINUX_PAD}/case/c3-before"
    pad2 = f"/tmp/claude-1000/-tmp-claude-1000-x/{UUID2}/scratchpad"
    resolve = _resolver({
        repo: WorkspaceIdentity(key=f"git_root:{repo}", kind="git_root",
                                root_path=repo),
        PROJ: WorkspaceIdentity(key=PROJ_KEY, kind="git_remote",
                                remote_url="github.com/acme/widget-services",
                                root_path=PROJ),
    })
    c = classify("path_hash", None, None, pad2 + "/y",
                 session_cwds={UUID2: repo, UUID: PROJ}, enc_index={},
                 home=HOME, exists=lambda _p: False, resolve=resolve)
    assert (c.role, c.project_key) == ("collapse", PROJ_KEY)
    # Without session 1's cwd it still never lands on the throwaway repo: it
    # falls back to session 1's encoded name, as any unresolved harness does.
    c = classify("path_hash", None, None, pad2 + "/y",
                 session_cwds={UUID2: repo}, enc_index={}, home=HOME,
                 exists=lambda _p: False, resolve=resolve)
    assert (c.role, c.project_key) == ("collapse", f"encoded:{ENC}")
