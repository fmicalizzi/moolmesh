"""Antigravity CLI harvester — reads ``transcript.jsonl`` files and stores events atomically."""

from __future__ import annotations

import collections
from collections.abc import Callable
from pathlib import Path

from hub.adapters.antigravity_adapter import AntigravityAdapter
from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.models.antigravity import AntigravityEntry, AntigravitySummary
from hub.parsers.antigravity_parser import AntigravityParser, default_antigravity_base
from hub.watchers.base import BaseHarvester


class AntigravityWatcher(BaseHarvester):
    """Harvests Antigravity CLI conversations into EventStore.

    One file per conversation (``brain/<uuid>/.system_generated/logs/transcript.jsonl``),
    all named alike, so the offset is keyed by ``antigravity:<path>``
    (``STABLE_KEY``): the path is stable per conversation, a content
    fingerprint of the first KB is not needed.

    Session metadata (title, workspace, parent conversation) comes from
    ``conversation_summaries.db``. It is read once per rescan, not per poll,
    and the last good answer is kept: a locked, absent or unknown-version
    index never stops the transcripts from being ingested (#52).
    """

    CATCHUP = True  # startup catch-up after a daemon outage (#45)
    STABLE_KEY = True

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        project_filter: str | None = None,
        antigravity_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._project_filter = project_filter
        self._antigravity_base = antigravity_base
        self._parser = AntigravityParser()
        self._adapter = AntigravityAdapter()
        self._file_projects: dict[Path, str] = {}
        self._summaries: dict[str, AntigravitySummary] = {}

    @property
    def provider_name(self) -> str:
        return "antigravity"

    def _base(self) -> Path:
        # Resolved per call so ANTIGRAVITY_APP_DATA_DIR changes are honored.
        return self._antigravity_base or default_antigravity_base()

    def discover_files(
        self, since: float | None = None, skip_dir: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        base = self._base()
        fresh = self._parser.read_summaries(base)
        if fresh is not None:
            self._summaries = fresh  # unreadable this time: keep the last good one
        discovery = ProjectDiscovery(antigravity_base=base, skip_dir=skip_dir)
        projects = discovery.discover_antigravity(summaries=self._summaries)
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

    def _project_for(self, path: Path, entry: AntigravityEntry) -> str:
        """Project label: the workspace, else the first Cwd, else what discovery found."""
        if entry.cwd:
            return ProjectDiscovery.short_cwd(entry.cwd) or self._file_projects.get(
                path, "antigravity")
        return self._file_projects.get(path) or "antigravity"

    def _apply_summary(self, entry: AntigravityEntry) -> None:
        """Stamp the index metadata of the entry's conversation (workspace wins as cwd)."""
        summary = self._summaries.get(entry.session_id)
        if summary is None:
            return
        if summary.cwd:
            entry.cwd = summary.cwd
        entry.parent_session = summary.parent_conversation_id
        entry.nesting_depth = summary.nesting_depth
        entry.summary_title = summary.title
        entry.summary_preview = summary.preview
        entry.summary_last_modified = summary.last_modified

    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        entries, new_offset = self._parser.parse_incremental(path, offset)
        events: list[dict] = []
        # Session metadata once per conversation per chunk, from the LAST entry
        # (the parser stamped the full context on every entry of the chunk).
        meta_entries: dict[str, AntigravityEntry] = {}
        times: dict[str, list[str]] = {}
        for entry in entries:
            self._apply_summary(entry)
            project = self._project_for(path, entry)
            for event in self._adapter.to_events(entry, project):
                events.append(event.to_dict())
            if entry.session_id:
                meta_entries[entry.session_id] = entry
                times.setdefault(entry.session_id, []).append(entry.timestamp)
        for session_id, entry in meta_entries.items():
            meta = self._adapter.to_session_meta(entry, self._project_for(path, entry))
            if not meta:
                continue
            # Steps can arrive out of order (a repeated conversation restarts at
            # step 0), so the last line is not the latest event: record the
            # earliest and the latest timestamp of the chunk explicitly — the
            # session's last_event_at must never go back.
            first, last = min(times[session_id]), max(times[session_id])
            self._store.upsert_session(meta.to_dict(), first)
            if last != first:
                self._store.upsert_session(meta.to_dict(), last)
        return events, new_offset
