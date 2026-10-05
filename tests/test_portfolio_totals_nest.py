"""Grouped totals include nested children (#57).

``derive_project_states`` always read every row with a ``project_key``
(project + collapse + nest); the grouped read summed only project/collapse. A
project whose activity lives in a subfolder or materials child showed a state
with 0 own sessions/days. One rule now: totals fold ``nest`` too. No double
count is possible — every ``path_attributions`` edge belongs to exactly one
workspace, and ``active_days`` unions the days of the folded group.
"""

import sqlite3
from datetime import datetime, timezone

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import (
    WorkspaceIdentity,
    resolve_dir,
    resolve_path,
)
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc


def _classify(s: WorkspaceStore, wid: int, role: str, pk: str,
              subtype: str = "project", label: str | None = None) -> None:
    with s._lock:
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_classification
               (workspace_id, category, subtype, role, project_key,
                project_label, resolved_via, classified_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, "A", subtype, role, pk, label or pk, "self",
             "2026-01-01T00:00:00"),
        )
        s._conn.commit()


def _rollup(s: WorkspaceStore, wid: int, day: str, session: int = 0,
            fs: int = 0) -> None:
    with s._lock:
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_rollup
               (workspace_id, day, session_touches, fs_touches, git_touches,
                last_activity, built_at)
               VALUES (?,?,?,?,0,?,?)""",
            (wid, day, session, fs, f"{day}T10:00:00+00:00",
             "2026-01-01T00:00:00"),
        )
        s._conn.commit()


def test_totals_include_nest_children(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.db")
    try:
        parent = store.upsert_workspace(WorkspaceIdentity(
            key="git_remote:github.com/acme/proj", kind="git_remote",
            remote_url="github.com/acme/proj", root_path="/x/proj"))
        child = store.upsert_workspace(WorkspaceIdentity(
            key="path_hash:child", kind="path_hash", dir_path="/x/proj/reportes"))
        _classify(store, parent, "project", "git_remote:github.com/acme/proj")
        _classify(store, child, "nest", "git_remote:github.com/acme/proj",
                  subtype="materials")
        # The activity lives in the nest child; the parent row is quiet.
        _rollup(store, parent, "2026-09-10", session=2)
        _rollup(store, child, "2026-09-10", session=1)

        g = store.get_portfolio_grouped()
    finally:
        store.close()
    proj = next(p for p in g["projects"]
                if p["project_key"] == "git_remote:github.com/acme/proj")
    # Totals fold the child (3 distinct edges), and the shared day counts once.
    assert proj["session_touches"] == 3
    assert proj["active_days"] == 1
    assert proj["last_activity"] == "2026-09-10T10:00:00+00:00"
    # The child is still its own visible row, by design.
    assert [c["workspace_key"] for c in proj["children"]] == ["path_hash:child"]


def test_project_whose_only_activity_is_nest_has_totals_and_state(
    tmp_path, monkeypatch
):
    """End-to-end through the MCP grouped read: a project whose single edge
    lives in a nest child now shows totals > 0 AND its state (the exact 'state
    with 0/0' incoherence of #57)."""
    import hub.config as cfgmod
    from hub.config import HubConfig
    from hub.mcp_server import _get_portfolio_grouped

    monkeypatch.setattr(cfgmod, "load_config", lambda: HubConfig())
    repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
    materials = tmp_path / "materials"
    materials.mkdir()
    store = WorkspaceStore(tmp_path / "workspace.db")
    try:
        parent_key = resolve_dir(str(repo)).key
        parent_wid = store.upsert_workspace(resolve_dir(str(repo)))
        child_file = str(materials / "brief.pdf")
        child_wid = store.record_attribution(
            "s1", "claude", child_file, resolve_path(child_file))
        _classify(store, parent_wid, "project", parent_key, label="acme/R")
        _classify(store, child_wid, "nest", parent_key, subtype="materials",
                  label="acme/R")
        store.build_rollup(str(tmp_path / "absent-github.db"))

        ev = tmp_path / "events.db"
        c = sqlite3.connect(str(ev))
        c.execute("CREATE TABLE sessions (id TEXT, provider TEXT, cwd TEXT)")
        c.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY, provider TEXT, project TEXT, event_type TEXT,
            timestamp TEXT, summary TEXT, session_id TEXT, created_at REAL NOT NULL,
            file_path TEXT, cwd TEXT, tool_name TEXT, model TEXT,
            tokens_json TEXT, fingerprint TEXT)""")
        now = datetime.now(UTC).timestamp()
        c.execute("INSERT INTO sessions (id, provider, cwd) VALUES ('s1','claude',?)",
                  (str(materials),))
        c.execute(
            "INSERT INTO events (provider, project, event_type, timestamp,"
            " summary, session_id, created_at, file_path) VALUES"
            " ('claude','p','tool_use','', 's','s1',?,?)", (now, child_file))
        c.commit()
        c.close()
    finally:
        store.close()

    data = _get_portfolio_grouped(
        str(tmp_path / "workspace.db"), events_db=str(ev),
        github_db=str(tmp_path / "absent-github.db"))
    proj = next(p for p in data["projects"] if p["project_key"] == parent_key)
    assert proj["session_touches"] == 1     # the nest child's edge, folded
    assert proj["active_days"] == 1
    assert proj["state"]["state"] == "activo"
