"""Codex session harvester — reads rollout JSONL files and stores events atomically."""

from __future__ import annotations

import collections
import time
from pathlib import Path

from hub.adapters.codex_adapter import CodexAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.parsers.codex_parser import CodexParser
from hub.watchers.base import BaseHarvester


class CodexWatcher(BaseHarvester):
    """Harvests Codex rollout JSONL files into EventStore."""

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

    def discover_files(self) -> list[Path]:
        discovery = ProjectDiscovery(codex_base=self._codex_base)
        projects = discovery.discover_codex()
        files: list[Path] = []
        cutoff = time.time() - (self.MAX_AGE_HOURS * 3600)
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
        for entry in entries:
            project = self._file_projects.get(path, "codex-sessions")
            event = self._adapter.to_event(entry, project)
            if event:
                events.append(event.to_dict())
        return events, new_offset
