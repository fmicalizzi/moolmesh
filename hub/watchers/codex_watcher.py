"""Codex session harvester — reads rollout JSONL files and stores events atomically."""

from __future__ import annotations

import collections
from collections.abc import Callable
from pathlib import Path

from hub.adapters.codex_adapter import CodexAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.parsers.codex_parser import CodexParser
from hub.watchers.base import BaseHarvester


class CodexWatcher(BaseHarvester):
    """Harvests Codex rollout JSONL files into EventStore."""

    CATCHUP = True  # startup catch-up after a daemon outage (#45)

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        codex_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._codex_base = codex_base
        self._parser = CodexParser()
        self._adapter = CodexAdapter()
        self._file_projects: dict[Path, str] = {}

    @property
    def provider_name(self) -> str:
        return "codex"

    def discover_files(
        self, since: float | None = None, skip_dir: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        discovery = ProjectDiscovery(codex_base=self._codex_base, skip_dir=skip_dir)
        projects = discovery.discover_codex()
        files: list[Path] = []
        cutoff = self._default_cutoff() if since is None else since
        for proj in projects:
            for f in proj.session_files:
                try:
                    if f.stat().st_mtime >= cutoff:
                        files.append(f)
                        self._file_projects[f] = proj.name
                except OSError:
                    continue
        return files

    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        entries, new_offset = self._parser.parse_incremental(path, offset)
        events = []
        seen_sessions: set[str] = set()
        for entry in entries:
            project = self._file_projects.get(path, "codex-sessions")
            # to_events: one Codex call can touch several directories (#40).
            for event in self._adapter.to_events(entry, project):
                events.append(event.to_dict())
            if entry.session_id and entry.session_id not in seen_sessions:
                seen_sessions.add(entry.session_id)
                meta = self._adapter.to_session_meta(entry, project)
                if meta:
                    self._store.upsert_session(meta.to_dict(), entry.timestamp)
                # Sub-agent session (#65): record the parent → child link in
                # the existing session_links table, additively and idempotent
                # (INSERT OR IGNORE on the unique key). The parent session may
                # not be ingested yet; the link is still valid.
                if entry.parent_session_id and entry.parent_session_id != entry.session_id:
                    link_meta = dict(entry.agent_meta or {})
                    self._store.link_sessions(
                        entry.parent_session_id, "codex",
                        entry.session_id, "codex",
                        link_type="subagent",
                        metadata=link_meta or None,
                    )
        return events, new_offset
