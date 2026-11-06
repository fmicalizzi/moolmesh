"""A removed git worktree keeps counting toward its repo (#149).

Synthetic fixtures only: the repo ``acme/widget-services``, its linked
worktree ``widget-services-wt-12`` (laid out on disk the way ``git worktree
add`` does, no git binary needed) and ``acme-wt-99``, an unrelated folder with
no git at all whose name merely looks like a worktree. HOME is a tmp dir.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3

import pytest

import hub.cache.workspace_store as ws
from hub.cache.event_store import EventStore
from hub.cache.workspace_store import WorkspaceStore, parse_worktree_adds
from hub.correlation.workspace_resolver import resolve_dir, worktree_main_root
from hub.mcp_server import _portfolio_production
from tests.test_edge_active_days import UTC, _insert
from tests.test_workspace_store import _mkrepo

D = "2026-09-18"
REPO_KEY = "git_remote:github.com/acme/widget-services"


def _mkworktree(repo, wt, name="widget-services-wt-12"):
    """Lay out a linked worktree exactly as ``git worktree add`` does."""
    admin = repo / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{wt / '.git'}\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "HEAD").write_text("ref: refs/heads/fix/wt-12\n")
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {admin}\n")
    return admin


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ws, "user_timezone", lambda: UTC)
    dev = home / "dev"
    repo = _mkrepo(dev, "widget-services", "git@github.com:acme/widget-services.git")
    wt = dev / "widget-services-wt-12"
    admin = _mkworktree(repo, wt)
    other = dev / "acme-wt-99"
    other.mkdir()
    events = tmp_path / "events.db"
    EventStore(db_path=events).close()
    return tmp_path, events, repo, wt, admin, other


def _store(tmp_path):
    return WorkspaceStore(tmp_path / "workspace.db")


def _incremental(tmp_path, events):
    s = _store(tmp_path)
    try:
        return s.attribute_incremental(events)
    finally:
        s.close()


def _full(tmp_path, events):
    s = _store(tmp_path)
    try:
        return s.backfill_from_events(events)
    finally:
        s.close()


def _keys_of(tmp_path, session):
    c = sqlite3.connect(tmp_path / "workspace.db")
    try:
        return {k for (k,) in c.execute(
            """SELECT w.workspace_key FROM path_attributions a
               JOIN workspaces w ON w.id = a.workspace_id
               WHERE a.session_id = ?""", (session,))}
    finally:
        c.close()


def _q(tmp_path, sql, params=()):
    c = sqlite3.connect(tmp_path / "workspace.db")
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _meta(tmp_path):
    return dict(_q(tmp_path, "SELECT key, value FROM rollup_meta"))


def _backups(tmp_path):
    d = tmp_path / "backups"
    return sorted(d.glob("workspace-*.db")) if d.exists() else []


def _make_pre_149(tmp_path, events):
    """workspace.db as v1.36.x left it after the worktree was removed: no
    registry, no migration 8, and a pass that re-resolved from disk."""
    c = sqlite3.connect(tmp_path / "workspace.db")
    c.execute("DELETE FROM known_git_roots")
    c.execute("DELETE FROM schema_migrations WHERE version = 8")
    c.commit()
    c.close()
    _full(tmp_path, events)          # empty registry + gone dir → path_hash
    c = sqlite3.connect(tmp_path / "workspace.db")
    c.execute("DELETE FROM known_git_roots")   # what that pass just learned
    c.commit()
    c.close()


class TestResolver:
    def test_live_worktree_resolves_to_repo_and_names_its_checkout(self, env):
        _tmp, _events, repo, wt, _admin, _other = env
        assert resolve_dir(str(wt)).key == REPO_KEY
        assert worktree_main_root(str(wt)) == str(repo)
        assert worktree_main_root(str(repo)) is None

    def test_removed_worktree_alone_falls_to_path_hash(self, env):
        _tmp, _events, _repo, wt, _admin, _other = env
        shutil.rmtree(wt)
        assert resolve_dir(str(wt)).kind == "path_hash"   # why #149 exists


class TestKeepKnownIdentity:
    def test_live_worktree_is_recorded_with_its_main_checkout(self, env):
        tmp_path, events, repo, wt, _admin, _other = env
        _insert(events, "s1", str(wt / "a.py"), f"{D}T10:00:00Z")
        _incremental(tmp_path, events)
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        rows = _q(tmp_path, "SELECT root_path, workspace_key, main_root, source"
                            " FROM known_git_roots WHERE root_path = ?", (str(wt),))
        assert rows == [(str(wt), REPO_KEY, str(repo), "resolved")]

    def test_removed_worktree_stays_on_repo_after_pass_and_rederive(self, env):
        tmp_path, events, _repo, wt, _admin, _other = env
        _insert(events, "s1", str(wt / "a.py"), f"{D}T10:00:00Z")
        _incremental(tmp_path, events)
        shutil.rmtree(wt)

        # A later incremental pass over new events in the vanished tree.
        _insert(events, "s1", str(wt / "pkg" / "b.py"), f"{D}T11:00:00Z")
        _insert(events, "s2", None, f"{D}T12:00:00Z", cwd=str(wt))
        _incremental(tmp_path, events)
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert _keys_of(tmp_path, "s2") == {REPO_KEY}     # cwd fallback too

        # A full re-derivation re-resolves every directory from disk.
        _full(tmp_path, events)
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("INSERT OR REPLACE INTO rollup_meta VALUES"
                  " ('active_days_rederive', 'pending')")
        c.commit()
        c.close()
        res = _incremental(tmp_path, events)
        assert res["active_days_edges"] >= 1
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert _keys_of(tmp_path, "s2") == {REPO_KEY}
        assert _q(tmp_path, "SELECT COUNT(*) FROM workspaces"
                            " WHERE kind = 'path_hash'") == [(0,)]

    def test_deleted_folder_that_was_never_a_repo_stays_path_hash(self, env):
        tmp_path, events, _repo, wt, _admin, other = env
        _insert(events, "s1", str(wt / "a.py"), f"{D}T10:00:00Z")
        _insert(events, "s9", str(other / "notes.md"), f"{D}T10:00:00Z")
        _incremental(tmp_path, events)
        before = _keys_of(tmp_path, "s9")
        assert len(before) == 1 and next(iter(before)).startswith("path_hash:")
        shutil.rmtree(other)
        shutil.rmtree(wt)
        _full(tmp_path, events)
        assert _keys_of(tmp_path, "s9") == before
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}

    def test_known_root_that_still_exists_answers_from_disk(self, env):
        """A recorded worktree path that is now a plain folder is a plain folder."""
        tmp_path, events, _repo, wt, _admin, _other = env
        _insert(events, "s1", str(wt / "a.py"), f"{D}T10:00:00Z")
        _incremental(tmp_path, events)
        shutil.rmtree(wt)
        wt.mkdir()                                   # same name, no git
        _full(tmp_path, events)
        assert next(iter(_keys_of(tmp_path, "s1"))).startswith("path_hash:")

    def test_session_in_checkout_and_worktree_counts_once(self, env):
        tmp_path, events, repo, wt, _admin, _other = env
        _insert(events, "both", str(repo / "a.py"), f"{D}T09:00:00Z")
        _insert(events, "both", str(wt / "b.py"), f"{D}T10:00:00Z")
        _insert(events, "solo", str(repo / "c.py"), f"{D}T11:00:00Z")
        _incremental(tmp_path, events)
        shutil.rmtree(wt)
        _full(tmp_path, events)
        s = _store(tmp_path)
        try:
            s.build_rollup(tmp_path / "github.db")
            s.classify_workspaces(events)
        finally:
            s.close()
        data = _portfolio_production(str(events), str(tmp_path / "workspace.db"),
                                     days=30, today=D, hide=False, tz=UTC)
        rows = [p for p in data["projects"] if p["project_key"] == REPO_KEY]
        assert len(rows) == 1
        assert rows[0]["sessions"] == 2          # "both" once, plus "solo"
        assert not [p for p in data["projects"]
                    if str(p["project_key"]).startswith("path_hash:")]


class TestRecoverExistingRows:
    def _seed(self, env, admin_entry=True, add_command=False):
        tmp_path, events, repo, wt, admin, other = env
        _insert(events, "s0", str(repo / "README.md"), f"{D}T09:00:00Z",
                cwd=str(repo))                        # work in the checkout
        _insert(events, "s1", str(wt / "a.py"), f"{D}T10:00:00Z", cwd=str(wt))
        _insert(events, "s9", str(other / "notes.md"), f"{D}T10:00:00Z")
        if add_command:
            c = sqlite3.connect(events)
            c.execute(
                "INSERT INTO events (provider, project, event_type, timestamp,"
                " summary, session_id, tool_name, file_path, cwd, fingerprint,"
                " created_at, historical) VALUES ('claude', 'p', 'tool_use', ?,"
                " ?, 'orch', 'Bash', NULL, ?, 'fp-add', 1800000000, 1)",
                (f"{D}T08:00:00Z",
                 "Bash: git worktree add -q -b fix/wt-12 ../widget-services-wt-12",
                 str(repo)))
            c.commit()
            c.close()
        _incremental(tmp_path, events)
        shutil.rmtree(wt)
        shutil.rmtree(other)
        if not admin_entry:
            shutil.rmtree(admin)           # ``git worktree remove`` cleans it
        _make_pre_149(tmp_path, events)
        assert next(iter(_keys_of(tmp_path, "s1"))).startswith("path_hash:")
        return tmp_path, events, wt

    def _upgrade(self, tmp_path, events, caplog):
        s = _store(tmp_path)                          # migration 8
        try:
            meta = _meta(tmp_path)
            assert meta["worktree_recover"] == "pending"
            assert meta["active_days_rederive"] == "pending"
            with caplog.at_level(logging.INFO, logger="hub"):
                return s.attribute_incremental(events)
        finally:
            s.close()

    def test_git_worktrees_entry_folds_back_with_a_backup(self, env, caplog):
        tmp_path, events, wt = self._seed(env, admin_entry=True)
        backups = len(_backups(tmp_path))
        res = self._upgrade(tmp_path, events, caplog)
        assert len(_backups(tmp_path)) == backups + 1
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert next(iter(_keys_of(tmp_path, "s9"))).startswith("path_hash:")
        assert res["worktree_evidence"]["git_worktrees"] == 1
        assert res["worktree_evidence"]["candidates"] == 2
        assert res["worktree_edges_recovered"] == 1
        assert _q(tmp_path, "SELECT source FROM known_git_roots WHERE root_path = ?",
                  (str(wt),)) == [("git_worktrees",)]
        msgs = [r.getMessage() for r in caplog.records]
        assert any("evidencia para 1" in m for m in msgs)
        assert any("1 aristas volvieron a su repo" in m
                   and "2 antes, 1 después" in m for m in msgs)
        meta = _meta(tmp_path)
        assert "worktree_recover" not in meta and "worktree_recover_before" not in meta

    def test_recorded_worktree_add_folds_back(self, env, caplog):
        tmp_path, events, wt = self._seed(env, admin_entry=False, add_command=True)
        res = self._upgrade(tmp_path, events, caplog)
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert res["worktree_evidence"]["worktree_add"] == 1
        assert _q(tmp_path, "SELECT source FROM known_git_roots WHERE root_path = ?",
                  (str(wt),)) == [("worktree_add",)]

    def test_edge_already_on_the_repo_is_not_downgraded(self, env, caplog):
        """Attributed while the worktree lived, removed before the upgrade:
        the upgrade's own full pass must not re-resolve it to path_hash."""
        tmp_path, events, repo, wt, admin, _other = env
        _insert(events, "s1", str(wt / "pkg" / "a.py"), f"{D}T10:00:00Z")
        s = _store(tmp_path)
        try:
            s.attribute_incremental(events)
            s._conn.execute("DELETE FROM known_git_roots")   # as v1.36.x
            s._conn.execute("DELETE FROM schema_migrations WHERE version = 8")
            s._conn.commit()
        finally:
            s.close()
        shutil.rmtree(wt)
        shutil.rmtree(admin)                      # no git evidence left either
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        res = self._upgrade(tmp_path, events, caplog)
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert res["worktree_evidence"]["attributed"] == 1
        assert _q(tmp_path, "SELECT source FROM known_git_roots WHERE root_path = ?",
                  (str(wt / "pkg"),)) == [("attributed",)]

    def test_second_recovery_pass_counts_the_rows_already_registered(self, env, caplog):
        """The log of a later recovery pass says how many removed folders were
        registered before, instead of a bare 0 (#149, #161 log text)."""
        tmp_path, events, repo, wt, admin, _other = env
        _insert(events, "s1", str(wt / "pkg" / "a.py"), f"{D}T10:00:00Z")
        s = _store(tmp_path)
        try:
            s.attribute_incremental(events)
            s._conn.execute("DELETE FROM known_git_roots")   # as v1.36.x
            s._conn.execute("DELETE FROM schema_migrations WHERE version = 8")
            s._conn.commit()
        finally:
            s.close()
        shutil.rmtree(wt)
        shutil.rmtree(admin)
        res = self._upgrade(tmp_path, events, caplog)
        assert res["worktree_evidence"]["attributed"] == 1
        assert res["worktree_evidence"]["attributed_known"] == 0
        msgs = [r.getMessage() for r in caplog.records]
        assert any("1 carpetas borradas ya atribuidas a un repo se registran "
                   "ahora (0 ya estaban registradas)" in m for m in msgs)
        caplog.clear()
        s = _store(tmp_path)
        try:
            s._conn.execute("INSERT OR REPLACE INTO rollup_meta (key, value)"
                            " VALUES ('worktree_recover', 'pending')")
            s._conn.execute("INSERT OR REPLACE INTO rollup_meta (key, value)"
                            " VALUES ('active_days_rederive', 'pending')")
            s._conn.commit()
            with caplog.at_level(logging.INFO, logger="hub"):
                res2 = s.attribute_incremental(events)
        finally:
            s.close()
        assert res2["worktree_evidence"]["attributed"] == 0
        assert res2["worktree_evidence"]["attributed_known"] == 1
        msgs = [r.getMessage() for r in caplog.records]
        assert any("0 carpetas borradas ya atribuidas a un repo se registran "
                   "ahora (1 ya estaban registradas)" in m for m in msgs)
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}

    def test_no_evidence_stays_as_it_is(self, env, caplog):
        tmp_path, events, _wt = self._seed(env, admin_entry=False)
        before = _keys_of(tmp_path, "s1")
        res = self._upgrade(tmp_path, events, caplog)
        assert _keys_of(tmp_path, "s1") == before
        assert res["worktree_edges_recovered"] == 0
        ev = res["worktree_evidence"]
        assert ev["git_worktrees"] == 0 and ev["worktree_add"] == 0

    def test_orphan_rows_leave_the_portfolio_after_the_rollup(self, env, caplog):
        tmp_path, events, _wt = self._seed(env, admin_entry=True)
        self._upgrade(tmp_path, events, caplog)
        s = _store(tmp_path)
        try:
            s.build_rollup(tmp_path / "github.db")
        finally:
            s.close()
        # Only the never-a-repo folder is left as path_hash.
        assert _q(tmp_path, "SELECT COUNT(*) FROM workspaces"
                            " WHERE kind = 'path_hash'") == [(1,)]

    def test_idempotent_second_pass_is_a_noop(self, env, caplog):
        tmp_path, events, _wt = self._seed(env, admin_entry=True)
        self._upgrade(tmp_path, events, caplog)
        edges = _q(tmp_path, "SELECT session_id, file_path, workspace_id"
                             " FROM path_attributions ORDER BY 1, 2")
        backups = len(_backups(tmp_path))
        res = _incremental(tmp_path, events)
        assert "worktree_evidence" not in res and res["attributed"] == 0
        assert len(_backups(tmp_path)) == backups
        _store(tmp_path).close()                         # migration runs once
        assert "worktree_recover" not in _meta(tmp_path)
        assert _q(tmp_path, "SELECT session_id, file_path, workspace_id"
                            " FROM path_attributions ORDER BY 1, 2") == edges

    def test_cut_midway_resumes_without_a_new_backup(self, env, monkeypatch, caplog):
        tmp_path, events, _wt = self._seed(env, admin_entry=True)
        _store(tmp_path).close()                         # migration 8
        real = WorkspaceStore._attribute_event_range

        def _boom(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", _boom)
        backups = len(_backups(tmp_path))
        with pytest.raises(sqlite3.OperationalError):
            _incremental(tmp_path, events)
        meta = _meta(tmp_path)
        assert meta["worktree_recover"] == "running"
        assert meta["worktree_recover_before"] == "2"
        assert len(_backups(tmp_path)) == backups + 1

        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", real)
        with caplog.at_level(logging.INFO, logger="hub"):
            res = _incremental(tmp_path, events)
        assert len(_backups(tmp_path)) == backups + 1     # no second backup
        assert _keys_of(tmp_path, "s1") == {REPO_KEY}
        assert res["worktree_edges_recovered"] == 1        # from the first count
        assert any("sin un respaldo nuevo" in r.getMessage() for r in caplog.records)
        assert "worktree_recover" not in _meta(tmp_path)

    def test_conflicting_evidence_folds_nothing(self, env, caplog):
        _tmp, _events, repo, wt, _admin, _other = env
        rival = _mkrepo(repo.parent, "globex-app", "git@github.com:globex/app.git")
        # The rival repo's git ALSO claims the same worktree path.
        entry = rival / ".git" / "worktrees" / "wt"
        entry.mkdir(parents=True)
        (entry / "gitdir").write_text(f"{wt / '.git'}\n")
        tmp_path, events, _wt = self._seed(env, admin_entry=True)
        # The rival repo must be known to the DB for its entry to be read.
        _insert(events, "r1", str(rival / "x.py"), f"{D}T07:00:00Z")
        _full(tmp_path, events)
        res = self._upgrade(tmp_path, events, caplog)
        assert res["worktree_evidence"]["conflicting"] == 1
        assert next(iter(_keys_of(tmp_path, "s1"))).startswith("path_hash:")


class TestParseWorktreeAdds:
    CWD = "/Users/u/work/widget-services"

    def test_relative_path_from_the_cwd(self):
        assert parse_worktree_adds(
            "git worktree add -q -b fix/x ../widget-services-wt-12 main",
            self.CWD, True) == [(self.CWD, "/Users/u/work/widget-services-wt-12")]

    def test_cd_and_git_c_move_the_base(self):
        out = parse_worktree_adds(
            "cd /Users/u/work && git -C widget-services worktree add wt-3; ls",
            "/tmp", True)
        assert out == [("/Users/u/work/widget-services",
                        "/Users/u/work/widget-services/wt-3")]

    def test_a_newline_separates_commands(self):
        assert parse_worktree_adds(
            "cd /Users/u/work/widget-services\ngit worktree add ../wt-7 -b x",
            "/", True) == [("/Users/u/work/widget-services", "/Users/u/work/wt-7")]

    def test_home_is_expanded(self):
        assert parse_worktree_adds(
            "cd ~/work/widget-services && git worktree add $HOME/work/wt-4",
            "/", True, home="/Users/u") == [
                ("/Users/u/work/widget-services", "/Users/u/work/wt-4")]

    def test_cut_text_never_yields_its_last_token(self):
        cut = "git worktree add -b fix/x ../widget-services-w"
        assert parse_worktree_adds(cut, self.CWD, False) == []
        assert parse_worktree_adds(cut + "t-12 main", self.CWD, False) == [
            (self.CWD, "/Users/u/work/widget-services-wt-12")]

    @pytest.mark.parametrize("cmd", [
        "git worktree add ../$NAME",
        "git worktree add ../wt-*",
        "echo x | git worktree add ../wt-5",
        "git worktree add ../wt-5 | tee log",
        "cd - && git worktree add ../wt-6",
        "git worktree list",
        "git worktree add -b only-a-branch",
    ])
    def test_unreadable_commands_yield_nothing(self, cmd):
        assert parse_worktree_adds(cmd, self.CWD, True) == []

    def test_relative_cwd_yields_nothing(self):
        assert parse_worktree_adds("git worktree add ../x", "rel", True) == []

