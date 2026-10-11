"""Registry of running MCP servers (#70): ``~/.moolmesh/run/mcp-<pid>.json``.

Every ``hub.mcp_server`` process outlives ``pipx upgrade`` for as long as its
agent session does, silently serving the code it started with. Each server
records itself here on startup so ``mool doctor`` can list the ones that are
still running an older version than the one installed.

This is MoolMesh's own state, like the daemon pidfile — the read-only invariant
covers provider data and ``events.db``, not ``~/.moolmesh/run/``. Stdlib only
and free of the ``mcp`` SDK import (and its warning) so ``doctor`` can import it
in any environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_log = logging.getLogger("moolmesh.mcp_registry")

_PATTERN = "mcp-*.json"


def run_dir() -> Path:
    """``~/.moolmesh/run`` — resolved on every call so a changed HOME is honored."""
    return Path.home() / ".moolmesh" / "run"


def write_run_file(
    version: str, started_at: str, directory: Path | None = None,
) -> Path:
    """Atomically record this process as ``mcp-<pid>.json``. Raises ``OSError``.

    Written next to the final name and moved with ``os.replace`` so a reader
    (``mool doctor``) never sees a half-written file.
    """
    directory = directory if directory is not None else run_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    payload = {
        "pid": pid,
        "ppid": os.getppid(),
        "version": version,
        "started_at": started_at,
        "python": sys.executable,
    }
    final = directory / f"mcp-{pid}.json"
    tmp = directory / f"mcp-{pid}.json.tmp"
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, final)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return final


def remove_run_file(path: Path) -> None:
    """Delete a run file; meant for ``atexit``, so it never raises."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        _log.debug("could not remove MCP run file %s", path, exc_info=True)


@dataclass(frozen=True)
class McpServer:
    """One live registered MCP server."""
    pid: int
    ppid: int | None
    version: str | None
    started_at: str | None
    python: str | None
    stale: bool


@dataclass
class McpScan:
    servers: list[McpServer] = field(default_factory=list)  # live ones, oldest first
    removed: int = 0                                         # dead-PID files deleted
    skipped: list[str] = field(default_factory=list)         # unreadable files, left alone

    @property
    def stale(self) -> int:
        return sum(1 for s in self.servers if s.stale)


def _parse(path: Path) -> dict | None:
    """The run file's JSON object, or None when it is unreadable or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    # bool is an int subclass; pid <= 0 would make os.kill(pid, 0) signal a process group
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return data


def scan_run_dir(
    directory: Path,
    installed_version: str | None,
    is_alive: Callable[[int], bool] | None = None,
) -> McpScan | None:
    """Read the registry, delete files of dead servers, flag the stale live ones.

    Returns ``None`` when the directory does not exist. A server is stale when
    its recorded version differs from ``installed_version``; with no installed
    version to compare against (``None`` / ``"dev"``) nothing is flagged.
    Never signals a process — liveness goes through ``hub.daemon._pid_alive``.
    """
    if not directory.is_dir():
        return None
    if is_alive is None:
        from hub.daemon import _pid_alive as is_alive
    comparable = installed_version not in (None, "", "dev")

    scan = McpScan()
    for path in sorted(directory.glob(_PATTERN)):
        data = _parse(path)
        if data is None:
            scan.skipped.append(path.name)
            continue
        if not is_alive(data["pid"]):
            try:
                path.unlink(missing_ok=True)
                scan.removed += 1
            except OSError:
                _log.debug("could not remove dead MCP run file %s", path, exc_info=True)
            continue
        version = data.get("version") if isinstance(data.get("version"), str) else None
        ppid = data.get("ppid") if isinstance(data.get("ppid"), int) else None
        scan.servers.append(McpServer(
            pid=data["pid"],
            ppid=ppid,
            version=version,
            started_at=data.get("started_at") if isinstance(data.get("started_at"), str) else None,
            python=data.get("python") if isinstance(data.get("python"), str) else None,
            stale=comparable and version is not None and version != installed_version,
        ))
    scan.servers.sort(key=lambda s: (s.started_at or "", s.pid))
    return scan


def format_age(started_at: str | None, now: datetime | None = None) -> str:
    """``3d`` / ``5h`` / ``12m`` since ``started_at`` (ISO 8601), ``?`` if unknown."""
    try:
        started = datetime.fromisoformat(started_at or "")
    except ValueError:
        return "?"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = int(((now or datetime.now(timezone.utc)) - started).total_seconds())
    if seconds < 0:
        return "?"
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(seconds // 60, 0)}m"
