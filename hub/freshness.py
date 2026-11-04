"""Is the data current? Measured from the data, not from "the process lives" (#76).

Two real outages left the daemon down for days while MCP tools kept answering
with a frozen portfolio and GitHub cache. ``compute_freshness`` reads when each
source last synced and says whether the numbers can be trusted as current:

- ``daemon_running``: pidfile + live PID (the harvesters only run in it).
- ``last_git_sync_at``: ``MAX(repos.last_fetch_at)`` in ``github.db`` — the git
  harvester writes it after each successful ``git fetch``.
- ``last_github_sync_at``: ``MAX(repos.last_github_sync_at)`` — the GitHub API
  harvester writes it after a successful issues/PRs poll (200 or 304).
- ``repos_stale``: registered repos whose own git sync — or GitHub sync, when
  that repo was ever API-synced — is older than the threshold (a max over 80
  repos would hide a single repo that stopped syncing).
- ``last_ingest_at``: ingest time of the newest row in ``events.db``. Reported,
  never a stale reason by itself: a weekend without agents is legitimate.
- ``data_age_seconds``: age of the most recent of those three timestamps.

``stale`` is true when the daemon is not running, or when repos are
registered and the last git sync — or the last GitHub API sync, once one was
ever recorded — is older than ``[daemon] stale_after_minutes`` (default 60).
A repo set with no git sync at all also counts as stale.

All reads are ``mode=ro`` and cheap: ``repos`` is one row per registered repo
and the newest event is looked up by rowid. Missing databases are "unknown",
never an error.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def _parse_ts(value) -> float | None:
    """Epoch seconds from an ISO timestamp (naive = UTC) or a number."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _connect_ro(path: str | Path) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    from hub.sqlite_ro import connect_ro
    return connect_ro(path)


def _repo_syncs(github_db: str | Path) -> list[tuple[float | None, float | None]] | None:
    """``[(git_epoch, github_epoch), ...]`` per registered repo, None if no DB."""
    try:
        conn = _connect_ro(github_db)
    except sqlite3.Error:
        return None
    if conn is None:
        return None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        if not cols:
            return []
        gh_col = "last_github_sync_at" if "last_github_sync_at" in cols else "NULL"
        rows = conn.execute(f"SELECT last_fetch_at, {gh_col} FROM repos").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [(_parse_ts(git), _parse_ts(gh)) for git, gh in rows]


def _last_ingest(events_db: str | Path) -> float | None:
    try:
        conn = _connect_ro(events_db)
    except sqlite3.Error:
        return None
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT created_at FROM events ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return _parse_ts(row[0]) if row else None


def daemon_running() -> bool:
    """Pidfile + live PID, without touching the pidfile (MCP calls this)."""
    from hub import daemon
    try:
        pid = int(daemon.PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return daemon._pid_alive(pid)


def stale_after_minutes() -> int:
    from hub.config import DEFAULT_STALE_AFTER_MINUTES, load_config
    try:
        return int(load_config().stale_after_minutes)
    except Exception:  # noqa: BLE001 — an unreadable config must not break a tool call
        return DEFAULT_STALE_AFTER_MINUTES


def compute_freshness(
    events_db: str | Path,
    github_db: str | Path,
    *,
    running: bool | None = None,
    threshold_minutes: int | None = None,
    now: float | None = None,
) -> dict:
    """The ``freshness`` block (see the module docstring for each key)."""
    now = time.time() if now is None else now
    running = daemon_running() if running is None else running
    minutes = stale_after_minutes() if threshold_minutes is None else threshold_minutes
    limit = minutes * 60

    repos = _repo_syncs(github_db) or []
    git_times = [g for g, _ in repos if g is not None]
    gh_times = [h for _, h in repos if h is not None]
    last_git = max(git_times) if git_times else None
    last_gh = max(gh_times) if gh_times else None
    last_ingest = _last_ingest(events_db)

    repos_stale = sum(
        1 for git, gh in repos
        if git is None or now - git > limit or (gh is not None and now - gh > limit)
    )

    reasons = []
    if not running:
        reasons.append("daemon_not_running")
    if repos:
        if last_git is None or now - last_git > limit:
            reasons.append("git_sync_old")
        if last_gh is not None and now - last_gh > limit:
            reasons.append("github_sync_old")

    known = [t for t in (last_git, last_gh, last_ingest) if t is not None]
    data_age = int(max(0.0, now - max(known))) if known else None
    return {
        "daemon_running": running,
        "last_git_sync_at": _iso(last_git),
        "last_github_sync_at": _iso(last_gh),
        "repos_registered": len(repos),
        "repos_stale": repos_stale,
        "last_ingest_at": _iso(last_ingest),
        "data_age_seconds": data_age,
        "stale_after_minutes": minutes,
        "stale": bool(reasons),
        "stale_reasons": reasons,
    }


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"
