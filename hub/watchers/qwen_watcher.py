"""Qwen CLI session harvester — reads JSONL files and stores events atomically."""

from __future__ import annotations

import collections
import time
from pathlib import Path

from hub.adapters.qwen_adapter import QwenAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.parsers.qwen_parser import QwenParser
from hub.watchers.base import BaseHarvester


class QwenWatcher(BaseHarvester):
    """Harvests Qwen CLI JSONL session files into EventStore."""

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        project_filter: str | None = None,
        qwen_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._project_filter = project_filter
        self._qwen_base = qwen_base
        self._parser = QwenParser()
        self._adapter = QwenAdapter()
        self._file_projects: dict[Path, str] = {}

    @property
    def provider_name(self) -> str:
        return "qwen"

    def discover_files(self) -> list[Path]:
        discovery = ProjectDiscovery(qwen_base=self._qwen_base)
        projects = discovery.discover_qwen()
        files: list[Path] = []
        cutoff = time.time() - (self.MAX_AGE_HOURS * 3600)
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
        for entry in entries:
            event = self._adapter.to_event(entry, project)
            if event:
                events.append(event.to_dict())
        return events, new_offset
