# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.2.0",
# ]
# ///
"""MoolMesh MCP Server — read-only access to AI agent session data."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any, Optional

# ── Database paths ──────────────────────────────────────────────────
EVENTS_DB = os.path.expanduser("~/.moolmesh/events.db")
GITHUB_DB = os.path.expanduser("~/.moolmesh/github.db")
WORKSPACE_DB = os.path.expanduser("~/.moolmesh/workspace.db")


# ── Helpers ─────────────────────────────────────────────────────────
def _connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_optional(db_path: str) -> sqlite3.Connection | None:
    """Open a read-only connection, or None if the DB does not exist yet.

    ``mode=ro`` raises when the file is absent, so new features whose DB is
    only created after their first backfill (e.g. workspace.db) must guard on
    existence rather than let the read tools explode on a fresh install.
    """
    if not os.path.exists(db_path):
        return None
    return _connect(db_path)


def _rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# ── Pure functions (testable without mcp SDK) ───────────────────────

def _get_schema() -> str:
    """Schema de la base de datos events.db."""
    return """CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,      -- claude | codex | qwen | opencode
    project TEXT NOT NULL,       -- nombre del proyecto
    event_type TEXT NOT NULL,    -- user | assistant | tool_use | tool_result | thinking | summary | reasoning | step-finish
    timestamp TEXT NOT NULL,     -- ISO 8601
    summary TEXT NOT NULL,       -- texto del mensaje o descripción
    session_id TEXT,
    tokens_json TEXT,            -- {"input": N, "output": N, "cached_input": N}
    tool_name TEXT,              -- Read, Edit, Bash, Write, etc.
    file_path TEXT,
    model TEXT,                  -- claude-opus-4-6, gpt-5, qwen-coder, etc.
    cwd TEXT,                    -- working directory
    fingerprint TEXT,
    created_at REAL NOT NULL
);
Índices: timestamp, provider, project, session_id, fingerprint (unique partial).
"""


def _get_projects_resource(db_path: str) -> str:
    """Lista de todos los proyectos con estadísticas."""
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT provider, project,
            COUNT(*) AS events,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.input'), 0) ELSE 0 END) AS input_tokens,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.output'), 0) ELSE 0 END) AS output_tokens,
            MAX(timestamp) AS last_event
        FROM events GROUP BY provider, project ORDER BY last_event DESC
    """).fetchall()
    conn.close()
    lines = [
        f"{r['provider']:10} {r['project']:40} {r['events']:>6} events  "
        f"{r['input_tokens']+r['output_tokens']:>8} tokens  last: {(r['last_event'] or '')[:19]}"
        for r in rows
    ]
    return "\n".join(lines)


def _get_recent_events(db_path: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Obtiene los eventos más recientes."""
    limit = min(limit, 500)
    offset = max(offset, 0)
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return _rows_to_dicts(reversed(rows))


def _get_active_sessions(db_path: str, hours: int = 4, limit: int = 50) -> list[dict[str, Any]]:
    """Lista sesiones con actividad en las últimas N horas."""
    limit = min(limit, 200)
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT provider, project, session_id,
            COUNT(*) AS events,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.input'), 0) ELSE 0 END) AS input_tokens,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.output'), 0) ELSE 0 END) AS output_tokens,
            SUM(CASE WHEN tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls,
            GROUP_CONCAT(DISTINCT model) AS models,
            MAX(timestamp) AS last_event,
            MIN(timestamp) AS first_event
        FROM events
        WHERE timestamp >= datetime('now', '-' || ? || ' hours')
        GROUP BY provider, project, session_id
        ORDER BY last_event DESC
        LIMIT ?
    """, (hours, limit)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def _get_token_usage(
    db_path: str,
    provider: Optional[str] = None,
    since: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Consumo de tokens agrupado por provider."""
    where = "1=1"
    params: list = []
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    if since:
        where += " AND timestamp >= ?"
        params.append(since)

    conn = _connect(db_path)
    rows = conn.execute(f"""
        SELECT provider,
            COUNT(*) AS events,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.input'), 0) ELSE 0 END) AS input_tokens,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.output'), 0) ELSE 0 END) AS output_tokens,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.cached_input'), 0)
                   + COALESCE(json_extract(tokens_json, '$.cache_read'), 0) ELSE 0 END) AS cached_tokens
        FROM events WHERE {where}
        GROUP BY provider ORDER BY input_tokens DESC
    """, params).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def _get_tool_stats(
    db_path: str,
    project: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Top herramientas usadas por los agentes AI."""
    where = "tool_name IS NOT NULL"
    params: list = []
    if project:
        where += " AND project LIKE ?"
        params.append(f"%{project}%")
    if since:
        where += " AND timestamp >= ?"
        params.append(since)

    conn = _connect(db_path)
    rows = conn.execute(f"""
        SELECT tool_name, COUNT(*) AS count,
            COUNT(DISTINCT project) AS projects
        FROM events WHERE {where}
        GROUP BY tool_name ORDER BY count DESC LIMIT ?
    """, (*params, limit)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def _search_events(
    db_path: str,
    query: str,
    provider: Optional[str] = None,
    project: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Busca eventos por texto en el summary."""
    limit = min(limit, 200)
    offset = max(offset, 0)
    where = "summary LIKE ?"
    params: list = [f"%{query}%"]
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    if project:
        where += " AND project LIKE ?"
        params.append(f"%{project}%")
    if event_type:
        where += " AND event_type = ?"
        params.append(event_type)

    conn = _connect(db_path)
    rows = conn.execute(f"""
        SELECT id, provider, project, event_type, timestamp, summary,
               session_id, tool_name, file_path, model
        FROM events WHERE {where}
        ORDER BY id DESC LIMIT ? OFFSET ?
    """, (*params, limit, offset)).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


def _get_project_activity(
    db_path: str,
    project: str,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """Resumen completo de actividad de un proyecto específico."""
    where = "project LIKE ?"
    params: list = [f"%{project}%"]
    if since:
        where += " AND timestamp >= ?"
        params.append(since)

    conn = _connect(db_path)

    stats = conn.execute(f"""
        SELECT COUNT(*) AS events,
            COUNT(DISTINCT session_id) AS sessions,
            COUNT(DISTINCT provider) AS providers,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.input'), 0) ELSE 0 END) AS input_tokens,
            SUM(CASE WHEN tokens_json IS NOT NULL
                THEN COALESCE(json_extract(tokens_json, '$.output'), 0) ELSE 0 END) AS output_tokens,
            MIN(timestamp) AS first_event,
            MAX(timestamp) AS last_event
        FROM events WHERE {where}
    """, params).fetchone()

    tools = conn.execute(f"""
        SELECT tool_name, COUNT(*) AS count
        FROM events WHERE {where} AND tool_name IS NOT NULL
        GROUP BY tool_name ORDER BY count DESC LIMIT 10
    """, params).fetchall()

    models = conn.execute(f"""
        SELECT DISTINCT model FROM events
        WHERE {where} AND model IS NOT NULL
    """, params).fetchall()

    conn.close()

    return {
        **dict(stats),
        "top_tools": _rows_to_dicts(tools),
        "models": [m["model"] for m in models],
    }


def _get_sessions(
    db_path: str,
    hours: int = 24,
    provider: Optional[str] = None,
    branch: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query sessions table with optional filters."""
    limit = min(limit, 500)
    conn = _connect(db_path)
    where_parts = ["1=1"]
    params: list = []
    if hours:
        where_parts.append("s.last_event_at >= datetime('now', '-' || ? || ' hours')")
        params.append(hours)
    if provider:
        where_parts.append("s.provider = ?")
        params.append(provider)
    if branch:
        where_parts.append("s.git_branch = ?")
        params.append(branch)
    where = " AND ".join(where_parts)
    try:
        rows = conn.execute(f"""
            SELECT s.id, s.provider, s.project, s.title, s.cwd,
                   s.git_branch, s.model, s.cli_version, s.source,
                   s.cost, s.is_sidechain, s.first_event_at, s.last_event_at,
                   (SELECT COUNT(*) FROM events e
                    WHERE e.session_id = s.id AND e.provider = s.provider) AS event_count,
                   s.is_active
            FROM sessions s
            WHERE {where}
            ORDER BY s.last_event_at DESC
            LIMIT ?
        """, params + [limit]).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def _get_session_detail(db_path: str, session_id: str) -> dict[str, Any] | None:
    """Get detailed info for a single session by ID."""
    conn = _connect(db_path)
    try:
        row = conn.execute("""
            SELECT s.id, s.provider, s.project, s.title, s.cwd,
                   s.git_branch, s.model, s.cli_version, s.source,
                   s.cost, s.is_sidechain, s.first_event_at, s.last_event_at,
                   (SELECT COUNT(*) FROM events e
                    WHERE e.session_id = s.id AND e.provider = s.provider) AS event_count,
                   s.is_active, s.initial_prompt, s.metadata_json,
                   s.ended_at, s.ended_reason
            FROM sessions s WHERE s.id = ?
        """, (session_id,)).fetchone()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("metadata_json"):
        try:
            d["metadata"] = json.loads(d.pop("metadata_json"))
        except (json.JSONDecodeError, TypeError):
            d.pop("metadata_json", None)
    else:
        d.pop("metadata_json", None)
    chain = _get_session_chain(db_path, session_id)
    if chain:
        d["linked_sessions"] = chain
    return d


def _get_session_events(
    db_path: str, session_id: str, text_mode: str = "none",
    limit: int = 100, offset: int = 0, order: str = "asc",
) -> list[dict[str, Any]]:
    """text_mode: 'none' (summary only), 'snippet' (500 chars), 'full' (complete text).
    order: 'asc' (oldest first) or 'desc' (newest first).
    offset: skip N events for pagination."""
    limit = min(limit, 500)
    offset = max(offset, 0)
    direction = "DESC" if order.lower() == "desc" else "ASC"
    conn = _connect(db_path)
    if text_mode in ("snippet", "full"):
        rows = conn.execute(f"""
            SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                   e.summary, e.session_id, e.tokens_json, e.tool_name,
                   e.file_path, e.model, e.cwd, ec.full_text
            FROM events e
            LEFT JOIN event_content ec ON e.id = ec.event_id
            WHERE e.session_id = ?
            ORDER BY e.timestamp {direction} LIMIT ? OFFSET ?
        """, (session_id, limit, offset)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                   e.summary, e.session_id, e.tokens_json, e.tool_name,
                   e.file_path, e.model, e.cwd, NULL as full_text
            FROM events e
            WHERE e.session_id = ?
            ORDER BY e.timestamp {direction} LIMIT ? OFFSET ?
        """, (session_id, limit, offset)).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        if d.get("tokens_json"):
            try:
                d["tokens"] = json.loads(d.pop("tokens_json"))
            except (json.JSONDecodeError, TypeError):
                d.pop("tokens_json", None)
        else:
            d.pop("tokens_json", None)
        ft = d.get("full_text")
        if ft and text_mode == "snippet" and len(ft) > 500:
            d["full_text"] = ft[:500] + " [truncated]"
        elif not ft:
            d.pop("full_text", None)
        results.append(d)
    return results


def _search_session_content(
    db_path: str,
    query: str,
    provider: str | None = None,
    project: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = min(limit, 200)
    conn = _connect(db_path)
    where_parts = ["ec.full_text LIKE ?"]
    params: list = [f"%{query}%"]
    if provider:
        where_parts.append("e.provider = ?")
        params.append(provider)
    if project:
        where_parts.append("e.project LIKE ?")
        params.append(f"%{project}%")
    where = " AND ".join(where_parts)
    rows = conn.execute(f"""
        SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
               e.summary, e.session_id, e.tool_name, e.model,
               SUBSTR(ec.full_text, MAX(1, INSTR(ec.full_text, ?) - 100), 300) as context
        FROM events e
        JOIN event_content ec ON e.id = ec.event_id
        WHERE {where}
        ORDER BY e.timestamp DESC LIMIT ?
    """, [query] + params + [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_session_chain(db_path: str, session_id: str) -> list[dict[str, Any]]:
    """Get linked sessions (predecessors and successors)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("""
            SELECT
                sl.source_session, sl.source_provider,
                sl.target_session, sl.target_provider,
                sl.link_type, sl.confidence, sl.created_at,
                CASE WHEN sl.source_session = ? THEN 'successor' ELSE 'predecessor' END AS direction,
                s.title, s.model, s.project, s.first_event_at, s.last_event_at,
                (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id) AS event_count
            FROM session_links sl
            LEFT JOIN sessions s ON (
                CASE WHEN sl.source_session = ?
                    THEN s.id = sl.target_session AND s.provider = sl.target_provider
                    ELSE s.id = sl.source_session AND s.provider = sl.source_provider
                END
            )
            WHERE sl.source_session = ? OR sl.target_session = ?
            ORDER BY sl.created_at ASC
        """, (session_id, session_id, session_id, session_id)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    results = []
    for r in rows:
        linked_id = r["target_session"] if r["source_session"] == session_id else r["source_session"]
        linked_provider = r["target_provider"] if r["source_session"] == session_id else r["source_provider"]
        results.append({
            "session_id": linked_id,
            "provider": linked_provider,
            "direction": r["direction"],
            "link_type": r["link_type"],
            "confidence": r["confidence"],
            "title": r["title"] or "",
            "model": r["model"] or "",
            "project": r["project"] or "",
            "first_event_at": r["first_event_at"] or "",
            "last_event_at": r["last_event_at"] or "",
            "event_count": r["event_count"] or 0,
        })
    return results


def _get_branch_sessions(
    db_path: str, branch: str, hours: int = 168, limit: int = 50
) -> list[dict[str, Any]]:
    """Get sessions associated with a specific git branch."""
    return _get_sessions(db_path, hours=hours, branch=branch, limit=limit)


# ── Workspace attribution (issue #20 — workspace.db, read-only) ─────

def _hide_project_names() -> bool:
    """Read the ``hide_project_names`` privacy flag (lazy — mcp is config-free).

    ``mcp_server`` is otherwise a thin read-only SQL layer that never imports
    ``hub.config``; this is the one place privacy requires it. Any failure
    (missing config, import error) defaults to *not hiding* — the flag is
    opt-in, and a broken read must not silently expose or hide inconsistently.
    """
    try:
        from hub.config import load_config
        return load_config().hide_project_names
    except Exception:
        return False


def _mask_workspace_rows(rows: list[dict[str, Any]], hide: bool) -> list[dict[str, Any]]:
    """Mask the human display fields of workspace rows when ``hide`` is set.

    Hashes remote_url/root_path/dir_path into a stable ``label`` and blanks the
    raw names, but NEVER touches ``workspace_key`` — it is the join handle the
    other tools resolve against.
    """
    from hub.config import masked_label
    out = []
    for r in rows:
        label = r.get("remote_url") or r.get("root_path") or r.get("dir_path") or r.get("workspace_key", "")
        r = dict(r)
        r["label"] = masked_label(label, hide)
        if hide:
            r["remote_url"] = None
            r["root_path"] = None
            r["dir_path"] = None
        out.append(r)
    return out


def _get_session_workspaces(
    db_path: str, session_id: str, provider: str | None = None
) -> list[dict[str, Any]]:
    """Workspaces (owning projects) a session touched, via path attribution.

    Returns ``[]`` when workspace.db does not exist yet (before the first
    ``mool workspace backfill`` run).
    """
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    try:
        where = "a.session_id = ?"
        params: list = [session_id]
        if provider:
            where += " AND a.provider = ?"
            params.append(provider)
        rows = conn.execute(f"""
            SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                   a.provider, COUNT(DISTINCT a.file_path) AS files
            FROM path_attributions a
            JOIN workspaces w ON w.id = a.workspace_id
            WHERE {where}
            GROUP BY w.id, a.provider
            ORDER BY files DESC
        """, params).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    return _mask_workspace_rows([dict(r) for r in rows], _hide_project_names())


def _get_workspace_sessions(db_path: str, workspace_key: str) -> list[dict[str, Any]]:
    """Sessions that touched a workspace. ``[]`` when workspace.db is absent."""
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT a.session_id, a.provider, COUNT(DISTINCT a.file_path) AS files
            FROM path_attributions a
            JOIN workspaces w ON w.id = a.workspace_id
            WHERE w.workspace_key = ?
            GROUP BY a.session_id, a.provider
            ORDER BY files DESC
        """, (workspace_key,)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def _list_workspaces(db_path: str) -> list[dict[str, Any]]:
    """All known workspaces with session/attribution/touch counts. ``[]`` if absent."""
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    try:
        # Touch count via correlated subquery, not a second LEFT JOIN, so it
        # never multiplies rows and inflates COUNT(a.id) (issue #21).
        rows = conn.execute("""
            SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                   w.first_seen, COUNT(a.id) AS attributions,
                   COUNT(DISTINCT a.session_id || '/' || a.provider) AS sessions,
                   (SELECT COUNT(*) FROM path_touches t
                    WHERE t.workspace_id = w.id) AS touches
            FROM workspaces w
            LEFT JOIN path_attributions a ON a.workspace_id = w.id
            GROUP BY w.id
            ORDER BY sessions DESC, attributions DESC, touches DESC
        """).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    return _mask_workspace_rows([dict(r) for r in rows], _hide_project_names())


def _get_workspace_touches(db_path: str, workspace_key: str) -> list[dict[str, Any]]:
    """Filesystem path-touches attributed to a workspace (issue #21).

    Returns ``[]`` when workspace.db or the ``path_touches`` table is absent
    (before the first filesystem-watcher cycle). Paths are masked when
    ``hide_project_names`` is set.
    """
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT t.path, t.mtime, t.source, t.resolved_via, t.last_seen
            FROM path_touches t
            JOIN workspaces w ON w.id = t.workspace_id
            WHERE w.workspace_key = ?
            ORDER BY t.mtime DESC
        """, (workspace_key,)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    result = [dict(r) for r in rows]
    if _hide_project_names():
        from hub.config import masked_label
        for r in result:
            r["path"] = masked_label(r["path"], True)
    return result


def _lit_sources(session_n: int, fs_n: int, git_n: int) -> list[str]:
    """Signals that lit a node (session / filesystem / git). Honest presence,
    not a summed total — the three counts are incommensurable units."""
    lit = []
    if session_n:
        lit.append("session")
    if fs_n:
        lit.append("filesystem")
    if git_n:
        lit.append("git")
    return lit


def _get_portfolio(
    db_path: str, since: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Machine-wide portfolio: the hot workspaces from the materialized rollup.

    Signal-agnostic — a node aggregates session, filesystem and git activity.
    Reads only ``workspace_rollup`` (git was folded in at build time), so this
    stays a single-table query. ``since`` filters by plain string compare on the
    ISO ``day`` (never ``date()`` — that reintroduces the ISO/epoch hazard).
    Returns ``[]`` when workspace.db or the rollup is absent (never built).
    Masked when ``hide_project_names`` is set.
    """
    limit = min(max(limit, 1), 500)
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    where = ""
    params: list = []
    if since:
        where = "WHERE r.day >= ?"
        params.append(since[:10])
    try:
        rows = conn.execute(f"""
            SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                   SUM(r.session_touches) AS session_touches,
                   SUM(r.fs_touches) AS fs_touches,
                   SUM(r.git_touches) AS git_touches,
                   COUNT(*) AS active_days,
                   MAX(r.last_activity) AS last_activity
            FROM workspace_rollup r
            JOIN workspaces w ON w.id = r.workspace_id
            {where}
            GROUP BY w.id
            ORDER BY last_activity DESC
            LIMIT ?
        """, params + [limit]).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = _lit_sources(
            d["session_touches"] or 0, d["fs_touches"] or 0, d["git_touches"] or 0
        )
        out.append(d)
    return _mask_workspace_rows(out, _hide_project_names())


def _get_workspace_activity(
    db_path: str, workspace_key: str, since: str | None = None
) -> list[dict[str, Any]]:
    """Per-day activity for one workspace from the rollup, newest day first.

    Signal-agnostic per-day breakdown (session / filesystem / git). Returns
    ``[]`` when workspace.db or the rollup is absent. The ``workspace_key`` is
    the (unmasked) join handle; the per-day rows carry no name fields to mask.
    """
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    where = "WHERE w.workspace_key = ?"
    params: list = [workspace_key]
    if since:
        where += " AND r.day >= ?"
        params.append(since[:10])
    try:
        rows = conn.execute(f"""
            SELECT r.day, r.session_touches, r.fs_touches, r.git_touches,
                   r.last_activity
            FROM workspace_rollup r
            JOIN workspaces w ON w.id = r.workspace_id
            {where}
            ORDER BY r.day DESC
        """, params).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = _lit_sources(
            d["session_touches"] or 0, d["fs_touches"] or 0, d["git_touches"] or 0
        )
        out.append(d)
    return out


def _get_delivery_candidates(db_path: str, limit: int = 100) -> list[dict[str, Any]]:
    """Delivery candidates from the rollup detector (issue #22).

    A candidate WITH CONFIDENCE, never a fact: each row carries the firing second
    signal (session_close | git_commit | root_artifact) and its detail, plus the
    real ``quiescent_since``. Returns ``[]`` when workspace.db or the table is
    absent. Masked when ``hide_project_names`` is set — including
    ``signal_detail`` for ``root_artifact`` (a filesystem path); session_id and
    commit sha are not project names and pass through.
    """
    limit = min(max(limit, 1), 500)
    conn = _connect_optional(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT w.workspace_key, w.kind, w.remote_url, w.root_path, w.dir_path,
                   d.signal, d.signal_detail, d.quiescent_since, d.confidence,
                   d.detected_at
            FROM delivery_candidates d
            JOIN workspaces w ON w.id = d.workspace_id
            ORDER BY d.confidence DESC, d.quiescent_since DESC
            LIMIT ?
        """, (limit,)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    hide = _hide_project_names()
    out = [dict(r) for r in rows]
    out = _mask_workspace_rows(out, hide)
    if hide:
        from hub.config import masked_label
        for r in out:
            if r.get("signal") == "root_artifact" and r.get("signal_detail"):
                r["signal_detail"] = masked_label(r["signal_detail"], True)
    return out


# ── MCP layer (guarded — only loads when mcp SDK is available) ──────

try:
    from mcp.server.fastmcp import FastMCP
    _mcp = FastMCP("moolmesh")
except ImportError:
    _mcp = None

if _mcp is not None:

    @_mcp.resource("hub://schema")
    def get_schema() -> str:
        """Schema de la base de datos events.db — columnas, tipos e índices."""
        return _get_schema()

    @_mcp.resource("hub://projects")
    def get_projects_resource() -> str:
        """Lista de todos los proyectos con estadísticas: provider, eventos, tokens, última actividad."""
        return _get_projects_resource(EVENTS_DB)

    @_mcp.tool()
    def get_recent_events(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Obtiene los eventos más recientes del MoolMesh.
        Útil para ver en qué está trabajando el usuario actualmente.

        Args:
            limit: Máximo de eventos a devolver (max 500, default 50)
            offset: Saltar N eventos para paginación (default 0). Los eventos se devuelven del más reciente al más antiguo.
        """
        return _get_recent_events(EVENTS_DB, limit, offset)

    @_mcp.tool()
    def get_active_sessions(hours: int = 4, limit: int = 50) -> list[dict[str, Any]]:
        """Lista las sesiones con actividad en las últimas N horas.
        Cada sesión muestra: provider, proyecto, eventos, tokens, modelos, última actividad.

        Args:
            hours: Ventana de tiempo en horas (default 4)
            limit: Máximo de sesiones a devolver (max 200, default 50)
        """
        return _get_active_sessions(EVENTS_DB, hours, limit)

    @_mcp.tool()
    def get_token_usage(
        provider: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Consumo de tokens agrupado por provider.
        Devuelve input_tokens, output_tokens y cached_tokens por provider.

        Args:
            provider: Filtrar por provider (claude, codex, qwen, opencode). None = todos.
            since: Fecha ISO 8601 desde la cual contar (e.g. "2026-06-22"). None = todo el historial.
        """
        return _get_token_usage(EVENTS_DB, provider, since)

    @_mcp.tool()
    def get_tool_stats(
        project: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Top herramientas usadas por los agentes AI (Read, Edit, Bash, Write, etc.).

        Args:
            project: Filtrar por proyecto (substring match). None = todos.
            since: Fecha ISO 8601 desde. None = todo.
            limit: Máximo de herramientas a devolver (default 20).
        """
        return _get_tool_stats(EVENTS_DB, project, since, limit)

    @_mcp.tool()
    def search_events(
        query: str,
        provider: Optional[str] = None,
        project: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Busca eventos por texto en el summary (mensajes, herramientas, etc.).

        Args:
            query: Texto a buscar en el campo summary.
            provider: Filtrar por provider. None = todos.
            project: Filtrar por proyecto (substring). None = todos.
            event_type: Filtrar por tipo (user, assistant, tool_use, etc.). None = todos.
            limit: Máximo de resultados (max 200, default 50).
            offset: Saltar N resultados para paginación (default 0).
        """
        return _search_events(EVENTS_DB, query, provider, project, event_type, limit, offset)

    @_mcp.tool()
    def get_project_activity(
        project: str,
        since: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resumen completo de actividad de un proyecto específico.
        Incluye: eventos totales, tokens, herramientas más usadas, modelos, sesiones.

        Args:
            project: Nombre del proyecto (substring match).
            since: Fecha ISO 8601 desde. None = todo el historial.
        """
        return _get_project_activity(EVENTS_DB, project, since)

    @_mcp.tool()
    def get_sessions(
        hours: int = 24,
        provider: Optional[str] = None,
        branch: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lista sesiones con metadata enriquecida (título, branch, modelo, cwd).
        Usa la tabla sessions para datos que no están en events.

        Args:
            hours: Ventana de tiempo en horas (default 24).
            provider: Filtrar por provider. None = todos.
            branch: Filtrar por git branch. None = todos.
            limit: Máximo de sesiones a devolver (max 500, default 50).
        """
        return _get_sessions(EVENTS_DB, hours, provider, branch, limit)

    @_mcp.tool()
    def get_session_detail(session_id: str) -> dict[str, Any]:
        """Detalle completo de una sesión específica por ID.
        Incluye metadata, prompt inicial, branch, modelo, eventos.

        Semántica de `is_active`: `false` significa que se observó el fin de la
        sesión (una señal terminal en el archivo, hoy solo el `/exit` de Claude),
        con `ended_at`/`ended_reason` registrando cuándo y por qué. `true`
        significa únicamente que aún no se observó un fin — NO que la sesión esté
        viva. Nunca se infiere de la recencia de eventos.

        Args:
            session_id: ID de la sesión.
        """
        result = _get_session_detail(EVENTS_DB, session_id)
        return result or {"error": f"Session {session_id} not found"}

    @_mcp.tool()
    def get_session_events(
        session_id: str,
        text_mode: str = "none",
        limit: int = 100,
        offset: int = 0,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        """Obtiene los eventos de una sesión con control de volumen de texto.

        Args:
            session_id: ID de la sesión.
            text_mode: Control de texto devuelto. 'none' = solo summary (~120 chars). 'snippet' = texto completo truncado a 500 chars. 'full' = texto completo sin truncar (alto consumo de tokens).
            limit: Máximo de eventos a devolver (max 500, default 100).
            offset: Saltar N eventos para paginación (default 0). Ejemplo: offset=100 con limit=50 devuelve eventos 101-150.
            order: Orden de eventos. 'asc' = más antiguos primero (default). 'desc' = más recientes primero (útil para ver la actividad reciente de una sesión larga).
        """
        return _get_session_events(EVENTS_DB, session_id, text_mode, limit, offset, order)

    @_mcp.tool()
    def search_session_content(
        query: str,
        provider: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Búsqueda de texto completo en el contenido de las sesiones.
        Busca en el texto completo (no truncado) de los eventos almacenados.

        Args:
            query: Texto a buscar en el contenido completo.
            provider: Filtrar por provider. None = todos.
            project: Filtrar por proyecto (substring). None = todos.
            limit: Máximo de resultados (max 200, default 50).
        """
        return _search_session_content(EVENTS_DB, query, provider, project, limit)

    @_mcp.tool()
    def get_branch_sessions(branch: str, hours: int = 168) -> list[dict[str, Any]]:
        """Sesiones correlacionadas con un branch de git específico.
        Útil para ver qué sesiones de agentes AI trabajaron en un branch.

        Args:
            branch: Nombre del branch (exact match).
            hours: Ventana de tiempo en horas (default 168 = 7 días).
        """
        return _get_branch_sessions(EVENTS_DB, branch, hours)

    @_mcp.tool()
    def get_session_chain(session_id: str) -> list[dict[str, Any]]:
        """Sesiones vinculadas (predecesoras y sucesoras) a una sesión dada.
        Muestra la cadena de trabajo entre sesiones de diferentes proveedores.

        Args:
            session_id: ID de la sesión.
        """
        return _get_session_chain(EVENTS_DB, session_id)

    @_mcp.tool()
    def get_session_workspaces(
        session_id: str,
        provider: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Workspaces (proyectos dueños) que tocó una sesión.

        Atribuye cada archivo que la sesión tocó al proyecto que lo posee — no
        al nombre del directorio de la sesión — vía la escalera de identidad
        git-remote → git-root → path-hash. Una sesión puede tocar varios
        workspaces (relación M:N). Devuelve `[]` si aún no se corrió
        `mool workspace backfill`.

        Args:
            session_id: ID de la sesión.
            provider: Filtrar por provider (claude, codex, ...). None = todos.
        """
        return _get_session_workspaces(WORKSPACE_DB, session_id, provider)

    @_mcp.tool()
    def get_workspace_sessions(workspace_key: str) -> list[dict[str, Any]]:
        """Sesiones que tocaron un workspace dado (relación M:N inversa).

        Args:
            workspace_key: Clave del workspace (git_remote:host/owner/repo,
                git_root:<ruta> o path_hash:<hash>). Ver `list_workspaces`.
        """
        return _get_workspace_sessions(WORKSPACE_DB, workspace_key)

    @_mcp.tool()
    def list_workspaces() -> list[dict[str, Any]]:
        """Lista todos los workspaces conocidos con conteo de sesiones, archivos
        y touches de filesystem (`touches`).
        Devuelve `[]` si aún no se corrió `mool workspace backfill`.
        """
        return _list_workspaces(WORKSPACE_DB)

    @_mcp.tool()
    def get_workspace_touches(workspace_key: str) -> list[dict[str, Any]]:
        """Touches de filesystem atribuidos a un workspace (issue #21, Fase B).

        Hace visible un proyecto aunque ningún agente ni git lo hayan tocado:
        el filesystem watcher observa raíces marcadas (opt-in) y emite un
        path-touch por archivo que cambió, resuelto al workspace dueño. Devuelve
        `[]` si aún no corrió el watcher. Con `hide_project_names` activo, los
        paths se enmascaran.

        Args:
            workspace_key: Clave del workspace (ver `list_workspaces`).
        """
        return _get_workspace_touches(WORKSPACE_DB, workspace_key)

    @_mcp.tool()
    def get_portfolio(
        since: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Portfolio machine-wide: los workspaces calientes del rollup (issue #22).

        Proyección signal-agnostic sobre el árbol de workspaces: un nodo se
        enciende venga la actividad de una sesión, del filesystem o de git — cada
        fila reporta `sources` (qué señales lo encendieron) y los conteos por
        señal por separado (unidades distintas, nunca sumadas). Ordena por
        `last_activity` (lo más recientemente tocado primero). Devuelve `[]` si
        aún no se construyó el rollup (`mool workspace rollup`). Con
        `hide_project_names` activo, los nombres se enmascaran.

        Args:
            since: Fecha ISO 8601 desde (compara por día). None = todo.
            limit: Máximo de workspaces (max 500, default 100).
        """
        return _get_portfolio(WORKSPACE_DB, since, limit)

    @_mcp.tool()
    def get_workspace_activity(
        workspace_key: str,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Actividad por día de un workspace desde el rollup (issue #22).

        Desglose signal-agnostic por día (session / filesystem / git) con
        `sources` por fila. Devuelve `[]` si el rollup no existe.

        Args:
            workspace_key: Clave del workspace (ver `list_workspaces`).
            since: Fecha ISO 8601 desde (compara por día). None = todo.
        """
        return _get_workspace_activity(WORKSPACE_DB, workspace_key, since)

    @_mcp.tool()
    def get_delivery_candidates(limit: int = 100) -> list[dict[str, Any]]:
        """Candidatos de entrega de trabajo, detectados localmente (issue #22).

        Candidato CON CONFIANZA, nunca un hecho: cada fila registra cuál de las
        tres señales admisibles cerró el burst (`session_close` vía ended_at de
        #16 / `git_commit` / `root_artifact`) en `signal` + `signal_detail`, más
        `quiescent_since` (última actividad real). La quiescencia sola NUNCA
        emite — es precondición. Sin LLM, señal estructural local. Devuelve `[]`
        si aún no se corrió el detector. Con `hide_project_names`, los nombres y
        el path de `root_artifact` se enmascaran.

        Args:
            limit: Máximo de candidatos (max 500, default 100).
        """
        return _get_delivery_candidates(WORKSPACE_DB, limit)


if __name__ == "__main__":
    if _mcp is None:
        print("Error: mcp package not installed. Run with: uv run hub/mcp_server.py", file=sys.stderr)
        sys.exit(1)
    print("MoolMesh MCP Server starting...", file=sys.stderr)
    _mcp.run(transport="stdio")
