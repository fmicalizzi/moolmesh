"""Read surfaces count calls, not directories (#48.1)."""

from __future__ import annotations

from hub.adapters.codex_adapter import CodexAdapter, is_codex_dir_extra
from hub.cache.event_store import EventStore
from hub.dashboard.server import SessionTracker
from hub.mcp_server import _get_project_activity, _get_tool_stats
from hub.models.codex import CodexEntry, CodexFunctionCall


def _patch(*paths: str, ts: str = "2026-09-21T10:00:05Z") -> CodexEntry:
    body = "".join(f"*** Update File: {p}\n@@\n-x\n+y\n" for p in paths)
    return CodexEntry(
        event_type="response_item", timestamp=ts, payload_type="custom_tool_call",
        function_call=CodexFunctionCall(
            call_id="c1", name="apply_patch",
            arguments=f"*** Begin Patch\n{body}*** End Patch\n"),
        session_id="sess-1", cwd="/r/a",
    )


def _events():
    # One call touching three directories -> 1 primary + 2 extras.
    return CodexAdapter().to_events(_patch("/r/a/1.md", "/r/b/2.md", "/r/c/3.md"), "proj")


def test_extras_are_recognized_and_primary_is_not():
    evs = [e.to_dict() for e in _events()]
    assert len(evs) == 3
    assert not is_codex_dir_extra(evs[0])
    assert all(is_codex_dir_extra(e) for e in evs[1:])


def test_non_codex_same_shape_is_not_an_extra():
    ev = {"provider": "pi", "tool_name": "Edit", "file_path": "/x/y.md",
          "summary": "Edit: /x/y.md"}
    assert not is_codex_dir_extra(ev)


def test_tool_stats_count_calls_not_directories(tmp_path):
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    store.store_batch([e.to_dict() for e in _events()])
    # A second, single-directory call.
    store.store_batch([e.to_dict() for e in CodexAdapter().to_events(
        _patch("/r/a/9.md", ts="2026-09-21T10:00:06Z"), "proj")])
    store.close()

    stats = _get_tool_stats(str(db))
    assert stats == [{"tool_name": "apply_patch", "count": 2, "projects": 1}]
    activity = _get_project_activity(str(db), "proj")
    assert activity["top_tools"] == [{"tool_name": "apply_patch", "count": 2}]


def test_dashboard_tracker_skips_extras():
    tracker = SessionTracker()
    for e in _events():
        tracker.track(e.to_dict())
    assert tracker.get_tool_stats() == [{"name": "apply_patch", "count": 1}]
    assert tracker.projects["codex:proj"]["tool_calls"] == 1


# --- #48.2: cwd-fallback edges are not "files" ---------------------------------


def _ws_store(tmp_path, monkeypatch):
    from hub.cache.workspace_store import WorkspaceStore
    from hub.correlation.workspace_resolver import resolve_dir

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    ident = resolve_dir(str(plain))
    store = WorkspaceStore(tmp_path / "workspace.db")
    # s-file: two real files. s-cwd: only a cwd-fallback edge.
    store.record_attribution("s-file", "codex", str(plain / "a.md"), ident)
    store.record_attribution("s-file", "codex", str(plain / "b.md"), ident)
    store.record_attribution("s-cwd", "codex", str(plain), ident)
    with store._lock:
        store._conn.execute(
            "UPDATE path_attributions SET via = 'cwd' WHERE session_id = 's-cwd'")
        store._conn.commit()
    return store, ident, tmp_path / "workspace.db"


def test_session_workspaces_split_cwd_edges(tmp_path, monkeypatch):
    from hub.mcp_server import _get_session_workspaces

    store, _, db = _ws_store(tmp_path, monkeypatch)
    try:
        (f,) = store.get_session_workspaces("s-file")
        assert (f["files"], f["cwd_edges"]) == (2, 0)
        (c,) = store.get_session_workspaces("s-cwd")
        assert (c["files"], c["cwd_edges"]) == (0, 1)
    finally:
        store.close()
    (m,) = _get_session_workspaces(str(db), "s-cwd")
    assert (m["files"], m["cwd_edges"]) == (0, 1)


def test_workspace_sessions_and_list_split_cwd_edges(tmp_path, monkeypatch):
    from hub.mcp_server import _get_workspace_sessions, _list_workspaces

    store, ident, db = _ws_store(tmp_path, monkeypatch)
    try:
        (ws,) = store.list_workspaces()
        assert (ws["attributions"], ws["files"], ws["cwd_edges"]) == (3, 2, 1)
        key = ws["workspace_key"]
        by_session = {r["session_id"]: r for r in store.get_workspace_sessions(key)}
        assert (by_session["s-cwd"]["files"], by_session["s-cwd"]["cwd_edges"]) == (0, 1)
        assert (by_session["s-file"]["files"], by_session["s-file"]["cwd_edges"]) == (2, 0)
    finally:
        store.close()
    (mws,) = _list_workspaces(str(db))
    assert (mws["files"], mws["cwd_edges"]) == (2, 1)
    rows = {r["session_id"]: r for r in _get_workspace_sessions(str(db), key)}
    assert rows["s-cwd"]["files"] == 0 and rows["s-cwd"]["cwd_edges"] == 1


def test_cli_files_label():
    from hub.cli import _files_label

    assert _files_label({"files": 2, "cwd_edges": 0}) == "2 files"
    assert _files_label({"files": 0, "cwd_edges": 1}) == "0 files (+1 cwd)"
