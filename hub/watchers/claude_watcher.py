"""Claude Code session harvester — reads JSONL files and stores events atomically."""

from __future__ import annotations

import collections
from collections.abc import Callable
from pathlib import Path

from hub.adapters.claude_adapter import ClaudeAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.parsers.claude_parser import ClaudeParser
from hub.watchers.base import BaseHarvester


class ClaudeWatcher(BaseHarvester):
    """Harvests Claude Code JSONL session files into EventStore."""

    CATCHUP = True  # startup catch-up after a daemon outage (#45)

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        project_filter: str | None = None,
        claude_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._project_filter = project_filter
        self._claude_base = claude_base
        self._parser = ClaudeParser()
        self._adapter = ClaudeAdapter()
        self._file_projects: dict[Path, str] = {}

    @property
    def provider_name(self) -> str:
        return "claude"

    def discover_files(
        self, since: float | None = None, skip_dir: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        discovery = ProjectDiscovery(claude_base=self._claude_base, skip_dir=skip_dir)
        projects = discovery.discover_claude()
        files: list[Path] = []
        cutoff = self._default_cutoff() if since is None else since
        for proj in projects:
            if self._project_filter and self._project_filter.lower() not in proj.name.lower():
                continue
            label = ProjectDiscovery.short_cwd(proj.path)
            for f in proj.session_files:
                try:
                    if f.stat().st_mtime >= cutoff:
                        files.append(f)
                        self._file_projects[f] = label or proj.name
                except OSError:
                    continue
        return files

    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        entries, new_offset = self._parser.parse_incremental(path, offset)
        project = self._file_projects.get(path, "unknown")
        events = []
        seen_sessions: set[str] = set()
        for entry in entries:
            event = self._adapter.to_event(entry, project)
            if event:
                events.append(event.to_dict())
            if entry.session_id and entry.session_id not in seen_sessions:
                seen_sessions.add(entry.session_id)
                meta = self._adapter.to_session_meta(entry, project)
                if meta:
                    self._store.upsert_session(meta.to_dict(), entry.timestamp)
            # Terminal signal: an observed /exit ends the session (issue #16).
            # Keyed on the entry's own session_id so a sidechain/subagent file
            # never ends its parent. Applied after upsert so it wins the batch.
            reason = self._adapter.terminal_reason(entry)
            if reason and entry.session_id:
                self._store.mark_session_ended(
                    entry.session_id, self.provider_name, entry.timestamp, reason
                )
        return events, new_offset
