"""Abstract base watcher — unified harvester pattern.

Each provider has ONE loop: discover -> read offset -> parse -> store -> sleep -> repeat.
The live loop only watches files modified within ``MAX_AGE_HOURS``; older
history is ingested by ``mool backfill`` (``hub/backfill.py``) through the same
parse/store path (``harvest_history_file``), never pushed to SSE (#45).
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
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
    # History ingestion (backfill / catch-up) writes at most this many events
    # per transaction, so a huge old file never holds the events.db write lock
    # long enough to starve the live daemon (busy_timeout is 5 s).
    HISTORY_CHUNK_EVENTS: int = 500
    # Startup catch-up (#45): when the last persisted cycle is older than the
    # live window (daemon was down), the first pass also ingests files modified
    # since that cycle — capped at CATCHUP_MAX_DAYS so a long outage never
    # turns a restart into a full backfill. Only for providers whose
    # discover_files() takes ``since`` (the file-based ones).
    CATCHUP: bool = False
    CATCHUP_MAX_DAYS: int = 30
    # Catch-up files harvested per loop iteration, so live files stay fresh.
    CATCHUP_FILES_PER_CYCLE: int = 25
    # Longest single history-ingest transaction seen (seconds) — reported by
    # ``mool backfill`` so lock pressure on events.db is measurable.
    max_history_txn_seconds: float = 0.0

    def __init__(self, store: EventStore, sse_buffer: collections.deque | None = None):
        self._store = store
        self._sse_buffer = sse_buffer  # shared deque for SSE broadcast
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._watched_files: dict[Path, str] = {}  # path -> fingerprint
        # Old files to ingest quietly after a daemon outage (#45), oldest first.
        self._catchup_queue: collections.deque[Path] = collections.deque()
        self.catchup_skipped: list[tuple[str, Path]] = []  # (reason, path)

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier: 'claude', 'codex', 'qwen'."""
        ...

    @abstractmethod
    def discover_files(
        self, since: float | None = None, skip_dir: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        """Find session files modified at/after ``since`` (epoch seconds).

        ``since=None`` means the live window (``_default_cutoff()``);
        ``skip_dir`` is forwarded to ``ProjectDiscovery`` (#45).
        """
        ...

    def _default_cutoff(self) -> float:
        """Oldest mtime the live loop watches: now - MAX_AGE_HOURS."""
        return time.time() - (self.MAX_AGE_HOURS * 3600)

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
        if self.CATCHUP:
            self._plan_catchup()

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

            if self._catchup_queue:
                self._drain_catchup(self.CATCHUP_FILES_PER_CYCLE)

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

        self._record_cycle()

    def _record_cycle(self) -> None:
        """Persist this provider's heartbeat — only once catch-up is drained.

        Advancing it while old files are still queued would lose them if the
        daemon stopped again before draining (the next start would compute the
        catch-up from the fresher heartbeat).
        """
        if not self.CATCHUP or self._catchup_queue:
            return
        try:
            self._store.set_watcher_cycle(self.provider_name, time.time())
        except Exception:
            logging.getLogger("moolmesh.watcher").warning(
                "could not persist %s watcher cycle", self.provider_name, exc_info=True
            )

    def _catchup_cutoff(self, now: float | None = None) -> float | None:
        """mtime cutoff for the startup catch-up, or None when no gap (#45).

        No persisted cycle (fresh install / first run after upgrade) → None:
        the full history is ``mool backfill``'s job, not every startup's.
        """
        now = time.time() if now is None else now
        last = self._store.get_watcher_cycle(self.provider_name)
        if last is None:
            return None
        window_start = now - self.MAX_AGE_HOURS * 3600
        if last >= window_start:
            return None
        floor = now - self.CATCHUP_MAX_DAYS * 86400
        # Back off one rescan interval: files touched between the last rescan
        # and the shutdown are re-read from their offsets (a no-op if done).
        return max(last - self.RESCAN_INTERVAL, floor)

    def _plan_catchup(self) -> None:
        """Queue files modified during the outage that the live window misses."""
        from hub.cloudfiles import PlaceholderSkipper
        try:
            cutoff = self._catchup_cutoff()
            if cutoff is None:
                return
            skipper = PlaceholderSkipper()
            files = self.discover_files(since=cutoff, skip_dir=skipper)
        except Exception:
            logging.getLogger("moolmesh.watcher").warning(
                "%s catch-up planning failed", self.provider_name, exc_info=True
            )
            return
        self.catchup_skipped.extend(("nube (directorio)", d) for d in skipper.skipped)
        window_start = self._default_cutoff()
        old: list[tuple[float, Path]] = []
        for f in files:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < window_start:  # newer files: the live rescan owns them
                old.append((mtime, f))
        old.sort()
        self._catchup_queue.extend(f for _, f in old)
        if old:
            logging.getLogger("moolmesh.watcher").info(
                "%s catch-up: %d files modified while the daemon was down",
                self.provider_name, len(old),
            )

    def _drain_catchup(self, max_files: int) -> None:
        """Ingest up to ``max_files`` queued catch-up files — never to SSE."""
        from hub.cloudfiles import is_cloud_placeholder
        for _ in range(max_files):
            if not self._catchup_queue or not self._running:
                break
            path = self._catchup_queue.popleft()
            if path in self._watched_files:
                continue  # it became live meanwhile; the live loop owns it
            if is_cloud_placeholder(path):
                self.catchup_skipped.append(("nube", path))
                continue
            fp = file_fingerprint(path)
            if not fp:
                continue
            try:
                self.harvest_history_file(path, fp)
            except Exception:
                logging.getLogger("moolmesh.watcher").warning(
                    "%s catch-up failed for %s", self.provider_name, path, exc_info=True
                )
        if not self._catchup_queue:
            self._record_cycle()

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
        except Exception:
            logging.getLogger("moolmesh.watcher").warning(
                "harvest error in %s: %s", path, __import__("traceback").format_exc()
            )
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

    def harvest_history_file(self, path: Path, fingerprint: str) -> tuple[int, int]:
        """Ingest one historical file from its stored offset — no SSE (#45).

        Same parse path as the live loop (``_parse_and_adapt``), but events are
        written in ``HISTORY_CHUNK_EVENTS``-sized transactions: intermediate
        chunks carry no fingerprint (no offset write) and only the last chunk
        persists the new offset. An interruption therefore leaves the old
        offset in place and the next run re-reads the file; ``INSERT OR
        IGNORE`` on the event fingerprint drops what was already stored.

        Session stats (``event_count``, first/last event) are refreshed after
        the store: the watchers upsert session metadata before the events land,
        which a single-pass ingest would otherwise leave stale.

        Returns ``(events_parsed, events_inserted)``. Parse errors propagate.
        """
        offset = self._store.get_offset(fingerprint) or 0
        try:
            events, new_offset = self._parse_and_adapt(path, offset)
        finally:
            # A history walk parses each file once: drop per-file parser
            # state (Codex keeps session_meta context per path) to bound memory.
            ctx = getattr(getattr(self, "_parser", None), "_session_ctx", None)
            if isinstance(ctx, dict):
                ctx.pop(str(path), None)
        if new_offset == offset and not events:
            return 0, 0

        inserted = 0
        step = max(1, self.HISTORY_CHUNK_EVENTS)
        chunks = [events[i:i + step] for i in range(0, len(events), step)] or [[]]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            t0 = time.monotonic()
            stored = self._store.store_with_offset(
                chunk, fingerprint if last else "", self.provider_name,
                str(path), new_offset,
            )
            self.max_history_txn_seconds = max(
                self.max_history_txn_seconds, time.monotonic() - t0
            )
            inserted += len(stored)

        session_ids = {e.get("session_id") for e in events if e.get("session_id")}
        if session_ids:
            self._store.refresh_session_stats(self.provider_name, session_ids)
        return len(events), inserted

    @property
    def watched_count(self) -> int:
        return len(self._watched_files)


# Backwards compatibility
BaseWatcher = BaseHarvester
