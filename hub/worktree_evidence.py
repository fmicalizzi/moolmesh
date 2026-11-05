"""``git worktree add`` commands read back from the raw transcripts (#161).

The one-time backfill of #161 needs the FULL command of every
``git worktree add`` a session ran, but ``events.db`` only kept a capped
summary of it before #161 (and nothing at all for some providers). The raw
session files still have it. This module reads them through each provider's
own parser and adapter — the same path the watchers use, so no provider format
is re-implemented here — and returns ``(command, cwd)`` for every shell call
whose adapter kept the command as evidence (``with_shell_evidence``).

Read-only by construction: files are only read (a cheap byte scan for
``worktree`` first, so most files are never parsed), SQLite sources are opened
by their parsers in ``mode=ro``, nothing is written to ``events.db`` (session
metadata goes to a null sink) and cloud-only placeholders are never opened.
Cursor is not read: its stored tool calls carry no shell command.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from hub.cloudfiles import PlaceholderSkipper, is_cloud_placeholder
from hub.models.base import SHELL_TOOLS, shell_evidence_command

_log = logging.getLogger("hub.worktree_evidence")

# File-based providers (scanned per file) and the SQLite one whose tool calls
# carry the command.
FILE_PROVIDERS = ("claude", "codex", "qwen", "pi", "antigravity")
DB_PROVIDERS = ("opencode",)
_NEEDLE = b"worktree"
_CHUNK = 1 << 20


class _NullSessionSink:
    """Stands in for EventStore while parsing: nothing is written.

    Every public method a watcher may call on its store is a no-op that
    returns None (#165): ``upsert_session``, ``mark_session_ended``,
    ``link_sessions`` for a Codex sub-agent (#65), and whatever a watcher
    starts calling later, so a new store call can never break the scan.
    """

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _noop


def _noop(*_a, **_kw) -> None:
    return None


@dataclass
class TranscriptScan:
    """What a scan found: the commands plus counts for ``hub.log``."""

    commands: list[tuple[str, str | None, str]] = field(default_factory=list)
    files_seen: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    files_cloud: int = 0


def _mentions_worktree(path: Path) -> bool:
    """True when the file's bytes contain ``worktree`` (streamed, read-only)."""
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                return False
            if _NEEDLE in tail + chunk:
                return True
            tail = chunk[-(len(_NEEDLE) - 1):]


def _make_watcher(provider: str, bases: dict[str, Path | None]):
    sink = _NullSessionSink()
    if provider == "opencode":
        from hub.watchers.opencode_watcher import OpenCodeWatcher
        return OpenCodeWatcher(sink, None, opencode_db=bases.get("opencode"))
    from hub.backfill import make_watcher
    return make_watcher(provider, sink, bases)


def _collect(scan: TranscriptScan, provider: str, events: list[dict]) -> None:
    for ev in events:
        if (ev.get("tool_name") or "") not in SHELL_TOOLS:
            continue
        cmd = shell_evidence_command(ev.get("full_text"))
        if cmd:
            scan.commands.append((cmd, ev.get("cwd"), provider))


def scan_worktree_adds(
    bases: dict[str, Path | None] | None = None,
    providers: tuple[str, ...] = FILE_PROVIDERS + DB_PROVIDERS,
) -> TranscriptScan:
    """Every ``git worktree add`` command in the providers' raw transcripts.

    ``bases`` overrides a provider's data root (tests). Each command is
    complete: it comes from the transcript itself, never from a capped copy.
    A file that fails to parse is counted and logged, never fatal.
    """
    bases = bases or {}
    scan = TranscriptScan()
    for provider in providers:
        try:
            watcher = _make_watcher(provider, bases)
            if provider in DB_PROVIDERS:
                files = watcher.discover_files()
            else:
                files = watcher.discover_files(since=0.0, skip_dir=PlaceholderSkipper())
        except Exception as exc:  # noqa: BLE001 — best-effort, never blocks (#165)
            _log.warning("worktree add: no pude listar las sesiones de %s (%s)",
                         provider, type(exc).__name__)
            continue
        for path in files:
            scan.files_seen += 1
            if is_cloud_placeholder(path):
                scan.files_cloud += 1
                continue
            try:
                if provider not in DB_PROVIDERS and not _mentions_worktree(path):
                    continue
                scan.files_parsed += 1
                events, _offset = watcher._parse_and_adapt(path, 0)
            except Exception as exc:  # noqa: BLE001 — one bad file never stops the scan (#165)
                # Only the error type: a session file's path names a project.
                scan.files_failed += 1
                _log.warning("worktree add: no pude leer un archivo de sesión "
                             "de %s (%s)", provider, type(exc).__name__)
                continue
            _collect(scan, provider, events)
    return scan
