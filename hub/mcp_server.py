# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.2.0,<2",
# ]
# ///
"""MoolMesh MCP Server — read-only access to AI agent session data."""

from __future__ import annotations

import atexit
import functools
import importlib
import importlib.metadata
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Default ``tz`` of the portfolio: the ``[user] timezone`` zone (#81).
from hub.localtime import CONFIGURED as _CONFIGURED_TZ

_log = logging.getLogger("hub.mcp_server")

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


# ── Stale-server detection (#70) ────────────────────────────────────
# An agent session keeps its MCP server alive until it closes, so after
# `pipx upgrade` it keeps serving the code it started with — silently. Each
# process remembers what it started as, re-reads what is installed on every
# call, and says so when they differ. Servers <= 1.26.1 predate this and
# cannot be fixed retroactively: only a restart replaces them.
try:
    from hub import __version__ as RUNNING_VERSION
except ImportError:  # run outside the package: nothing to compare against
    RUNNING_VERSION = "dev"
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
STALE_HINT = (
    "This MoolMesh MCP server is running old code. Restart this agent session "
    "(or its MCP server) to load the installed version."
)
_SERVER_INSTRUCTIONS = (
    "If results look inconsistent, call get_server_info: "
    "a stale server must be restarted. "
    "If a result carries `stale: true` (see its `freshness`), its numbers are "
    "not current: the MoolMesh daemon is down or its sync is old — say so "
    "instead of presenting them as today's."
)
_stale_logged = False


def _installed_version() -> str | None:
    """Version of moolmesh on disk right now, or None when it can't be verified.

    Re-read on every call, never cached. ``importlib.metadata`` reads METADATA
    lazily and already refreshes its directory listing when the directory mtime
    changes (so a running process sees an upgrade's new ``dist-info`` without
    restarting — covered by a test); ``invalidate_caches()`` additionally closes
    the corner where an upgrade lands within the filesystem's timestamp
    granularity of the last lookup. Never raises: this runs inside tool calls,
    and a half-finished upgrade can make the lookup fail.
    """
    try:
        importlib.invalidate_caches()
        return importlib.metadata.version("moolmesh") or None
    except Exception:  # noqa: BLE001 — contract: unverifiable → None, never an error
        _log.debug("could not read the installed moolmesh version", exc_info=True)
        return None


def _stale_from(installed: str | None) -> dict[str, str] | None:
    """Staleness for a given installed version; warns once per process."""
    global _stale_logged
    if installed is None or RUNNING_VERSION == "dev" or installed == RUNNING_VERSION:
        return None
    if not _stale_logged:
        _stale_logged = True
        _log.warning(
            "MoolMesh MCP server is running v%s but v%s is installed — "
            "restart this agent session to load the new code",
            RUNNING_VERSION, installed,
        )
    return {
        "running": RUNNING_VERSION,
        "installed": installed,
        "started_at": STARTED_AT,
        "hint": STALE_HINT,
    }


def _staleness() -> dict[str, str] | None:
    """``None`` when versions match or the installed one can't be verified."""
    return _stale_from(_installed_version())


def _get_server_info() -> dict[str, Any]:
    installed = _installed_version()
    stale = _stale_from(installed)
    info: dict[str, Any] = {
        "running_version": RUNNING_VERSION,
        "installed_version": installed,
        "stale": stale is not None,
        "started_at": STARTED_AT,
        "pid": os.getpid(),
        # Data freshness (#76) — distinct from `stale` above, which is about
        # this server's code, not the data it reads.
        "freshness": _freshness(),
    }
    if stale is not None:
        info["hint"] = STALE_HINT
    return info


# ── Stale-data flag (#76) ───────────────────────────────────────────
# Measured from the data (last git / GitHub API sync), not from "the process
# lives". Cached a few seconds: several tool calls in a row share one read.
FRESHNESS_TTL = 5.0
_freshness_cache: tuple[float, tuple, dict] | None = None


def _freshness() -> dict[str, Any]:
    """``hub.freshness.compute_freshness`` for this server's DBs; never raises."""
    global _freshness_cache
    import time as _time
    key = (EVENTS_DB, GITHUB_DB)
    now = _time.monotonic()
    cached = _freshness_cache
    if cached is not None and cached[1] == key and now - cached[0] < FRESHNESS_TTL:
        return dict(cached[2])
    try:
        from hub.freshness import compute_freshness
        result = compute_freshness(EVENTS_DB, GITHUB_DB)
    except Exception:  # noqa: BLE001 — a freshness failure must not break the tool
        _log.warning("could not compute data freshness", exc_info=True)
        return {"stale": None, "error": "freshness unavailable"}
    _freshness_cache = (now, key, result)
    return dict(result)


def _freshness_aware(fn):
    """Add ``freshness`` (and top-level ``stale: true``) to a dict result (#76).

    For tools that read the portfolio or the GitHub cache. Same shape as
    ``_stale_aware``: applied at the MCP layer only; list-returning tools are
    not wrapped (their type must not change) — ``get_server_info`` covers them.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        fresh = _freshness()
        out = {**result, "freshness": fresh}
        if fresh.get("stale"):
            out["stale"] = True
        return out
    return wrapper


def _stale_aware(fn):
    """Add ``stale_server`` to a dict-returning tool's result when this process is stale.

    Applied at the MCP layer only, so the ``_get_*`` functions (also used by
    ``mool query``) stay untouched. List-returning tools are not wrapped — a
    list can't carry the flag without changing its type; ``get_server_info``
    and the log cover them.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        stale = _staleness()
        if stale is not None and isinstance(result, dict):
            return {**result, "stale_server": stale}
        return result
    return wrapper


def _register_run_file() -> Path | None:
    """Record this server in ``~/.moolmesh/run`` so ``mool doctor`` can list it.

    Called only from the server's ``__main__`` startup — never on import, or
    every CLI command and test that imports this module would register itself.
    A failure is logged and never stops the server from starting.
    """
    try:
        from hub import mcp_registry
        path = mcp_registry.write_run_file(RUNNING_VERSION, STARTED_AT)
    except Exception:  # noqa: BLE001 — the registry is best-effort by contract
        _log.warning("could not write the MCP run file; `mool doctor` will not "
                     "list this server", exc_info=True)
        return None
    atexit.register(mcp_registry.remove_run_file, path)
    return path


# ── Pure functions (testable without mcp SDK) ───────────────────────

def _get_schema() -> str:
    """Schema de la base de datos events.db."""
    return """CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,      -- claude | codex | qwen | opencode | cursor | pi | antigravity
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
    created_at REAL NOT NULL,
    historical INTEGER NOT NULL DEFAULT 0  -- 1 = ingerido por backfill/catch-up/re-parseo (no en vivo)
);
Índices: timestamp, provider, project, session_id, fingerprint (unique partial), id WHERE historical = 0.
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
    # Skip history ingested by backfill/catch-up/re-parse (#45): new ids, old
    # timestamps. Guarded — an events.db not yet migrated has no such column.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    live = "WHERE historical = 0 " if "historical" in cols else ""
    rows = conn.execute(
        f"SELECT * FROM events {live}ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
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
    """Busca eventos por texto en el summary.

    Orden: hora parseada del evento, más reciente primero (#52). ``id`` sigue
    el orden de ingesta, así que tras un backfill lo histórico quedaba arriba;
    ``julianday`` normaliza 'Z' y offsets. Sin hora parseable → al final, con
    lo en vivo antes que lo histórico y luego ``id DESC``.
    """
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
    # Guarded — an events.db not yet migrated has no ``historical`` column.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    hist = "historical ASC, " if "historical" in cols else ""
    rows = conn.execute(f"""
        SELECT id, provider, project, event_type, timestamp, summary,
               session_id, tool_name, file_path, model
        FROM events WHERE {where}
        ORDER BY julianday(timestamp) DESC, {hist}id DESC
        LIMIT ? OFFSET ?
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


def _user_emails() -> list[str]:
    """Owner commit emails from ``[user]`` (issue #36, decisión 1A).

    Lazy like ``_hide_project_names``; any failure (missing config, import
    error) returns ``[]`` — with no emails nothing is ever marked ``team_only``,
    so a broken read degrades to the previous behavior, never a wrong claim.
    """
    try:
        from hub.config import load_config
        return list(load_config().user_emails)
    except Exception:
        return []


def _temporary_containers() -> list[str]:
    """Owner-declared temporary containers from ``[workspace]`` (decisión 3).

    Their direct children render in their own "Temporal" section. Lazy like the
    other config reads; a failure degrades to no sectioning.
    """
    try:
        from hub.config import load_config
        return list(load_config().temporary_containers)
    except Exception:
        return []


def _exclusions():
    """Confidential owners/repos from ``[workspace]`` + registry (issue #95).

    Lazy like the other config reads; a failure degrades to no exclusions
    being known here (the harvesters still never read them).
    """
    from hub.config import Exclusions
    try:
        from hub.config import exclusions_of, load_config
        return exclusions_of(load_config())
    except Exception:
        _log.exception("exclusions unavailable")
        return Exclusions()


def _row_excluded(row: dict[str, Any], excl: Any) -> bool:
    """True when a portfolio row is an excluded repo (key or remote)."""
    return (excl.matches_remote(row.get("project_key"))
            or excl.matches_remote(row.get("_remote_url")))


def _drop_excluded_rows(rows: list[dict[str, Any]], excl: Any) -> list[dict[str, Any]]:
    """Rows minus excluded repos, recursively through ``children`` (#95)."""
    out = []
    for r in rows or []:
        if _row_excluded(r, excl):
            continue
        if r.get("children"):
            r["children"] = _drop_excluded_rows(r["children"], excl)
        out.append(r)
    return out


def _drop_excluded_grouped(grouped: dict[str, Any], excl: Any) -> None:
    """Remove excluded repos from every section of the grouped payload (#95).

    Data already in the stores is not deleted; it is only never shown.
    """
    if not excl:
        return
    for sec in ("projects", "unclassified", "reference", "temporary"):
        if sec in grouped:
            grouped[sec] = _drop_excluded_rows(grouped[sec], excl)
    summary = grouped.get("summary")
    if isinstance(summary, dict) and summary:
        projects = grouped.get("projects", []) + grouped.get("temporary", [])
        summary["projects"] = len(projects)
        summary["children"] = sum(len(p.get("children") or []) for p in projects)
        for sec in ("unclassified", "reference", "temporary"):
            if sec in summary:
                summary[sec] = len(grouped.get(sec, []))


def _registered_remote_keys(github_db: str) -> set[str]:
    """Registered repo identities (``github.com/owner/repo``, lowercased).

    The canonical key of a ``repos`` row in ``github.db`` (read-only). Used to
    tell a registered GitHub project (measurable outcome) from a remote that was
    never registered ("no medido", issue #60) — never a per-person signal.
    Returns ``set()`` when the DB/table is absent.
    """
    if not os.path.exists(github_db):
        return set()
    try:
        conn = sqlite3.connect(f"file:{github_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return set()
    try:
        rows = conn.execute("SELECT owner, repo_name FROM repos").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {
        f"github.com/{owner}/{repo}".lower()
        for owner, repo in rows if owner and repo
    }


def _board_remote_keys(github_db: str) -> set[str]:
    """Identities (``github.com/owner/repo``, lowercased) of board repos (#93).

    Read-only over ``github.db``; ``set()`` when the DB, the table or the
    ``kind`` column (a DB the daemon has not migrated yet) is absent.
    """
    if not os.path.exists(github_db):
        return set()
    try:
        conn = sqlite3.connect(f"file:{github_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return set()
    try:
        rows = conn.execute(
            "SELECT owner, repo_name FROM repos WHERE kind = 'board'").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {
        f"github.com/{owner}/{repo}".lower()
        for owner, repo in rows if owner and repo
    }


def _remote_repo_activity(
    github_db: str, first_day: str, last_day: str, tz: Any,
) -> dict[str, dict[str, str]]:
    """Remote-only repos (#94) with GitHub activity in the window.

    ``{project_key: {"label": "owner/repo", "last_day": day}}`` for every
    ``kind = 'remote'`` repo with an issue or PR created, updated, closed or
    merged on a local day of ``[first_day, last_day]``. Such a repo has no
    clone, hence no session edges: without this it would never appear in the
    portfolio. Read-only; ``{}`` when the DB, table or column is absent.
    """
    if not os.path.exists(github_db):
        return {}
    try:
        conn = sqlite3.connect(f"file:{github_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return {}
    try:
        repos = conn.execute(
            "SELECT id, owner, repo_name FROM repos WHERE kind = 'remote'"
        ).fetchall()
        if not repos:
            return {}
        rows = conn.execute(
            """SELECT i.repo_id, i.created_at, i.updated_at, i.closed_at,
                      i.pr_merged_at
               FROM github_issues i JOIN repos r ON r.id = i.repo_id
               WHERE r.kind = 'remote'"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    from hub import localtime
    last: dict[int, str] = {}
    for rid, *stamps in rows:
        for ts in stamps:
            day = localtime.local_day(ts, tz) if ts else None
            if day and first_day <= day <= last_day and day > last.get(rid, ""):
                last[rid] = day
    out: dict[str, dict[str, str]] = {}
    for rid, owner, name in repos:
        if rid in last and owner and name:
            out[f"git_remote:github.com/{owner}/{name}".lower()] = {
                "label": f"{owner}/{name}", "last_day": last[rid]}
    return out


def _remote_only_keys(github_db: str) -> set[str]:
    """Identities (``github.com/owner/repo``, lowercased) of remote-only repos
    (#94, ``kind = 'remote'``). ``set()`` when absent/unmigrated."""
    if not os.path.exists(github_db):
        return set()
    try:
        conn = sqlite3.connect(f"file:{github_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return set()
    try:
        rows = conn.execute(
            "SELECT owner, repo_name FROM repos WHERE kind = 'remote'").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {
        f"github.com/{owner}/{repo}".lower()
        for owner, repo in rows if owner and repo
    }


def _annotate_github_measured_projects(
    projects: list[dict[str, Any]], registered: set[str],
    boards: set[str] | None = None, remotes: set[str] | None = None,
) -> None:
    """Mark each project ``has_remote`` (GitHub) and ``registered`` in place.

    Tri-state honesty for the delivery column (#60): registered → counts are a
    real read; a GitHub remote that is NOT registered → "no medido" (distinct
    from "— sin repo" and from a measured "0 entregado"). Both are booleans with
    no names — masking is unaffected.
    """
    boards = boards or set()
    remotes = remotes or set()
    for p in projects:
        pk = p.get("project_key") or ""
        if pk.startswith("git_remote:github.com/"):
            ident = pk[len("git_remote:"):].lower()
            p["has_remote"] = True
            p["registered"] = ident in registered
            # A board (#93) is an issues-only backlog: its outcome is the
            # backlog, never "0 entregado" for lack of merged PRs.
            p["board"] = ident in boards
            # Remote-only (#94): no clone, so no commits — merged PRs are the
            # delivery; the UI says "sin clon", never "0 entregado".
            p["no_clone"] = ident in remotes
        else:
            p["has_remote"] = False
            p["registered"] = False
            p["board"] = False
            p["no_clone"] = False


def _get_snapshot(github_db: str, repo: str, since: str, until: str, *,
                  allow_incomplete: bool = False, tz: Any = None,
                  config: Any = None) -> dict[str, Any]:
    """``{"snapshot": <v1 payload>}`` or ``{error, reason, ...}`` (#82).

    ``tz=None`` = the ``[user] timezone``; ``config=None`` = load it.
    """
    from hub import localtime
    from hub.snapshot import SnapshotError, build_snapshot
    try:
        snap = build_snapshot(
            github_db, repo, since, until, allow_incomplete=allow_incomplete,
            tz=localtime.CONFIGURED if tz is None else tz, config=config)
    except SnapshotError as exc:
        return exc.as_dict()
    return {"snapshot": snap}


def _get_board_issues(
    github_db: str,
    repo: str,
    *,
    state: str = "open",
    label: str | None = None,
    assignee: str | None = None,
    min_age_days: int | None = None,
    max_age_days: int | None = None,
    days: int = 30,
    limit: int = 100,
    today: str | None = None,
    tz: Any = None,
) -> dict[str, Any]:
    """Issues of one registered repo — the backlog view of a board (#93).

    ``repo`` is ``owner/repo`` (case-insensitive). Filters: ``state``
    (``open`` | ``closed`` | ``all``), ``label`` (exact, case-insensitive),
    ``assignee`` (login, case-insensitive), and age in days since
    ``created_at`` (``min_age_days`` / ``max_age_days``, measured in local
    days). PRs are never listed. The window summary counts issues ``opened``
    and ``closed`` on a local day of the last ``days`` days (``hub.localtime``;
    ``today``/``tz`` injectable for tests, ``tz=None`` = the ``[user]
    timezone``) over the whole backlog, not the filtered list, plus
    ``open_by_label`` for the open backlog. Read-only over ``github.db``.
    """
    from hub import localtime

    days = min(max(int(days), 1), 365)
    limit = min(max(int(limit), 1), 500)
    state = (state or "open").lower()
    if state not in ("open", "closed", "all"):
        return {"error": f"state must be open, closed or all (got {state!r})"}
    parsed = repo.strip().split("/")
    if len(parsed) != 2 or not all(parsed):
        return {"error": f"repo must be owner/repo (got {repo!r})"}
    owner, name = parsed
    if _exclusions().matches(owner, name):
        # Confidential (#95): answered exactly like an unknown repo.
        return {"error": f"repo not registered: {repo}"}

    conn = _connect_optional(github_db)
    if conn is None:
        return {"error": "github.db not found — register a repo with mool repo add"}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        kind_sql = "kind" if "kind" in cols else "'code'"
        row = conn.execute(
            f"SELECT id, owner, repo_name, {kind_sql} AS kind FROM repos"
            " WHERE lower(owner) = lower(?) AND lower(repo_name) = lower(?)",
            (owner, name)).fetchone()
        if row is None:
            return {"error": f"repo not registered: {repo}"}
        issues = conn.execute(
            """SELECT number, title, state, author, assignees, labels,
                      created_at, updated_at, closed_at
               FROM github_issues
               WHERE repo_id = ? AND COALESCE(is_pull_request, 0) = 0
               ORDER BY created_at DESC""",
            (row["id"],)).fetchall()
    except sqlite3.OperationalError:
        return {"error": "github.db has no issues table yet"}
    finally:
        conn.close()

    if tz is None:
        tz = localtime.user_timezone()
    if today is None:
        today = localtime.today(tz)
    start = localtime.window_start(today, days)
    from datetime import date
    today_d = date.fromisoformat(today)

    label_l = label.lower() if label else None
    assignee_l = assignee.lower() if assignee else None
    opened_w = closed_w = open_total = 0
    by_label: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for r in issues:
        labels = json.loads(r["labels"]) if r["labels"] else []
        assignees = json.loads(r["assignees"]) if r["assignees"] else []
        created_day = localtime.local_day(r["created_at"], tz)
        closed_day = localtime.local_day(r["closed_at"], tz) if r["closed_at"] else None
        if created_day and start <= created_day <= today:
            opened_w += 1
        if closed_day and start <= closed_day <= today:
            closed_w += 1
        if r["state"] == "open":
            open_total += 1
            for lb in labels or ["(sin label)"]:
                by_label[lb] = by_label.get(lb, 0) + 1
        if state != "all" and r["state"] != state:
            continue
        if label_l and label_l not in {str(x).lower() for x in labels}:
            continue
        if assignee_l and assignee_l not in {str(x).lower() for x in assignees}:
            continue
        age = ((today_d - date.fromisoformat(created_day)).days
                if created_day else None)
        if min_age_days is not None and (age is None or age < int(min_age_days)):
            continue
        if max_age_days is not None and (age is None or age > int(max_age_days)):
            continue
        if len(out) < limit:
            out.append({
                "number": r["number"],
                "title": r["title"],
                "state": r["state"],
                "labels": labels,
                "assignees": assignees,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "closed_at": r["closed_at"],
                "age_days": age,
            })
    return {
        "repo": f"{row['owner']}/{row['repo_name']}",
        "kind": row["kind"] or "code",
        "window_days": days,
        "window_start": start,
        "window_end": today,
        "opened_window": opened_w,
        "closed_window": closed_w,
        "open_total": open_total,
        "open_by_label": dict(sorted(by_label.items(),
                                     key=lambda kv: (-kv[1], kv[0]))),
        "matched": len(out),
        "issues": out,
    }


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
    excl = _exclusions()
    out = []
    for r in rows:
        d = dict(r)
        if excl and excl.matches_remote(d.get("remote_url")):
            continue  # confidential repo (#95)
        d["sources"] = _lit_sources(
            d["session_touches"] or 0, d["fs_touches"] or 0, d["git_touches"] or 0
        )
        out.append(d)
    return _mask_workspace_rows(out, _hide_project_names())


def _mask_grouped(grouped: dict[str, Any], hide: bool) -> dict[str, Any]:
    """Mask every display name in a grouped-portfolio payload (issues #24, #29).

    Recurses into client nodes, project groups and their nested children.
    ``client_label``/``project_label`` and the child/orphan name fields are NAMES
    → masked; ``client_key``/``project_key``/``workspace_key`` are JOIN HANDLES →
    never touched (same contract as ``_mask_workspace_rows``). State/outcome dicts
    hold no labels, so they pass through untouched.
    """
    from hub.config import masked_label

    def _mask_project(p: dict[str, Any]) -> None:
        p["project_label"] = masked_label(p.get("project_label") or "", hide)
        p["children"] = _mask_workspace_rows(p.get("children", []), hide)

    for c in grouped.get("clients", []):
        c["client_label"] = masked_label(c.get("client_label") or "", hide)
        for p in c.get("projects", []):
            _mask_project(p)
        for p in c.get("inactive", []):
            _mask_project(p)
    for p in grouped.get("projects", []):
        _mask_project(p)
    for p in grouped.get("external", []):
        _mask_project(p)
    for p in grouped.get("temporary", []):
        _mask_project(p)
    grouped["unclassified"] = _mask_workspace_rows(grouped.get("unclassified", []), hide)
    grouped["reference"] = _mask_workspace_rows(grouped.get("reference", []), hide)
    return grouped


def _get_portfolio_grouped(
    db_path: str, since: str | None = None,
    events_db: str | None = None, github_db: str | None = None,
) -> dict[str, Any]:
    """Hierarchical portfolio (issue #24, Stage 1): projects with nested children.

    Collapses harness folders onto their real project and nests subdirs/materials
    under it — a read-time projection over ``workspace_classification`` +
    ``workspace_rollup`` (the rollup is never re-keyed). Each project also carries
    a derived ``state`` (#28) — the honest activity+outcome fusion from
    ``derive_project_states`` (day-scale quiescence over real clocks); it holds no
    labels, so it passes through ``_mask_grouped`` untouched and
    ``hide_project_names`` still holds. Returns an empty structure when
    workspace.db or the classification table is absent (never classified). Masked
    when ``hide_project_names`` is set.
    """
    empty = {"projects": [], "unclassified": [], "reference": [],
             "temporary": [], "summary": {}}
    if not os.path.exists(db_path):
        return empty
    if events_db is None:
        events_db = EVENTS_DB
    if github_db is None:
        github_db = GITHUB_DB
    from pathlib import Path
    from hub.cache.workspace_store import WorkspaceStore
    store = WorkspaceStore(Path(db_path))
    try:
        grouped = store.get_portfolio_grouped(
            since, temporary_containers=_temporary_containers())
        try:
            states = store.derive_project_states(
                events_db, github_db, own_emails=_user_emails()
            )
        except Exception:
            _log.exception("derive_project_states failed; states omitted")
            states = {}
        for p in grouped.get("projects", []) + grouped.get("temporary", []):
            st = states.get(p.get("project_key"))
            if st:
                p["state"] = st
    finally:
        store.close()

    # Confidential repos (#95) never surface, whatever is already stored.
    _drop_excluded_grouped(grouped, _exclusions())

    # Registered-vs-remote honesty (#60): annotate BEFORE the client tier so the
    # folding (registrados sin actividad) and the "no medido" column both see it.
    _annotate_github_measured_projects(
        grouped.get("projects", []), _registered_remote_keys(github_db),
        _board_remote_keys(github_db), _remote_only_keys(github_db))

    # Third tier (#29): client/org hierarchy over the flat projects. Resolve the
    # effective client config — auto-seed only when the owner identity is known
    # (personal_orgs), else the projection is a flat no-op (pre-#29 shape).
    grouped = _apply_client_hierarchy(grouped, db_path, github_db)
    return _mask_grouped(grouped, _hide_project_names())


def _apply_client_hierarchy(
    grouped: dict[str, Any], workspace_db: str, github_db: str,
) -> dict[str, Any]:
    """Hang the client/org tier over the flat grouped payload (issue #29).

    Reads the client config lazily (mcp is config-free); resolves
    ``personal_orgs`` from ``[user].github_handle`` when unset and auto-seeds
    ``client_orgs`` from the two stores ONLY when a personal identity is known.
    Strips the internal join-only fields before returning.
    """
    from hub.cache.portfolio_clients import (
        group_by_client, suggest_client_orgs, strip_internal, _norm_org,
    )
    from hub.config import load_config
    try:
        cfg = load_config()
        personal = list(cfg.personal_orgs)
        if not personal and cfg.github_handle:
            personal = [cfg.github_handle]
        # Local [workspace] values merged with the shared registry (#95).
        client_orgs = cfg.effective_client_orgs()
        if not client_orgs and personal:
            client_orgs = suggest_client_orgs(workspace_db, github_db)
        overrides = cfg.effective_client_overrides()
        aliases = cfg.effective_owner_aliases()
    except Exception:
        _log.exception("client config unavailable; portfolio stays flat")
        personal, client_orgs, overrides, aliases = [], [], {}, {}

    hierarchy = group_by_client(
        grouped.get("projects", []),
        client_orgs={_norm_org(o) for o in client_orgs},
        personal_orgs={_norm_org(o) for o in personal},
        overrides=overrides,
        owner_aliases={_norm_org(k): v for k, v in aliases.items()},
    )
    grouped["clients"] = hierarchy["clients"]
    grouped["projects"] = hierarchy["projects"]
    grouped["external"] = hierarchy["external"]
    summary = grouped.setdefault("summary", {})
    summary["clients"] = len(hierarchy["clients"])
    summary["external"] = len(hierarchy["external"])
    strip_internal(grouped)
    return grouped


# Deliverable = a produced image or video artifact, by extension (issue #24
# Stage 2). Counted from path_touches (the filesystem watcher) — 0 until the
# owner marks a root, surfaced honestly, never a silent zero.
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico", ".psd", ".ai",
})
_VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv",
    ".wmv", ".mpg", ".mpeg",
})
_DELIVERABLE_EXTS = _IMAGE_EXTS | _VIDEO_EXTS


def _portfolio_production(
    events_db: str,
    workspace_db: str,
    days: int = 30,
    today: str | None = None,
    hide: bool | None = None,
    github_db: str | None = None,
    own_emails: list[str] | None = None,
    tz: Any = _CONFIGURED_TZ,
) -> dict[str, Any]:
    """Per-project production over time — the honest-metric chart (#24 Stage 2).

    EFFORT, not duration: each session is a unit of work dated by the clock of
    its edges (``path_attributions.activity_ts``, #56), the latest event of THAT
    edge per the #53 rule (live rows: ingest epoch, honest even on resumed
    sessions whose original timestamps span months, #18; historical rows:
    parsed event time), so a long session that touched project A weeks ago and
    B today contributes to A on its old day and to B today. Sessions are
    aggregated over the CANONICAL project (``workspace_classification.project_key``
    from Stage 1), so harness/scratchpad folders fold into their real project
    instead of masquerading as projects. Per project we return a per-day,
    per-provider session count (the contribution strip), the total session
    count, active-day count, and a deliverable (image/video) count.

    Days worked (#114): a session counts on EVERY local day it had events on
    a project (``edge_active_days``), not only on its edge's latest event — a
    session resumed over three days lights three days. Per-day cells count
    distinct sessions; ``sessions`` counts distinct sessions in the window
    (never the sum of the days) and ``active_days`` the distinct days. The
    table is used only when its recorded zone is the zone being read; an edge
    with no day rows (its events are gone, or the first derivation has not run
    yet) is dated by its ``activity_ts`` as before.

    A session that touched N distinct projects counts once in EACH — the strip
    is a per-project statement ("this project saw a session that day"), so a
    cross-project session is genuine activity in every project it touched;
    portfolio-wide session counts are therefore NOT additive across rows. On
    each project-day a session counts once, no matter how many edges reached it.

    The session activity clock (``read_session_activity``, one events.db scan)
    is only a fallback for an edge whose ``activity_ts`` and ``event_ts`` are
    both NULL (a legacy edge whose events are gone); on a database filled by the
    #56 pass, events.db is never scanned and the view reads workspace.db alone.

    Each project also carries an OUTCOME layer (issue #27) — ``merged_prs``,
    ``closed_issues``, ``open_issues`` — the authoritative delivery already
    ingested in ``github.db``, folded onto the SAME canonical project (read-only
    join; see ``WorkspaceStore.read_github_outcome``). Contributor-agnostic (all
    authors summed, none surfaced). These are a merged-PR/closed-issue **FACT**,
    a distinct signal from ``delivery_candidate`` (#22) — never conflated. NOTE:
    ``merged_prs`` / ``closed_issues`` / ``open_issues`` are **all-time totals**
    (the UI labels this); ``merged_prs_window`` / ``closed_issues_window``
    (#81) count only what merged / closed on a local day of the window. They
    attach only to projects already visible in the window (no phantom rows).
    Missing ``github.db`` → all are 0.

    Window: the last ``days`` LOCAL days ending at ``today`` (defaults to the
    current local day; injectable for tests). "Local day" is the one shared
    definition of ``hub.localtime`` (#81): the ``[user] timezone`` zone unless
    a ``tz`` is given (``None`` = the system zone, as in ``hub.localtime``). Read-only over events.db +
    workspace.db (+ github.db for outcome); empty structure when events/workspace
    DB or its tables are absent. Labels masked when ``hide_project_names`` is set
    (outcome counts are integers — nothing to mask).
    """
    days = min(max(int(days), 1), 90)
    if hide is None:
        hide = _hide_project_names()
    empty = {"window_days": days, "providers": [], "projects": [],
             "deliverables_measurable": False}

    wconn = _connect_optional(workspace_db)
    if wconn is None:
        return empty
    econn = _connect_optional(events_db)

    try:
        # 1. Every edge with its canonical project_key (#56). The edge's OWN
        #    clock (activity_ts, else event_ts) dates it; the session clock is
        #    only a fallback for an edge with no clock of its own.
        try:
            crows = wconn.execute("""
                SELECT a.session_id, a.provider, c.project_key, c.project_label,
                       a.activity_ts, a.event_ts, a.workspace_id, a.via
                FROM path_attributions a
                JOIN workspace_classification c
                  ON c.workspace_id = a.workspace_id
                WHERE c.project_key IS NOT NULL
            """).fetchall()
        except sqlite3.OperationalError:
            return empty

        from hub import localtime
        if tz is _CONFIGURED_TZ:
            tz = localtime.user_timezone()
        # Days worked per edge (#114), keyed like the edge minus its file:
        # (session, provider, workspace, via) -> [day, ...]. Read only when the
        # table is bucketed in the zone being read; else every edge falls back.
        edge_days: dict[tuple[str, str, int, str], list[str]] = {}
        try:
            zrow = wconn.execute(
                "SELECT value FROM rollup_meta WHERE key = 'active_days_zone'"
            ).fetchone()
            if zrow is not None and zrow[0] == localtime.zone_key(tz):
                for sid, prov, wid, via, day in wconn.execute(
                    "SELECT session_id, provider, workspace_id, via, day"
                    " FROM edge_active_days"
                ):
                    edge_days.setdefault((sid, prov or "", wid, via), []).append(day)
        except sqlite3.OperationalError:
            edge_days = {}  # pre-#114 workspace.db: activity_ts only

        from hub.cache.workspace_store import _parse_ts
        # (session, provider, project, datetime | None, [days] | None)
        edge_rows: list[tuple[str, str, str, Any, list[str] | None]] = []
        labels: dict[str, str] = {}
        days_done: set[tuple[str, str, int, str]] = set()
        for r in crows:
            pk = r["project_key"]
            sid, prov = r["session_id"], r["provider"] or ""
            dkey = (sid, prov, r["workspace_id"], r["via"])
            worked = edge_days.get(dkey)
            if worked is not None:
                # Many file edges share one key: fold its days once.
                if dkey not in days_done:
                    days_done.add(dkey)
                    edge_rows.append((sid, prov, pk, None, worked))
            else:
                edge_rows.append((
                    sid, prov, pk,
                    _parse_ts(r["activity_ts"]) or _parse_ts(r["event_ts"]),
                    None,
                ))
            if pk not in labels and r["project_label"]:
                labels[pk] = r["project_label"]

        # 2. deliverable (image/video) count per canonical project, from the
        #    filesystem watcher. Empty until a root is marked — reported via
        #    deliverables_measurable so the UI never shows a silent zero.
        deliverables: dict[str, int] = {}
        measurable = False
        try:
            has_touch = wconn.execute(
                "SELECT 1 FROM path_touches LIMIT 1"
            ).fetchone()
            measurable = has_touch is not None
            if measurable:
                trows = wconn.execute("""
                    SELECT c.project_key AS pk, t.path AS path
                    FROM path_touches t
                    JOIN workspace_classification c
                      ON c.workspace_id = t.workspace_id
                    WHERE c.project_key IS NOT NULL
                """).fetchall()
                for r in trows:
                    ext = os.path.splitext(r["path"])[1].lower()
                    if ext in _DELIVERABLE_EXTS:
                        deliverables[r["pk"]] = deliverables.get(r["pk"], 0) + 1
        except sqlite3.OperationalError:
            measurable = False

        # 3. Lazy session fallback (#53): only an edge with NO per-edge clock
        #    needs the session scan — every clocked edge is dated from
        #    workspace.db alone (#56), so the ~194MB scan is skipped entirely on
        #    a healthy database. The same map feeds the state layer, so when it
        #    is needed it is paid once (hot-path invariant).
        activity: dict[tuple[str, str], Any] = {}
        if econn is not None and any(dt is None and worked is None
                                     for *_, dt, worked in edge_rows):
            from hub.cache.workspace_store import read_session_activity
            try:
                activity = read_session_activity(econn)
            except sqlite3.OperationalError:
                activity = {}
    finally:
        wconn.close()
        if econn is not None:
            econn.close()

    # Window bounds (inclusive) in local days (#81 — hub.localtime).
    if today is None:
        today = localtime.today(tz)
    start = localtime.window_start(today, days)

    # 4. Fold each edge into its canonical project on the edge's OWN local day
    #    (#56) — a long session that touched project A weeks ago and B today
    #    contributes to A on its old day and to B today, not to both today.
    #    One session counts once per project per day: several edges to the same
    #    project on one day are still that one session (a per-project
    #    statement, not an edge count).
    proj: dict[str, dict[str, Any]] = {}
    providers_seen: set[str] = set()
    seen_cells: set[tuple[str, str, str, str]] = set()  # (pk, day, provider, sid)
    for sid, provider, pk, dt, worked in edge_rows:
        if worked is None:
            if dt is None:
                dt = activity.get((sid, provider))
            if dt is None:
                continue
            worked = [localtime.local_day(dt, tz)]
        for day in worked:
            if not day or day < start or day > today:
                continue
            providers_seen.add(provider)
            g = proj.setdefault(pk, {
                "project_key": pk,
                "project_label": labels.get(pk, ""),
                "_sessions": set(), "_days": set(), "days": {},
                "last_day": "",
            })
            if (pk, day, provider, sid) in seen_cells:
                continue
            seen_cells.add((pk, day, provider, sid))
            g["_sessions"].add((sid, provider))
            g["_days"].add(day)
            g["days"].setdefault(day, {})
            g["days"][day][provider] = g["days"][day].get(provider, 0) + 1
            if day > g["last_day"]:
                g["last_day"] = day

    # 5. Outcome layer (#27) — merged-PR/closed-issue/open-issue per canonical
    #    project from github.db (read-only). Best-effort: a missing/unreadable
    #    github.db must never break the effort view, so failures fall back to {}.
    if github_db is None:
        github_db = os.path.join(os.path.dirname(workspace_db), "github.db")
    # Outcome counts (#27) and derived per-project STATE (#28) both fold onto the
    # canonical project. The state is a read of evidence over honest clocks
    # (session ingest / fs / git) fused with outcome — carrying no labels, so it
    # needs no masking (see WorkspaceStore.derive_project_states). Best-effort:
    # neither a missing github.db nor a state-derivation failure may break the
    # effort view.
    outcome: dict[str, Any] = {}
    states: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    try:
        from pathlib import Path
        from hub.cache.workspace_store import WorkspaceStore
        # Reuse the fallback map computed above (empty when every edge carried
        # its own clock): the state layer would need the SAME scan, so hand it
        # in and events.db is read once, not twice (hot-path invariant).
        store = WorkspaceStore(Path(workspace_db))
        try:
            outcome = store.read_github_outcome(
                workspace_db, github_db, window=(start, today), tz=tz)
            try:
                states = store.derive_project_states(
                    events_db, github_db, session_ingest=activity,
                    own_emails=own_emails)
            except Exception:
                _log.exception("derive_project_states failed; states omitted")
                states = {}
            # #59: per-project history coverage — where the git history is
            # partial, the UI labels outcome totals "desde <fecha>".
            try:
                coverage = store.read_history_coverage(
                    workspace_db, github_db, tz=tz)
            except Exception:
                _log.exception("read_history_coverage failed; coverage omitted")
                coverage = {}
        finally:
            store.close()
    except Exception:
        outcome = {}

    from hub.config import masked_label
    projects = []
    for pk, g in proj.items():
        oc = outcome.get(pk, {})
        row = {
            "project_key": pk,
            "project_label": masked_label(g["project_label"], hide),
            "sessions": len(g["_sessions"]),
            "active_days": len(g["_days"]),
            "deliverables": deliverables.get(pk, 0),
            "merged_prs": oc.get("merged_prs", 0),
            "closed_issues": oc.get("closed_issues", 0),
            "open_issues": oc.get("open_issues", 0),
            "merged_prs_window": oc.get("merged_prs_window", 0),
            "closed_issues_window": oc.get("closed_issues_window", 0),
            "last_day": g["last_day"],
            "days": g["days"],
        }
        cov = coverage.get(pk)
        if cov:
            row["outcome_complete"] = bool(cov.get("complete"))
            row["outcome_since"] = cov.get("since")
        st = states.get(pk)
        if st:
            row["state"] = st
        projects.append(row)
    # Remote-only repos (#94) have no clone, so no session edges: one with
    # GitHub activity in the window gets its own row (sessions 0) so its
    # merged PRs show up as delivery.
    try:
        remote_rows = _remote_repo_activity(github_db, start, today, tz)
    except Exception:
        _log.exception("remote repo activity failed; rows omitted")
        remote_rows = {}
    for pk, info in remote_rows.items():
        if pk in proj:
            continue
        oc = outcome.get(pk, {})
        row = {
            "project_key": pk,
            "project_label": masked_label(info["label"], hide),
            "sessions": 0,
            "active_days": 0,
            "deliverables": 0,
            "merged_prs": oc.get("merged_prs", 0),
            "closed_issues": oc.get("closed_issues", 0),
            "open_issues": oc.get("open_issues", 0),
            "merged_prs_window": oc.get("merged_prs_window", 0),
            "closed_issues_window": oc.get("closed_issues_window", 0),
            "last_day": info["last_day"],
            "days": {},
        }
        cov = coverage.get(pk)
        if cov:
            row["outcome_complete"] = bool(cov.get("complete"))
            row["outcome_since"] = cov.get("since")
        st = states.get(pk)
        if st:
            row["state"] = st
        projects.append(row)
    # Confidential repos (#95) never surface, whatever is already stored.
    excl = _exclusions()
    if excl:
        projects = [p for p in projects
                    if not excl.matches_remote(p["project_key"])]
    # Registered-vs-remote honesty (#60): a GitHub remote never registered shows
    # "no medido" instead of "— sin repo" — a third, distinct state.
    _annotate_github_measured_projects(
        projects, _registered_remote_keys(github_db), _board_remote_keys(github_db),
        _remote_only_keys(github_db))
    # Hottest (most-recent activity) on top; ties broken by session volume.
    projects.sort(key=lambda p: (p["last_day"], p["sessions"]), reverse=True)

    return {
        "window_days": days,
        "providers": sorted(providers_seen),
        "deliverables_measurable": measurable,
        "projects": projects,
    }


def _get_portfolio_production(
    events_db: str, workspace_db: str, days: int = 30, today: str | None = None,
    github_db: str | None = None,
) -> dict[str, Any]:
    """MCP/dashboard entry for the production chart (issue #24 Stage 2 + #27).

    Thin wrapper resolving ``hide_project_names`` at call time; the work lives
    in ``_portfolio_production`` (pure over its DB paths + injectable ``today``).
    ``github_db`` defaults to the ``events.db`` sibling for the outcome layer.
    """
    if github_db is None:
        github_db = GITHUB_DB
    return _portfolio_production(events_db, workspace_db, days, today=today,
                                 github_db=github_db,
                                 own_emails=_user_emails())


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
    excl = _exclusions()
    out = []
    for r in rows:
        d = dict(r)
        if excl and excl.matches_remote(d.get("remote_url")):
            continue  # confidential repo (#95)
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

_mcp_import_error: ImportError | None = None

try:
    from mcp.server.fastmcp import FastMCP
    try:
        _mcp = FastMCP("moolmesh", instructions=_SERVER_INSTRUCTIONS)
    except TypeError:  # an SDK at the mcp>=1.2.0 floor without `instructions=`
        _mcp = FastMCP("moolmesh")
except ImportError as exc:
    # No tragamos el error: lo guardamos con contexto para distinguir
    # "mcp no instalado" de "mcp versión incompatible" (2.x movió/renombró
    # FastMCP → MCPServer). Ver issue #33 y AGENTS.md §4.
    _mcp = None
    _mcp_import_error = exc
    _log.warning("No se pudo cargar el SDK mcp: %s", exc, exc_info=True)


def _mcp_unavailable_message(exc: ImportError | None) -> str:
    """Mensaje que distingue 'mcp no instalado' de 'versión incompatible'.

    Un ImportError sobre el módulo raíz 'mcp' → no está instalado.
    Un ImportError sobre un submódulo (p.ej. 'mcp.server.fastmcp', que mcp 2.x
    renombró a MCPServer) → está instalado pero es una versión incompatible.
    """
    missing = getattr(exc, "name", None)
    if exc is not None and missing and missing != "mcp":
        return (
            f"Error: the 'mcp' package is installed but is an incompatible "
            f"version ('{missing}' not found). MoolMesh requires mcp>=1.2.0,<2. "
            f"Install a compatible version into moolmesh's environment:  "
            f"pipx inject moolmesh \"mcp>=1.2.0,<2\"  "
            f"(or: pip install \"mcp>=1.2.0,<2\")"
        )
    return (
        "Error: mcp package not installed. Install it into moolmesh's "
        "environment:  mool mcp setup --install-deps  "
        "(or: pipx inject moolmesh \"mcp>=1.2.0,<2\")"
    )

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
    def get_server_info() -> dict[str, Any]:
        """Estado de ESTE servidor MCP: versión en ejecución vs. versión instalada.

        Llamala si los datos te parecen raros o no coinciden con el dashboard
        o la CLI: un servidor MCP vive tanto como la sesión del agente, y tras
        `pipx upgrade` sigue sirviendo el código viejo hasta que se reinicia.
        `stale: true` significa que hay que reiniciar esta sesión (o su servidor
        MCP) para cargar la versión instalada; `installed_version: null`, que no
        se pudo verificar. Los servidores <= 1.26.1 no pueden avisar solos.

        `freshness` dice si los DATOS están al día (#76): `daemon_running`,
        `last_git_sync_at`, `last_github_sync_at`, `repos_stale`,
        `last_ingest_at`, `data_age_seconds` y `stale`. `freshness.stale: true`
        = el daemon no corre o el último sync de git / API de GitHub es más
        viejo que `[daemon] stale_after_minutes`: los números no son actuales.
        """
        return _get_server_info()

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
            provider: Filtrar por provider (claude, codex, qwen, opencode, cursor, pi, antigravity). None = todos.
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
    @_stale_aware
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
    @_stale_aware
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
    @_stale_aware
    @_freshness_aware
    def get_portfolio_grouped(
        since: Optional[str] = None,
    ) -> dict[str, Any]:
        """Portfolio jerárquico: proyectos reales, no 376 carpetas planas (#24).

        Colapsa las carpetas de harness (scratchpad, session-storage) sobre su
        proyecto real, anida subdirs y materiales bajo su proyecto, y manda los
        dotfolders del home y raíces degeneradas a `unclassified`. Devuelve
        `{projects: [...], unclassified: [...], summary: {...}}`; cada proyecto
        trae sus totales (con el harness plegado), `collapsed_harness` (cuántas
        carpetas se plegaron — nada se borra, se agrupa) y sus `children`
        anidados. Cada proyecto trae además `state` (#28) — el estado derivado
        que FUSIONA la actividad local (sesiones/fs/git) con el outcome de GitHub
        en una sola lectura honesta sobre relojes reales: `{state: activo |
        enfriandose | entregado | estancado | pausado, basis: <qué señal lo
        determinó>, last_activity, age_days, outcome_measurable, merged_prs,
        closed_issues, open_issues}`. Es una lectura de evidencia surfaced con su
        base (auditable), nunca un flag asertado; los proyectos sin actividad no
        traen `state`. No lleva nombres → no hay nada que enmascarar. Proyección
        de read-layer sobre `workspace_classification` (no re-keyea el rollup).
        Vacío si no se corrió `mool workspace classify` (o `rollup`). Con
        `hide_project_names`, los nombres se enmascaran.

        Trae `freshness` (#76); con `stale: true` arriba, los números NO son
        actuales (daemon caído o sync viejo) — decilo antes de usarlos.

        Args:
            since: Fecha ISO 8601 desde (compara por día). None = todo.
        """
        return _get_portfolio_grouped(WORKSPACE_DB, since)

    @_mcp.tool()
    @_stale_aware
    @_freshness_aware
    def get_portfolio_production(days: int = 30) -> dict[str, Any]:
        """Producción por proyecto en el tiempo — esfuerzo, no duración (#24).

        Métrica honesta: cada sesión es una unidad de trabajo fechada por
        `MAX(events.created_at)` (epoch de INGESTA — honesto incluso en sesiones
        resumidas, cuyos timestamps originales abarcan meses) en las filas en
        vivo, y por la hora real del evento en la historia importada (backfill /
        catch-up / re-parseo, #53), agregada sobre el
        proyecto CANÓNICO (`workspace_classification.project_key` de la Etapa 1),
        así el harness/scratchpad se pliega en su proyecto real. Por proyecto
        devuelve la serie diaria por provider (la tira de contribución: una
        sesión cuenta en CADA día local en que trabajó el proyecto, #114; cada
        día cuenta sesiones distintas), `sessions` (sesiones distintas en la
        ventana, nunca la suma de los días), `active_days` (días distintos) y
        `deliverables` (conteo de imagen/video desde
        el watcher de filesystem — 0 hasta que se marca un root, con
        `deliverables_measurable` para no mostrar un cero silencioso). Cada
        proyecto trae además la capa de OUTCOME (#27): `merged_prs`,
        `closed_issues`, `open_issues` desde `github.db` (entrega autoritativa,
        contributor-agnostic — todos los autores sumados, ninguno expuesto),
        plegada sobre el mismo proyecto canónico. Esos tres son TOTALES all-time;
        `merged_prs_window` y `closed_issues_window` (#81) cuentan sólo lo
        mergeado / cerrado en un día local de la ventana (zona `[user] timezone`,
        la misma definición de "día local" que digests y rollup; una issue
        cerrada sin fecha de cierre no cuenta en la ventana). Son un HECHO
        distinto de `delivery_candidate` (#22, heurístico
        gitless) — nunca se confunden. Cada proyecto trae además `state` (#28) —
        el estado derivado que fusiona esa actividad con el outcome en una sola
        lectura honesta: `{state: activo | enfriandose | entregado | estancado |
        pausado, basis, last_activity, age_days, outcome_measurable, ...}`,
        surfaced con su base (nunca un flag pelado; sin `state` si no hay
        actividad). Una sesión que tocó N proyectos cuenta en CADA uno (los
        totales de esfuerzo NO son aditivos entre filas). Ordenado por actividad
        más reciente. Con `hide_project_names`, los labels se enmascaran (los
        conteos de outcome y el `state` son enteros/tokens — nada que enmascarar).

        Trae `freshness` (#76); con `stale: true` arriba, los números NO son
        actuales (daemon caído o sync viejo) — decilo antes de usarlos.

        Args:
            days: Ventana en días (1–90, default 30).
        """
        return _get_portfolio_production(EVENTS_DB, WORKSPACE_DB, days)

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

    @_mcp.tool()
    @_stale_aware
    @_freshness_aware
    def get_board_issues(
        repo: str,
        state: str = "open",
        label: str | None = None,
        assignee: str | None = None,
        min_age_days: int | None = None,
        max_age_days: int | None = None,
        days: int = 30,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Backlog de issues de un repo registrado — la vista de un repo board (#93).

        Un repo `kind = "board"` es un backlog sólo de issues (sin código): no
        corre git y no se lee como "0 entregado". Sirve igual para un repo de
        código. Lista issues (nunca PRs) filtrables por `label`, `assignee` y
        antigüedad (`min_age_days` / `max_age_days`, días locales desde
        `created_at`), y resume el backlog entero: `opened_window` /
        `closed_window` (issues abiertas / cerradas en un día local de los
        últimos `days` días, zona `[user] timezone`), `open_total` y
        `open_by_label`. Lectura de `github.db`; los datos son tan frescos como
        el último sync de issues (`freshness`).

        Args:
            repo: "owner/repo" (sin distinguir mayúsculas).
            state: open | closed | all (default open).
            label: Sólo issues con este label (exacto, sin mayúsculas).
            assignee: Sólo issues asignadas a este login.
            min_age_days: Antigüedad mínima en días.
            max_age_days: Antigüedad máxima en días.
            days: Ventana de abiertas/cerradas (1-365, default 30).
            limit: Máximo de issues listadas (1-500, default 100).
        """
        return _get_board_issues(
            GITHUB_DB, repo, state=state, label=label, assignee=assignee,
            min_age_days=min_age_days, max_age_days=max_age_days,
            days=days, limit=limit)


    @_mcp.tool()
    @_stale_aware
    @_freshness_aware
    def get_snapshot(
        repo: str,
        since: str,
        until: str,
        allow_incomplete: bool = False,
    ) -> dict[str, Any]:
        """Snapshot versionado de un repo para un rango de días locales (#82).

        El mismo payload que `mool snapshot export` (esquema v1,
        `docs/schema/snapshot.v1.md`) bajo `snapshot`: issues abiertas /
        cerradas en el período, totales al corte, PRs mergeadas con sus stats
        (+/-, archivos, `closes` como `owner/repo#n`) y `snapshot.freshness`
        (`complete`: si el último sync de GitHub del repo cubre el período).
        Un rango que el sync no cubre (p. ej. `until` = hoy) devuelve
        `{error, reason: "incomplete"}` salvo `allow_incomplete=true`, que lo
        marca `complete: false`. El `freshness` de arriba es el global (#76).

        Args:
            repo: "owner/repo" (sin distinguir mayúsculas).
            since: Primer día local, YYYY-MM-DD (inclusive).
            until: Último día local, YYYY-MM-DD (inclusive).
            allow_incomplete: Exportar igual un período no cubierto por el sync.
        """
        return _get_snapshot(GITHUB_DB, repo, since, until,
                             allow_incomplete=allow_incomplete)

if __name__ == "__main__":
    if _mcp is None:
        print(_mcp_unavailable_message(_mcp_import_error), file=sys.stderr)
        sys.exit(1)
    _register_run_file()
    print("MoolMesh MCP Server starting...", file=sys.stderr)
    _mcp.run(transport="stdio")
