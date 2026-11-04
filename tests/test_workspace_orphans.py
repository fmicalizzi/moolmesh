"""#46: build_rollup prunes orphan workspaces, never registered repos or live ones."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hub.cache import workspace_store as ws
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity, resolve_dir
from tests.test_workspace_store import _make_github_db


def _ident(key: str, root: str) -> WorkspaceIdentity:
    return WorkspaceIdentity(key=key, kind="git_root", root_path=root)


def _keys(s: WorkspaceStore) -> set[str]:
    with s._lock:
        return {k for (k,) in s._conn.execute("SELECT workspace_key FROM workspaces")}


def test_orphan_pruned_registered_and_active_kept(tmp_path, monkeypatch):
    reg = tmp_path / "registered"
    (reg / ".git").mkdir(parents=True)  # no remote → git_root key
    gh = _make_github_db(tmp_path / "github.db", [(1, str(reg))], [])
    s = WorkspaceStore(tmp_path / "workspace.db")
    s.upsert_workspace(_ident("git_root:/synthetic/orphan", "/synthetic/orphan"))
    reg_key = resolve_dir(str(reg)).key
    s.upsert_workspace(resolve_dir(str(reg)))
    active = s.upsert_workspace(_ident("git_root:/synthetic/active", "/synthetic/active"))
    with s._lock:  # only an active day keeps it
        s._conn.execute(
            "INSERT INTO edge_active_days (session_id, provider, workspace_id, via, day)"
            " VALUES ('s1', 'claude', ?, 'file', '2026-11-01')", (active,))
        s._conn.commit()
    infos: list[str] = []
    monkeypatch.setattr(ws._log, "info", lambda msg, *a, **k: infos.append(msg % a))
    stats = s.build_rollup(gh)
    assert stats["orphans_pruned"] == 1
    assert _keys(s) == {reg_key, "git_root:/synthetic/active"}
    assert any("1 workspace(s) huérfano" in m for m in infos)
    # Idempotent: nothing left to prune.
    assert s.build_rollup(gh)["orphans_pruned"] == 0


def test_live_signals_and_project_key_target_kept(tmp_path):
    s = WorkspaceStore(tmp_path / "workspace.db")
    d = tmp_path / "plain"
    d.mkdir()
    s.record_touch(str(d / "a.txt"), 1_700_000_000.0, resolve_dir(str(d)))
    s.record_attribution("s1", "claude", "/synthetic/sess/x.py",
                         _ident("git_root:/synthetic/sess", "/synthetic/sess"))
    target = "git_root:/synthetic/parent"
    s.upsert_workspace(_ident(target, "/synthetic/parent"))
    child = s.upsert_workspace(_ident("git_root:/synthetic/parent/sub", "/synthetic/parent/sub"))
    with s._lock:
        s._conn.execute(
            "INSERT INTO workspace_classification (workspace_id, category, subtype, role,"
            " project_key, project_label, resolved_via, classified_at)"
            " VALUES (?, 'B', 'subdir', 'nest', ?, 'parent', 'subdir', 'x')",
            (child, target))
        s._conn.commit()
    s.build_rollup(tmp_path / "absent-github.db")
    keys = _keys(s)
    assert target in keys  # another workspace's project_key
    assert "git_root:/synthetic/sess" in keys
    assert resolve_dir(str(d)).key in keys
    # The classified child itself has no activity → pruned (classification cascades).
    assert "git_root:/synthetic/parent/sub" not in keys
    with s._lock:
        assert s._conn.execute(
            "SELECT COUNT(*) FROM workspace_classification").fetchone()[0] == 0


def test_unreadable_registry_skips_prune(tmp_path):
    gh = tmp_path / "github.db"
    sqlite3.connect(gh).execute("CREATE TABLE unrelated (x)").connection.close()
    s = WorkspaceStore(tmp_path / "workspace.db")
    s.upsert_workspace(_ident("git_root:/synthetic/orphan", "/synthetic/orphan"))
    stats = s.build_rollup(gh)
    assert stats["orphans_pruned"] == 0
    assert "git_root:/synthetic/orphan" in _keys(s)
