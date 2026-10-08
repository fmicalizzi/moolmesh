"""Pi session harvester — reads session JSONL files and stores events atomically."""

from __future__ import annotations

import collections
from collections.abc import Callable
from pathlib import Path

from hub.adapters.pi_adapter import PiAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.models.pi import PiEntry
from hub.parsers.pi_parser import PiParser
from hub.watchers.base import BaseHarvester


class PiWatcher(BaseHarvester):
    """Harvests Pi session JSONL files into EventStore."""

    CATCHUP = True  # startup catch-up after a daemon outage (#45)

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        project_filter: str | None = None,
        pi_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._project_filter = project_filter
        self._pi_base = pi_base
        self._parser = PiParser()
        self._adapter = PiAdapter()
        self._file_projects: dict[Path, str] = {}

    @property
    def provider_name(self) -> str:
        return "pi"

    def discover_files(
        self, since: float | None = None, skip_dir: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        discovery = ProjectDiscovery(pi_base=self._pi_base, skip_dir=skip_dir)
        projects = discovery.discover_pi()
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
        # Session metadata from the LAST entry per session in this chunk: the
        # parser stamps the full context (model from the last model_change or
        # assistant, first user prompt) onto every entry, and the upsert merges
        # additively, so one write per session per chunk is enough.
        meta_entries: dict[str, PiEntry] = {}
        for entry in entries:
            project = self._file_projects.get(path, project)
            # to_events: one assistant message can touch several directories.
            for event in self._adapter.to_events(entry, project):
                events.append(event.to_dict())
            if entry.session_id:
                meta_entries[entry.session_id] = entry
        for entry in meta_entries.values():
            meta = self._adapter.to_session_meta(entry, project)
            if meta:
                self._store.upsert_session(meta.to_dict(), entry.timestamp)
        return events, new_offset
