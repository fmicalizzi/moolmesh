"""cwd-fallback attribution for sessions without file paths (issue #40, part B).

A session with no ``via='file'`` edge anywhere gets one ``via='cwd'`` edge per
distinct qualifying cwd of its events, resolved on the directory itself. The
moment it gains a file edge its cwd edges are deleted in the same transaction,
and ``build_rollup`` recomputes ``session_touches`` so a retracted cwd edge
leaves no phantom count behind.
"""

import sqlite3
from pathlib import Path

import pytest

import hub.cache.workspace_store as store_mod
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from tests.test_workspace_attribution import _add_event, _count, _make_events_db
from tests.test_workspace_store import _mkrepo


def _edges(store: WorkspaceStore) -> set[tuple]:
    """(session, provider, file_path, via, workspace_key) for every edge."""
    with store._lock:
        return set(store._conn.execute(
            """SELECT a.session_id, a.provider, a.file_path, a.via, w.workspace_key
               FROM path_attributions a JOIN workspaces w ON w.id = a.workspace_id"""
        ).fetchall())


def _rollup_by_key(store: WorkspaceStore) -> dict[str, tuple[int, int, int]]:
    with store._lock:
        return {
            k: (s, f, g) for k, s, f, g in store._conn.execute(
                """SELECT w.workspace_key, SUM(r.session_touches), SUM(r.fs_touches),
                          SUM(r.git_touches)
                   FROM workspace_rollup r JOIN workspaces w ON w.id = r.workspace_id
                   GROUP BY w.id"""
            ).fetchall()
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """events.db, two repos (``site`` holds a sub-folder), a plain dir, a store."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    events = _make_events_db(tmp_path / "events.db")
    site = _mkrepo(tmp_path, "site", "git@github.com:acme/site.git")
    (site / "docs").mkdir()
    other = _mkrepo(tmp_path, "other", "git@github.com:acme/other.git")
    plain = tmp_path / "plain"
    plain.mkdir()
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s, events, site, other, plain, tmp_path
    s.close()


def _cwd_event(db: Path, session: str, cwd: str | None, provider: str = "codex") -> int:
    """A tool/prompt event with no absolute file path — only a cwd."""
    return _add_event(db, session, None, cwd=cwd, provider=provider)


class TestCwdFallbackEdges:
    def test_session_without_file_edges_gets_cwd_edge(self, env):
        s, events, site, *_ = env
        _cwd_event(events, "cx1", str(site))
        _cwd_event(events, "cx1", str(site))  # same cwd again → still one edge
        r = s.attribute_incremental(events)
        assert r["attributed"] == 0
        assert r["cwd_attributed"] == 1
        assert _edges(s) == {
            ("cx1", "codex", str(site), "cwd", "git_remote:github.com/acme/site")}

    def test_cwd_resolves_the_directory_itself_not_its_parent(self, env):
        s, events, _, _, plain, _ = env
        _cwd_event(events, "cx1", str(plain))
        s.attribute_incremental(events)
        (edge,) = _edges(s)
        assert edge[4] == resolve_dir(str(plain)).key
        with s._lock:
            dir_path = s._conn.execute("SELECT dir_path FROM workspaces").fetchone()[0]
        assert dir_path == str(plain)

    def test_one_edge_per_distinct_cwd(self, env):
        s, events, site, other, *_ = env
        _cwd_event(events, "cx1", str(site))
        _cwd_event(events, "cx1", str(other) + "/")  # normalized
        _cwd_event(events, "cx1", str(other))
        s.attribute_incremental(events)
        assert {e[2] for e in _edges(s)} == {str(site), str(other)}

    def test_gaining_file_edge_deletes_cwd_edges(self, env):
        s, events, site, other, *_ = env
        _cwd_event(events, "cx1", str(site))
        _cwd_event(events, "cx1", str(other))
        s.attribute_incremental(events)
        assert {e[3] for e in _edges(s)} == {"cwd"}

        _add_event(events, "cx1", str(site / "docs" / "a.md"), provider="codex")
        r = s.attribute_incremental(events)
        assert r["attributed"] == 1 and r["cwd_attributed"] == 0
        assert _edges(s) == {("cx1", "codex", str(site / "docs" / "a.md"), "file",
                              "git_remote:github.com/acme/site")}

    def test_session_with_file_edges_never_gets_cwd_edge(self, env):
        s, events, site, other, *_ = env
        _add_event(events, "c1", str(site / "a.py"), cwd=str(site))
        s.attribute_incremental(events)
        # Later range: only cwd rows for the same session — still no cwd edge.
        _cwd_event(events, "c1", str(other), provider="claude")
        r = s.attribute_incremental(events)
        assert r["cwd_attributed"] == 0
        assert {e[3] for e in _edges(s)} == {"file"}

    def test_same_range_file_and_cwd_rows_only_file(self, env):
        s, events, site, other, *_ = env
        _cwd_event(events, "c1", str(other), provider="claude")
        _add_event(events, "c1", str(site / "a.py"), cwd=str(site))
        s.attribute_incremental(events)
        assert {e[3] for e in _edges(s)} == {"file"}

    def test_provider_is_part_of_the_session_key(self, env):
        s, events, site, *_ = env
        _add_event(events, "same-id", str(site / "a.py"), provider="claude")
        _cwd_event(events, "same-id", str(site), provider="codex")
        s.attribute_incremental(events)
        assert {(e[1], e[3]) for e in _edges(s)} == {("claude", "file"), ("codex", "cwd")}

    @pytest.mark.parametrize("bad", ["", "/", "relative/dir", "HOME", "HOME/"])
    def test_excluded_cwds(self, env, bad):
        s, events, _, _, _, tmp = env
        cwd = bad.replace("HOME", str(tmp / "home"))
        _cwd_event(events, "cx1", cwd or None)
        r = s.attribute_incremental(events)
        assert r["cwd_attributed"] == 0
        assert _count(s, "path_attributions") == 0
        assert _count(s, "workspaces") == 0

    def test_home_subfolder_is_not_excluded(self, env):
        s, events, _, _, _, tmp = env
        proj = tmp / "home" / "proj"
        proj.mkdir()
        _cwd_event(events, "cx1", str(proj))
        assert s.attribute_incremental(events)["cwd_attributed"] == 1

    def test_rows_without_session_are_ignored(self, env):
        s, events, site, *_ = env
        _cwd_event(events, "", str(site))
        assert s.attribute_incremental(events)["cwd_attributed"] == 0

    def test_rerun_is_idempotent(self, env):
        s, events, site, *_ = env
        _cwd_event(events, "cx1", str(site))
        s.backfill_from_events(events)
        before = _edges(s)
        s.backfill_from_events(events)
        assert _edges(s) == before and len(before) == 1


class TestIncrementalMatchesFull:
    def _populate(self, events, site, other, plain):
        _cwd_event(events, "cx-only", str(site))
        _cwd_event(events, "cx-only", str(other))
        _cwd_event(events, "cx-late-file", str(plain))
        _cwd_event(events, "cx-late-file", str(site))
        _add_event(events, "cl-files", str(site / "a.py"), cwd=str(site))
        _cwd_event(events, "cl-files", str(other), provider="claude")
        _add_event(events, "cx-late-file", str(other / "b.md"), provider="codex")
        _cwd_event(events, "cx-only", str(plain))

    def test_row_by_row_incremental_equals_single_full_pass(self, env):
        s, events, site, other, plain, tmp = env
        self._populate(events, site, other, plain)
        hi = s._read_max_event_id(events)
        for i in range(1, hi + 1):  # one event per pass: maximum churn
            s._attribute_event_range(events, i - 1, i)
        full = WorkspaceStore(tmp / "full.db")
        try:
            full.backfill_from_events(events)
            assert _edges(s) == _edges(full)
        finally:
            full.close()
        assert {(e[0], e[3]) for e in _edges(s)} == {
            ("cx-only", "cwd"), ("cx-late-file", "file"), ("cl-files", "file")}


class TestFailureDiscipline:
    def test_cwd_chunk_failure_rolls_back_and_keeps_cursor(self, env, monkeypatch):
        s, events, site, *_ = env
        _cwd_event(events, "cx1", str(site))
        real = s._upsert_workspace_locked

        def boom(*a, **k):
            real(*a, **k)  # half-written on the shared connection
            raise RuntimeError("disk said no")

        monkeypatch.setattr(s, "_upsert_workspace_locked", boom)
        with pytest.raises(RuntimeError):
            s.attribute_incremental(events)
        assert s.get_attribution_cursor() == 0
        s.set_attribution_cursor(0)  # an unrelated commit on the same conn
        assert _count(s, "workspaces") == 0
        monkeypatch.undo()
        assert s.attribute_incremental(events)["cwd_attributed"] == 1

    def test_resolve_dir_for_cwd_runs_outside_lock(self, env, monkeypatch):
        s, events, site, *_ = env
        _cwd_event(events, "cx1", str(site))
        held = []

        def spy(container):
            held.append(s._lock.locked())
            return resolve_dir(container)

        monkeypatch.setattr(store_mod, "resolve_dir", spy)
        s.attribute_incremental(events)
        assert held == [False]


class TestRollupReconcile:
    def test_retracted_cwd_edge_leaves_no_phantom_session_touch(self, env, monkeypatch):
        s, events, site, other, plain, tmp = env
        gh = tmp / "absent-github.db"
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-20T10:00:00+00:00")
        _cwd_event(events, "cx1", str(plain))  # prompt-only so far
        s.attribute_incremental(events)
        s.build_rollup(gh)
        plain_key = resolve_dir(str(plain)).key
        assert _rollup_by_key(s)[plain_key] == (1, 0, 0)

        # Next day the session edits a file in another workspace.
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-21T10:00:00+00:00")
        _add_event(events, "cx1", str(other / "x.md"), provider="codex")
        s.attribute_incremental(events)
        r = s.build_rollup(gh)
        assert r["session_reconciled"] == 1
        rollup = _rollup_by_key(s)
        assert plain_key not in rollup  # all-zero row pruned
        assert rollup == {"git_remote:github.com/acme/other": (1, 0, 0)}

        full = WorkspaceStore(tmp / "full.db")
        try:
            full.backfill_from_events(events)
            full.build_rollup(gh)
            assert _rollup_by_key(full) == rollup
        finally:
            full.close()

    def test_reconcile_keeps_fs_history_on_the_row(self, env, monkeypatch):
        s, events, site, other, plain, tmp = env
        gh = tmp / "absent-github.db"
        monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-20T10:00:00+00:00")
        _cwd_event(events, "cx1", str(plain))
        s.attribute_incremental(events)
        s.build_rollup(gh)
        # Same day, the filesystem watcher saw a file there (durable fs history).
        with s._lock:
            s._conn.execute(
                "UPDATE workspace_rollup SET fs_touches = 4 WHERE day = '2026-09-20'")
            s._conn.commit()
        _add_event(events, "cx1", str(other / "x.md"), provider="codex")
        s.attribute_incremental(events)
        s.build_rollup(gh)
        assert _rollup_by_key(s)[resolve_dir(str(plain)).key] == (0, 4, 0)

    def test_unchanged_rollup_reconciles_nothing(self, env, monkeypatch):
        s, events, site, *_ = env
        _add_event(events, "c1", str(site / "a.py"))
        s.attribute_incremental(events)
        s.build_rollup(env[5] / "absent-github.db")
        assert s.build_rollup(env[5] / "absent-github.db")["session_reconciled"] == 0


# Literal pre-#40 DDL — NOT _SCHEMA — so the migration is exercised for real.
_OLD_PATH_ATTRIBUTIONS = """
CREATE TABLE workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, remote_url TEXT,
    root_path TEXT, dir_path TEXT, first_seen TEXT NOT NULL
);
CREATE TABLE path_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    resolved_via TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    UNIQUE(session_id, provider, file_path)
);
INSERT INTO workspaces VALUES (1, 'path_hash:x', 'path_hash', NULL, NULL, '/old', 't');
INSERT INTO path_attributions VALUES (1, 's', 'claude', '/old/a.py', 1, 'path_hash', 't');
"""


class TestViaMigration:
    def test_migration_adds_via_on_old_db(self, tmp_path):
        db = tmp_path / "workspace.db"
        c = sqlite3.connect(db)
        c.executescript(_OLD_PATH_ATTRIBUTIONS)
        c.close()

        s = WorkspaceStore(db)
        try:
            with s._lock:
                cols = {r[1] for r in s._conn.execute(
                    "PRAGMA table_info(path_attributions)")}
                via = s._conn.execute("SELECT via FROM path_attributions").fetchall()
                applied = s._conn.execute(
                    "SELECT version, name FROM schema_migrations").fetchall()
            assert "via" in cols
            assert via == [("file",)]
            assert (1, "attribution_via") in applied
        finally:
            s.close()
        WorkspaceStore(db).close()  # re-open: runs once, no duplicate-column error

    def test_fresh_db_has_via_and_records_migration(self, tmp_path):
        s = WorkspaceStore(tmp_path / "workspace.db")
        try:
            with s._lock:
                cols = {r[1] for r in s._conn.execute(
                    "PRAGMA table_info(path_attributions)")}
                applied = {r[0] for r in s._conn.execute(
                    "SELECT version FROM schema_migrations")}
            assert "via" in cols and 1 in applied
        finally:
            s.close()


class TestDeliveryWorkingSet:
    def test_cwd_edges_add_no_extension_signal(self, env):
        s, events, site, *_ = env
        dotted = site / "release.v2"
        dotted.mkdir()
        _cwd_event(events, "cx1", str(dotted))
        s.attribute_incremental(events)
        seen = {}
        real = WorkspaceStore._working_set_exts

        def spy(wid, work_exts, touches, root):
            seen.update({w: dict(e) for w, e in work_exts.items()})
            return real(wid, work_exts, touches, root)

        s._working_set_exts = spy
        s.detect_delivery_candidates()
        assert all(".v2" not in exts for exts in seen.values())
