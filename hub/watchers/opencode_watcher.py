"""OpenCode session harvester — polls SQLite DB using rowid as incremental cursor."""

from __future__ import annotations

import collections
import time
from pathlib import Path

from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.cache.event_store import EventStore, file_fingerprint
from hub.parsers.opencode_parser import OpenCodeParser
from hub.watchers.base import BaseHarvester


_DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


class OpenCodeWatcher(BaseHarvester):
    """Harvests OpenCode SQLite sessions into EventStore.

    Uses rowid as cursor instead of byte offset — same atomic
    store_with_offset pattern as JSONL watchers.
    """

    POLL_INTERVAL: float = 2.0
    RESCAN_INTERVAL: float = 60.0

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        opencode_db: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._db_path = opencode_db or _DEFAULT_DB
        self._parser = OpenCodeParser()
        self._adapter = OpenCodeAdapter()
        self._entry_projects: dict[str, str] = {}

    @property
    def provider_name(self) -> str:
        return "opencode"

    def discover_files(self) -> list[Path]:
        if self._db_path.exists():
            return [self._db_path]
        return []

    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        entries, new_offset = self._parser.parse_incremental(path, offset)
        events = []
        for entry in entries:
            project = entry.project_name or entry.project_dir or "opencode"
            event = self._adapter.to_event(entry, project)
            if event:
                events.append(event.to_dict())
        return events, new_offset
