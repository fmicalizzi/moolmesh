"""Cursor session harvester — polls the global state.vscdb by rowid."""

from __future__ import annotations

import collections
from pathlib import Path

from hub.adapters.cursor_adapter import CursorAdapter
from hub.cache.event_store import EventStore
from hub.parsers.cursor_parser import CursorParser, default_cursor_base
from hub.watchers.base import BaseHarvester


class CursorWatcher(BaseHarvester):
    """Harvests Cursor agent/composer bubbles into EventStore.

    Uses the global state.vscdb cursorDiskKV rowid as the incremental cursor.
    """

    POLL_INTERVAL: float = 2.0
    RESCAN_INTERVAL: float = 60.0

    def __init__(
        self,
        store: EventStore,
        sse_buffer: collections.deque | None = None,
        cursor_base: Path | None = None,
    ):
        super().__init__(store, sse_buffer)
        self._base = cursor_base or default_cursor_base()
        self._parser = CursorParser(cursor_base=self._base)
        self._adapter = CursorAdapter()

    @property
    def provider_name(self) -> str:
        return "cursor"

    def discover_files(self) -> list[Path]:
        global_db = self._base / "globalStorage" / "state.vscdb"
        if global_db.exists():
            return [global_db]
        return []

    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        bubbles, new_offset = self._parser.parse_incremental(path, offset)
        events: list[dict] = []
        seen_sessions: set[str] = set()
        for bubble in bubbles:
            project = (
                bubble.composer.project
                if bubble.composer and bubble.composer.project
                else "cursor"
            )
            event = self._adapter.to_event(bubble, project)
            if event:
                events.append(event.to_dict())
            if bubble.composer_id and bubble.composer_id not in seen_sessions:
                seen_sessions.add(bubble.composer_id)
                meta = self._adapter.to_session_meta(bubble, project)
                if meta:
                    self._store.upsert_session(
                        meta.to_dict(), self._adapter.event_timestamp(bubble)
                    )
        return events, new_offset
