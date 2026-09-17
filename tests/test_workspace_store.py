"""Tests for WorkspaceStore + the read-only backfill pass (issue #20)."""

import sqlite3
import time
from pathlib import Path

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity
from hub.mcp_server import (
    _get_session_workspaces,
    _get_workspace_sessions,
    _list_workspaces,
)


def _mkrepo(root: Path, name: str, remote: str | None = None) -> Path:
    d = root / name
    (d / ".git").mkdir(parents=True)
    cfg = "[core]\n\tbare = false\n"
    if remote is not None:
        cfg += f'[remote "origin"]\n\turl = {remote}\n'
    (d / ".git" / "config").write_text(cfg)
    return d


def _make_events_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (session_id, provider, file_path, cwd)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
        event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
        tokens_json TEXT, tool_name TEXT, file_path TEXT, model TEXT, cwd TEXT,
        fingerprint TEXT, created_at REAL NOT NULL)""")
    for sess, prov, fp, cwd in rows:
        c.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " session_id, tool_name, file_path, cwd, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (prov, "p", "tool_use", "2026-01-01T00:00:00", "s", sess, "Edit", fp, cwd, time.time()),
        )
    c.commit()
    c.close()
    return path


@pytest.fixture
def store(tmp_path) -> WorkspaceStore:
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s
    s.close()


class TestSchema:
    def test_tables_created(self, store):
        with store._lock:
            tables = {
                r[0] for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"workspaces", "path_attributions", "schema_migrations"}.issubset(tables)


class TestUpsertAndSelfHeal:
    def test_upsert_is_stable(self, store):
        ident = WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote", remote_url="h/o/r")
        a = store.upsert_workspace(ident)
        b = store.upsert_workspace(ident)
        assert a == b

    def test_rerun_repoints_attribution_when_repo_gains_remote(self, tmp_path, store):
        """The real self-heal path: a checkout with no remote resolves to a
        git_root workspace; after it gains a remote, a re-run repoints the *same*
        (session, provider, file_path) attribution to the git_remote workspace.

        (A directory gaining a remote produces a *different* workspace_key, so
        the healing happens on ``path_attributions.workspace_id`` via ON CONFLICT
        DO UPDATE — not by mutating a workspace row in place.)
        """
        r = _mkrepo(tmp_path, "proj", remote=None)
        fp = str(r / "x.py")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", fp, None)])

        store.backfill_from_events(ev)
        ws = store.get_session_workspaces("s1")
        assert len(ws) == 1
        assert ws[0]["kind"] == "git_root"

        # The checkout gains an origin remote.
        (r / ".git" / "config").write_text(
            '[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:acme/proj.git\n'
        )
        store.backfill_from_events(ev)

        ws2 = store.get_session_workspaces("s1")
        assert len(ws2) == 1  # attribution repointed, not duplicated
        assert ws2[0]["kind"] == "git_remote"
        assert ws2[0]["workspace_key"] == "git_remote:github.com/acme/proj"
        with store._lock:
            n = store._conn.execute(
                "SELECT COUNT(*) FROM path_attributions WHERE session_id='s1'"
            ).fetchone()[0]
        assert n == 1  # one edge, resolved_via updated in place

    def test_first_seen_pinned_across_upserts(self, store):
        ident = WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote", remote_url="h/o/r")
        wid = store.upsert_workspace(ident)
        with store._lock:
            first = store._conn.execute(
                "SELECT first_seen FROM workspaces WHERE id=?", (wid,)
            ).fetchone()[0]
        store.upsert_workspace(
            WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote",
                              remote_url="h/o/r", root_path="/new/path")
        )
        with store._lock:
            row = store._conn.execute(
                "SELECT first_seen, root_path FROM workspaces WHERE id=?", (wid,)
            ).fetchone()
        assert row[0] == first          # first_seen pinned
        assert row[1] == "/new/path"    # evidence refreshed


class TestBackfillMxN:
    def test_one_session_many_workspaces(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        B = _mkrepo(tmp_path, "B", "https://github.com/acme/B")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
            ("s1", "claude", str(B / "y.py"), None),
        ])
        result = store.backfill_from_events(ev)
        assert result["attributed"] == 2
        ws = store.get_session_workspaces("s1")
        keys = {w["workspace_key"] for w in ws}
        assert keys == {"git_remote:github.com/acme/a", "git_remote:github.com/acme/b"}

    def test_one_workspace_many_sessions(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
            ("s2", "codex", str(A / "z.py"), None),
        ])
        store.backfill_from_events(ev)
        sessions = store.get_workspace_sessions("git_remote:github.com/acme/a")
        pairs = {(s["session_id"], s["provider"]) for s in sessions}
        assert pairs == {("s1", "claude"), ("s2", "codex")}

    def test_three_ladder_rungs_and_skips(self, tmp_path, store):
        remote = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        rootonly = _mkrepo(tmp_path, "L", remote=None)
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(remote / "a.py"), None),      # git_remote
            ("s1", "claude", str(rootonly / "b.py"), None),    # git_root
            ("s1", "claude", "/gone/dir/c.py", None),          # path_hash (nonexistent)
            ("s1", "claude", "gh issue list --repo x", None),  # skipped (non-absolute)
        ])
        result = store.backfill_from_events(ev)
        assert result["attributed"] == 3
        assert result["skipped_non_absolute"] == 1
        kinds = {w["kind"] for w in store.get_session_workspaces("s1")}
        assert kinds == {"git_remote", "git_root", "path_hash"}

    def test_provider_disambiguates_same_session_id(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("shared", "claude", str(A / "x.py"), None),
            ("shared", "codex", str(A / "x.py"), None),
        ])
        store.backfill_from_events(ev)
        # same session_id + file, two providers → two distinct attributions
        sessions = store.get_workspace_sessions("git_remote:github.com/acme/a")
        assert len(sessions) == 2

    def test_backfill_idempotent(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
        ])
        r1 = store.backfill_from_events(ev)
        r2 = store.backfill_from_events(ev)
        assert r1["workspaces"] == r2["workspaces"] == 1
        with store._lock:
            n = store._conn.execute("SELECT COUNT(*) FROM path_attributions").fetchone()[0]
        assert n == 1

    def test_events_db_never_written(self, tmp_path, store):
        """The backfill opens events.db read-only; it must be byte-identical after."""
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", str(A / "x.py"), None)])
        before = ev.read_bytes()
        store.backfill_from_events(ev)
        assert ev.read_bytes() == before


class TestMcpReadSurface:
    def test_helpers_return_empty_when_db_absent(self, tmp_path):
        """A fresh install has no workspace.db — the MCP tools must not explode."""
        missing = str(tmp_path / "nope.db")
        assert _get_session_workspaces(missing, "s1") == []
        assert _get_workspace_sessions(missing, "git_remote:h/o/r") == []
        assert _list_workspaces(missing) == []

    def test_helpers_read_backfilled_db(self, tmp_path):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", str(A / "x.py"), None)])
        db = tmp_path / "workspace.db"
        s = WorkspaceStore(db)
        s.backfill_from_events(ev)
        s.close()

        ws = _get_session_workspaces(str(db), "s1")
        assert ws and ws[0]["workspace_key"] == "git_remote:github.com/acme/a"
        assert _get_workspace_sessions(str(db), "git_remote:github.com/acme/a")
        assert _list_workspaces(str(db))
