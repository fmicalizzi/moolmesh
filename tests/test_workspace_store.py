"""Tests for WorkspaceStore + the read-only backfill pass (issue #20)."""

import sqlite3
import time
from pathlib import Path

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity
from hub.mcp_server import (
    _get_portfolio,
    _get_session_workspaces,
    _get_workspace_activity,
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


def _make_github_db(path: Path, repos: list[tuple[int, str]],
                    commits: list[tuple[int, str, str]]) -> Path:
    """repos: (repo_id, path). commits: (repo_id, sha, timestamp)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE repos (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, remote_url TEXT)""")
    c.execute("""CREATE TABLE git_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        sha TEXT NOT NULL, timestamp TEXT NOT NULL)""")
    for rid, rpath in repos:
        c.execute("INSERT INTO repos (id, path) VALUES (?, ?)", (rid, rpath))
    for rid, sha, ts in commits:
        c.execute("INSERT INTO git_commits (repo_id, sha, timestamp) VALUES (?,?,?)",
                  (rid, sha, ts))
    c.commit()
    c.close()
    return path


class TestPortfolioRollup:
    def test_schema_table_created(self, store):
        with store._lock:
            tables = {r[0] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "workspace_rollup" in tables

    def test_signal_agnostic_union(self, tmp_path):
        """One workspace lights up from session + filesystem + git — a single
        node, three signals — because all three resolve on the same ladder key."""
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        key = "git_remote:github.com/acme/r"
        s = WorkspaceStore(tmp_path / "workspace.db")
        # session edge on a deep file inside the repo
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("sess1", "claude", str(repo / "src" / "x.py"),
                             resolve_path(str(repo / "src" / "x.py")))
        # filesystem touch on another deep file
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(repo / "y.py"), 1_700_000_000.0,
                       resolve_dir(str(repo)))
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "abc", "2026-02-01T10:00:00")])
        r = s.build_rollup(gh)
        assert r["multi_source_nodes"] == 1
        assert r["git_repos_matched"] == 1 and r["git_repos_new"] == 0

        pf = s.get_portfolio()
        node = [p for p in pf if p["workspace_key"] == key]
        assert len(node) == 1
        assert set(node[0]["sources"]) == {"session", "filesystem", "git"}
        assert node[0]["session_touches"] == 1
        assert node[0]["fs_touches"] == 1
        assert node[0]["git_touches"] == 1
        s.close()

    def test_iso_epoch_trap_fs_grouped_by_iso_day(self, tmp_path):
        """path_touches.mtime is REAL epoch but last_seen is ISO — the rollup
        must group on the ISO day and never yield an empty date string."""
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        s.build_rollup(tmp_path / "absent-github.db")
        with s._lock:
            days = [r[0] for r in s._conn.execute(
                "SELECT day FROM workspace_rollup").fetchall()]
        assert days and all(len(day) == 10 and day != "" for day in days)
        # ISO date, not an epoch-derived or blank value
        assert all(day[4] == "-" and day[7] == "-" for day in days)
        s.close()

    def test_insert_or_replace_preserves_prior_days(self, tmp_path):
        """A re-touch re-dates path_touches.last_seen to today, but the rollup
        keyed INSERT OR REPLACE must keep the earlier day's row — the rollup is
        the only durable per-day fs record (never wipe-and-rebuild)."""
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        ident = resolve_dir(str(d))
        wid = s.record_touch(str(d / "a.bin"), 1_700_000_000.0, ident)
        # Simulate an earlier rollup day already materialized.
        with s._lock:
            s._conn.execute(
                """INSERT INTO workspace_rollup
                   (workspace_id, day, session_touches, fs_touches, git_touches,
                    last_activity, built_at)
                   VALUES (?, '2020-01-01', 0, 3, 0, '2020-01-01T00:00:00', 'x')""",
                (wid,))
            s._conn.commit()
        s.build_rollup(tmp_path / "absent-github.db")
        with s._lock:
            old = s._conn.execute(
                "SELECT fs_touches FROM workspace_rollup WHERE day='2020-01-01'"
            ).fetchone()
        assert old is not None and old[0] == 3  # earlier day survived
        s.close()

    def test_since_filter_string_compare(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        wid = s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        with s._lock:
            for day in ("2020-01-01", "2030-12-31"):
                s._conn.execute(
                    """INSERT OR REPLACE INTO workspace_rollup
                       (workspace_id, day, session_touches, fs_touches,
                        git_touches, last_activity, built_at)
                       VALUES (?, ?, 0, 1, 0, ?, 'x')""",
                    (wid, day, day + "T00:00:00"))
            s._conn.commit()
        key = s.list_workspaces()[0]["workspace_key"]
        recent = s.get_workspace_activity(key, since="2025-01-01")
        assert [r["day"] for r in recent] == ["2030-12-31"]
        s.close()

    def test_absent_github_db_git_contributes_nothing(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        r = s.build_rollup(tmp_path / "does-not-exist.db")
        assert r["git_repos_matched"] == 0 and r["git_repos_new"] == 0
        assert all(p["git_touches"] == 0 for p in s.get_portfolio())
        s.close()

    def test_mcp_portfolio_masking(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        db = tmp_path / "workspace.db"
        s = WorkspaceStore(db)
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("sess1", "claude", str(repo / "x.py"),
                             resolve_path(str(repo / "x.py")))
        s.build_rollup(tmp_path / "absent.db")
        s.close()

        import hub.mcp_server as mcp
        # unmasked
        rows = _get_portfolio(str(db))
        assert rows and rows[0]["remote_url"] == "github.com/acme/r"
        # masked
        orig = mcp._hide_project_names
        mcp._hide_project_names = lambda: True
        try:
            masked = _get_portfolio(str(db))
        finally:
            mcp._hide_project_names = orig
        assert masked[0]["remote_url"] is None
        assert masked[0]["label"] and masked[0]["workspace_key"]  # key preserved
        # per-day view
        act = _get_workspace_activity(str(db), rows[0]["workspace_key"])
        assert act and act[0]["sources"] == ["session"]

    def test_mcp_portfolio_empty_when_db_absent(self, tmp_path):
        assert _get_portfolio(str(tmp_path / "nope.db")) == []
        assert _get_workspace_activity(str(tmp_path / "nope.db"), "k") == []
