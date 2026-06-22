"""Abstract base watcher — unified harvester pattern.

Each provider has ONE loop: discover -> read offset -> parse -> store -> sleep -> repeat.
No queue, no dispatcher, no backfill. The first cycle IS the backfill.
"""

from __future__ import annotations

import collections
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from hub.cache.event_store import EventStore, file_fingerprint


class BaseHarvester(ABC):
    """Base class for provider-specific harvesters.

    Subclasses implement:
        - discover_files() -> list[Path]
        - _parse_and_adapt(path, offset) -> tuple[list[dict], int]
        - provider_name -> str (property)
    """

    # How often to poll for changes (seconds)
    POLL_INTERVAL: float = 2.0
    # How often to rescan for new files (seconds)
    RESCAN_INTERVAL: float = 30.0
    # Max age of files to watch (hours)
    MAX_AGE_HOURS: int = 12

    def __init__(self, store: EventStore, sse_buffer: collections.deque | None = None):
        self._store = store
        self._sse_buffer = sse_buffer  # shared deque for SSE broadcast
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._watched_files: dict[Path, str] = {}  # path -> fingerprint

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier: 'claude', 'codex', 'qwen'."""
        ...

    @abstractmethod
    def discover_files(self) -> list[Path]:
        """Find all session files for this provider within MAX_AGE_HOURS."""
        ...

    @abstractmethod
    def _parse_and_adapt(self, path: Path, offset: int) -> tuple[list[dict], int]:
        """Parse file from offset, return (event_dicts, new_offset).

        Uses the parser's chunk-and-tail parse_incremental() and the adapter's
        to_event(). Returns event dicts ready for store_with_offset().
        """
        ...

    def start(self) -> None:
        """Start harvesting in a daemon thread."""
        self._running = True
        self._thread = threading.Thread(target=self._harvest_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _harvest_loop(self) -> None:
        """Main loop: discover, read, parse, store, sleep, repeat."""
        last_rescan = 0.0

        while self._running:
            now = time.monotonic()

            # Rescan for new/removed files periodically
            if now - last_rescan >= self.RESCAN_INTERVAL:
                self._rescan()
                last_rescan = now

            # Process all watched files
            for path, fingerprint in list(self._watched_files.items()):
                if not self._running:
                    break
                self._harvest_file(path, fingerprint)

            # Sleep between cycles
            time.sleep(self.POLL_INTERVAL)

    def _rescan(self) -> None:
        """Discover files, register new ones, unregister stale ones."""
        try:
            current_files = self.discover_files()
        except OSError:
            return

        current_set = set(current_files)
        watched_set = set(self._watched_files.keys())

        # Register new files
        for path in current_set - watched_set:
            fp = file_fingerprint(path)
            if fp:
                self._watched_files[path] = fp

        # Unregister stale files (outside time window)
        for path in watched_set - current_set:
            # Harvest any remaining data before dropping
            fp = self._watched_files.get(path)
            if fp:
                self._harvest_file(path, fp)
            self._watched_files.pop(path, None)

    def _harvest_file(self, path: Path, fingerprint: str) -> None:
        """Read new data from one file, store atomically."""
        # Get offset from SQLite (persistent across restarts)
        offset = self._store.get_offset(fingerprint)
        if offset is None:
            offset = 0  # New file — read from beginning (this IS the backfill)

        try:
            events, new_offset = self._parse_and_adapt(path, offset)
        except OSError:
            return

        if new_offset == offset and not events:
            return  # No new data

        # Atomic: store events + update offset in one transaction
        # Returns only newly-inserted events with their SQLite IDs
        stored = self._store.store_with_offset(
            events, fingerprint, self.provider_name, str(path), new_offset
        )

        # Push stored events (with IDs) to SSE buffer for broadcast
        if self._sse_buffer is not None and stored:
            for ev in stored:
                self._sse_buffer.append(ev)

    @property
    def watched_count(self) -> int:
        return len(self._watched_files)


# Backwards compatibility
BaseWatcher = BaseHarvester
