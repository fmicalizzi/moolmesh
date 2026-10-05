"""Shell-tool events never mint workspaces (#58).

Shell tools put the COMMAND in ``events.file_path``; a command starting with an
absolute binary path passed ``LIKE '/%'`` and created fake workspaces. The
attribution ignores ``file_path`` for the shell tools by tool NAME (never by
text pattern, which would drop real folders with spaces), and the full pass
purges legacy edges whose only evidence is a shell event (the rollup's
``session_touches`` reconcile then zeroes their days).
"""

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from tests.test_workspace_edge_clock import (
    _add_event,
    _make_events_db,
    _utc,
)
from tests.test_workspace_store import _mkrepo

# (provider, tool_name) pairs that carry a command in file_path. Codex `shell`
# is the legacy function name (command[:80]); `exec_command` is its successor.
SHELL_CARRIERS = [
    ("claude", "Bash"),
    ("opencode", "bash"),
    ("qwen", "run_shell_command"),
    ("codex", "shell"),
    ("codex", "exec_command"),
]


def _count(store: WorkspaceStore) -> int:
    with store._lock:
        return store._conn.execute(
            "SELECT COUNT(*) FROM path_attributions").fetchone()[0]


@pytest.mark.parametrize("provider,tool", SHELL_CARRIERS)
def test_shell_command_in_file_path_creates_no_edge(tmp_path, provider, tool):
    events = _make_events_db(tmp_path / "events.db")
    _add_event(events, "s1", "/usr/bin/foo --flag", provider=provider,
               tool_name=tool)
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        r = s.attribute_incremental(events)
        assert r["attributed"] == 0
        assert _count(s) == 0
    finally:
        s.close()


def test_real_folder_touched_by_shell_only_is_purged_on_full_pass(tmp_path):
    """A stale pre-#58 edge minted from a command string is deleted by the full
    pass; the workspace row itself survives (orphans are #46's business)."""
    events = _make_events_db(tmp_path / "events.db")
    _add_event(events, "s1", "/opt/bin/tool --flag", provider="claude",
               tool_name="Bash")
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        ident = resolve_dir("/opt/bin")
        wid = s.upsert_workspace(ident)
        with s._lock:
            s._record_attribution_locked(
                "s1", "claude", "/opt/bin/tool --flag", wid, ident.kind,
                "2026-09-20T00:00:00+00:00")
            s._conn.commit()
        assert _count(s) == 1

        r = s.backfill_from_events(events)  # full pass
        assert r["shell_edges_removed"] == 1
        assert _count(s) == 0
        with s._lock:
            workspaces = s._conn.execute(
                "SELECT COUNT(*) FROM workspaces").fetchone()[0]
        assert workspaces == 1  # never deletes the workspace (#46 owns that)
    finally:
        s.close()


def test_edge_with_a_real_file_also_seen_by_shell_survives(tmp_path):
    """An edge is purged only when EVERY event of the key is a shell event; a
    real file corroborated by a file-tool event keeps the edge and its clock."""
    events = _make_events_db(tmp_path / "events.db")
    repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
    f = str(repo / "a.py")
    _add_event(events, "s1", f, provider="claude", tool_name="Bash")
    _add_event(events, "s1", f, provider="claude", tool_name="Edit",
               created_at=_utc(2026, 9, 15, 10))
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        r = s.backfill_from_events(events)
        assert r["shell_edges_removed"] == 0
        assert _count(s) == 1
        with s._lock:
            ats = s._conn.execute(
                "SELECT activity_ts FROM path_attributions").fetchone()[0]
        assert ats.startswith("2026-09-15T10:00:00")
    finally:
        s.close()


def test_codex_exec_patch_path_is_not_filtered(tmp_path):
    """Codex ``exec`` is NOT a shell carrier since #40: its file_path is a real
    patch path and must keep minting the edge."""
    events = _make_events_db(tmp_path / "events.db")
    repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
    _add_event(events, "s1", str(repo / "patch.tsx"), provider="codex",
               tool_name="exec", created_at=_utc(2026, 9, 15, 10))
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        s.attribute_incremental(events)
        assert _count(s) == 1
    finally:
        s.close()


def test_shell_event_still_feeds_the_cwd_fallback(tmp_path):
    """Only file_path is ignored: the event's real cwd still attributes the
    session through the #40 fallback (a shell-only session is not dropped)."""
    events = _make_events_db(tmp_path / "events.db")
    plain = tmp_path / "plain"
    plain.mkdir()
    _add_event(events, "s1", "/usr/bin/foo --flag", cwd=str(plain),
               provider="claude", tool_name="Bash")
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        s.attribute_incremental(events)
        with s._lock:
            rows = s._conn.execute(
                "SELECT via, file_path FROM path_attributions").fetchall()
        assert rows == [("cwd", str(plain))]
        with s._lock:
            keys = [r[0] for r in s._conn.execute(
                "SELECT workspace_key FROM workspaces").fetchall()]
        assert keys and all("/usr/bin" not in k for k in keys)
    finally:
        s.close()
