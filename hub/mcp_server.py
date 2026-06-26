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


# ── Helpers ─────────────────────────────────────────────────────────
def _connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


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


def _get_recent_events(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    """Obtiene los eventos más recientes."""
    limit = min(limit, 500)
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return _rows_to_dicts(reversed(rows))


def _get_active_sessions(db_path: str, hours: int = 4) -> list[dict[str, Any]]:
    """Lista sesiones con actividad en las últimas N horas."""
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
    """, (hours,)).fetchall()
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
) -> list[dict[str, Any]]:
    """Busca eventos por texto en el summary."""
    limit = min(limit, 200)
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
        ORDER BY id DESC LIMIT ?
    """, (*params, limit)).fetchall()
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
) -> list[dict[str, Any]]:
    """Query sessions table with optional filters."""
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
        """, params).fetchall()
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
                   s.is_active, s.initial_prompt, s.metadata_json
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
    db_path: str, session_id: str, include_full_text: bool = False, limit: int = 200
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    if include_full_text:
        rows = conn.execute("""
            SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                   e.summary, e.session_id, e.tokens_json, e.tool_name,
                   e.file_path, e.model, e.cwd, ec.full_text
            FROM events e
            LEFT JOIN event_content ec ON e.id = ec.event_id
            WHERE e.session_id = ?
            ORDER BY e.timestamp ASC LIMIT ?
        """, (session_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT e.id, e.provider, e.project, e.event_type, e.timestamp,
                   e.summary, e.session_id, e.tokens_json, e.tool_name,
                   e.file_path, e.model, e.cwd, NULL as full_text
            FROM events e
            WHERE e.session_id = ?
            ORDER BY e.timestamp ASC LIMIT ?
        """, (session_id, limit)).fetchall()
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
        if not d.get("full_text"):
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
    db_path: str, branch: str, hours: int = 168
) -> list[dict[str, Any]]:
    """Get sessions associated with a specific git branch."""
    return _get_sessions(db_path, hours=hours, branch=branch)


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
    def get_recent_events(limit: int = 50) -> list[dict[str, Any]]:
        """Obtiene los eventos más recientes del MoolMesh.
        Útil para ver en qué está trabajando el usuario actualmente.

        Args:
            limit: Máximo de eventos a devolver (max 500, default 50)
        """
        return _get_recent_events(EVENTS_DB, limit)

    @_mcp.tool()
    def get_active_sessions(hours: int = 4) -> list[dict[str, Any]]:
        """Lista las sesiones con actividad en las últimas N horas.
        Cada sesión muestra: provider, proyecto, eventos, tokens, modelos, última actividad.

        Args:
            hours: Ventana de tiempo en horas (default 4)
        """
        return _get_active_sessions(EVENTS_DB, hours)

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
    ) -> list[dict[str, Any]]:
        """Busca eventos por texto en el summary (mensajes, herramientas, etc.).

        Args:
            query: Texto a buscar en el campo summary.
            provider: Filtrar por provider. None = todos.
            project: Filtrar por proyecto (substring). None = todos.
            event_type: Filtrar por tipo (user, assistant, tool_use, etc.). None = todos.
            limit: Máximo de resultados (max 200, default 50).
        """
        return _search_events(EVENTS_DB, query, provider, project, event_type, limit)

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
    ) -> list[dict[str, Any]]:
        """Lista sesiones con metadata enriquecida (título, branch, modelo, cwd).
        Usa la tabla sessions para datos que no están en events.

        Args:
            hours: Ventana de tiempo en horas (default 24).
            provider: Filtrar por provider. None = todos.
            branch: Filtrar por git branch. None = todos.
        """
        return _get_sessions(EVENTS_DB, hours, provider, branch)

    @_mcp.tool()
    def get_session_detail(session_id: str) -> dict[str, Any]:
        """Detalle completo de una sesión específica por ID.
        Incluye metadata, prompt inicial, branch, modelo, eventos.

        Args:
            session_id: ID de la sesión.
        """
        result = _get_session_detail(EVENTS_DB, session_id)
        return result or {"error": f"Session {session_id} not found"}

    @_mcp.tool()
    def get_session_events(
        session_id: str,
        include_full_text: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Obtiene todos los eventos de una sesión, opcionalmente con texto completo.
        Útil para exportar transcripts o analizar una sesión en detalle.

        Args:
            session_id: ID de la sesión.
            include_full_text: Si True, incluye el texto completo (no truncado) de cada evento.
            limit: Máximo de eventos a devolver (default 200).
        """
        return _get_session_events(EVENTS_DB, session_id, include_full_text, limit)

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


if __name__ == "__main__":
    if _mcp is None:
        print("Error: mcp package not installed. Run with: uv run hub/mcp_server.py", file=sys.stderr)
        sys.exit(1)
    print("MoolMesh MCP Server starting...", file=sys.stderr)
    _mcp.run(transport="stdio")
