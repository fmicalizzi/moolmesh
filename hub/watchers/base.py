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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hub.cache.event_store import EventStore, file_fingerprint, registry_path

_log = logging.getLogger("moolmesh.watcher")


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
    # SQLite-backed providers (OpenCode, Cursor) tail ONE database whose first
    # KB (the SQLite header) changes on writes, so a content fingerprint would
    # key every daemon start differently and re-read from rowid 0. They set
    # this and are keyed by ``<provider>:<path>`` instead (#50).
    STABLE_KEY: bool = False
    # Resilience (issue #65): a file that fails this many consecutive cycles is
    # quarantined (with backoff) so one poisoned file cannot occupy every poll;
    # it is retried after the backoff and reported via ``quarantined_files``.
    QUARANTINE_AFTER: int = 5
    QUARANTINE_BACKOFF: float = 60.0
    # A watcher with no completed cycle for this many RESCAN_INTERVALs is
    # reported as stalled by /health (never a silent outage).
    HEALTH_STALL_FACTOR: float = 3.0

    def __init__(self, store: EventStore, sse_buffer: collections.deque | None = None):
        self._store = store
        self._sse_buffer = sse_buffer  # shared deque for SSE broadcast
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._watched_files: dict[Path, str] = {}  # path -> fingerprint
        # Old files to ingest quietly after a daemon outage (#45), oldest first.
        self._catchup_queue: collections.deque[Path] = collections.deque()
        self.catchup_skipped: list[tuple[str, Path]] = []  # (reason, path)
        # Health/resilience state (issue #65). Paths are never put in a
        # message: last_error carries only the exception type + the file, and
        # the file is masked at snapshot time under hide_project_names.
        self._started_at: float | None = None
        self._last_cycle_at: float | None = None
        self._last_error: dict[str, str] | None = None
        self._file_failures: dict[Path, int] = {}
        self._quarantine_until: dict[Path, float] = {}
        self._logged_file_error: dict[Path, str] = {}
        self._logged_loop_error: str | None = None

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

    def registry_key(self, path: Path) -> str:
        """``file_registry`` key of ``path`` ('' when unreadable).

        Content fingerprint of the first KB by default (survives renames);
        ``<provider>:<path>`` for ``STABLE_KEY`` providers. Together with the
        path it identifies the file (#50).
        """
        if self.STABLE_KEY:
            return f"{self.provider_name}:{registry_path(path)}"
        return file_fingerprint(path)

    def _stored_offset(self, key: str, path: Path) -> int | None:
        """Stored offset of ``path``; a new stable key resumes from the old rows."""
        offset = self._store.get_offset(key, str(path))
        if offset is None and self.STABLE_KEY:
            offset = self._store.latest_offset_for_path(self.provider_name, str(path))
        return offset

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
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._harvest_loop, name=f"{self.provider_name}-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def alive(self) -> bool:
        """True while the harvesting thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def running(self) -> bool:
        """True while the watcher was started and not stopped."""
        return self._running

    @property
    def last_cycle_at(self) -> float | None:
        """Wall-clock epoch of the last completed loop iteration."""
        return self._last_cycle_at

    @property
    def last_error(self) -> dict[str, str] | None:
        """Last error as ``{"type": ..., "file": ...}`` (never a message)."""
        return self._last_error

    @property
    def quarantined_files(self) -> int:
        """Files currently in quarantine (failures >= N, backoff not expired)."""
        now = time.time()
        return sum(1 for until in self._quarantine_until.values() if until > now)

    def status_snapshot(self, *, hide_project_names: bool = False) -> dict[str, Any]:
        """Health snapshot for /health and ``mool daemon status`` (issue #65).

        Additive: ``alive`` (thread lives), ``stalled`` (no completed cycle in
        ``HEALTH_STALL_FACTOR × RESCAN_INTERVAL``), ``last_cycle_at`` (ISO),
        ``last_error`` (exception TYPE + file, never a message; the file is
        masked when ``hide_project_names`` is on) and ``quarantined_files``.
        """
        now = time.time()
        last_cycle = self._last_cycle_at
        stalled = False
        if self._running:
            reference = last_cycle if last_cycle is not None else self._started_at
            if reference is not None:
                stalled = (now - reference) > self.HEALTH_STALL_FACTOR * self.RESCAN_INTERVAL
        last_error = None
        if self._last_error:
            path = self._last_error.get("file") or ""
            if path and hide_project_names:
                from hub.config import masked_label
                path = masked_label(path, True)
            last_error = {"type": self._last_error.get("type", ""), "file": path}
        return {
            "alive": self.alive,
            "running": self._running,
            "stalled": stalled,
            "last_cycle_at": (
                datetime.fromtimestamp(last_cycle, tz=timezone.utc).isoformat()
                if last_cycle is not None else None
            ),
            "last_error": last_error,
            "quarantined_files": self.quarantined_files,
        }

    def note_thread_death(self) -> None:
        """Record that the harvesting thread died (called by the supervisor)."""
        if not self._last_error:
            self._last_error = {"type": "ThreadDied", "file": ""}

    def _harvest_loop(self) -> None:
        """Main loop: discover, read, parse, store, sleep, repeat.

        The whole iteration is guarded: no exception — from discovery, a
        provider parser, the store or the catch-up queue — may kill the
        thread and leave the provider silently unharvested (#65). Per-file
        failures are handled in ``_harvest_file``; only truly unexpected
        failures land here, logged once per error type.
        """
        last_rescan = 0.0
        if self.CATCHUP:
            self._plan_catchup()

        while self._running:
            try:
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
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                self._note_loop_error(exc)

            self._last_cycle_at = time.time()
            # Sleep between cycles
            time.sleep(self.POLL_INTERVAL)

    def _note_loop_error(self, exc: BaseException) -> None:
        """Record a loop-level failure, logging once per error type."""
        err_type = type(exc).__name__
        self._last_error = {"type": err_type, "file": ""}
        if self._logged_loop_error != err_type:
            self._logged_loop_error = err_type
            _log.warning(
                "%s harvest loop iteration failed (%s); retrying next cycle",
                self.provider_name, err_type, exc_info=exc,
            )

    def _note_file_error(self, path: Path, exc: BaseException) -> None:
        """Record a per-file failure; log once per (file, error type) (#65)."""
        err_type = type(exc).__name__
        self._last_error = {"type": err_type, "file": str(path)}
        failures = self._file_failures.get(path, 0) + 1
        self._file_failures[path] = failures
        if self._logged_file_error.get(path) != err_type:
            self._logged_file_error[path] = err_type
            _log.warning(
                "%s harvest error (%s) in %s; will retry next cycle",
                self.provider_name, err_type, path, exc_info=exc,
            )
        if failures >= self.QUARANTINE_AFTER:
            self._quarantine_until[path] = time.time() + self.QUARANTINE_BACKOFF
            if failures == self.QUARANTINE_AFTER:
                _log.warning(
                    "%s quarantined %s for %.0fs after %d consecutive failures",
                    self.provider_name, path, self.QUARANTINE_BACKOFF, failures,
                )

    def _clear_file_error(self, path: Path) -> None:
        """A file recovered: forget its failures/quarantine/log-once mark."""
        self._file_failures.pop(path, None)
        self._quarantine_until.pop(path, None)
        self._logged_file_error.pop(path, None)
        if self._last_error and self._last_error.get("file") == str(path):
            self._last_error = None

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
            fp = self.registry_key(path)
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
            _log.warning(
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
            _log.warning(
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
            _log.info(
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
            fp = self.registry_key(path)
            if not fp:
                continue
            try:
                self.harvest_history_file(path, fp)
                self._clear_file_error(path)
            except OSError:
                continue  # vanished mid-drain: not a poisoned file
            except Exception as exc:  # noqa: BLE001 — one file never stops the drain
                self._note_file_error(path, exc)
        if not self._catchup_queue:
            self._record_cycle()

    def _harvest_file(self, path: Path, fingerprint: str) -> None:
        """Read new data from one file, store atomically.

        Never raises (issue #65): a get_offset / parse / store failure is
        recorded and logged once per (file, error type) and retried next
        cycle. After ``QUARANTINE_AFTER`` consecutive failures the file is
        skipped for ``QUARANTINE_BACKOFF`` seconds so it cannot occupy every
        poll; it is retried afterwards and reported in the health snapshot.
        """
        if self._quarantine_until.get(path, 0.0) > time.time():
            return  # in backoff; retried when it expires

        # Get offset from SQLite (persistent across restarts)
        try:
            offset = self._stored_offset(fingerprint, path)
        except OSError:
            return  # unreadable file (e.g. vanished): not a data error
        except Exception as exc:  # noqa: BLE001
            self._note_file_error(path, exc)
            return
        if offset is None:
            offset = 0  # New file — read from beginning (this IS the backfill)

        try:
            events, new_offset = self._parse_and_adapt(path, offset)
        except OSError:
            return
        except Exception as exc:  # noqa: BLE001
            self._note_file_error(path, exc)
            return

        if new_offset == offset and not events:
            self._clear_file_error(path)
            return  # No new data

        # Atomic: store events + update offset in one transaction
        # Returns only newly-inserted events with their SQLite IDs
        try:
            stored = self._store.store_with_offset(
                events, fingerprint, self.provider_name, str(path), new_offset
            )
        except Exception as exc:  # noqa: BLE001
            self._note_file_error(path, exc)
            return
        self._clear_file_error(path)

        # Push stored events (with IDs) to SSE buffer for broadcast
        if self._sse_buffer is not None and stored:
            for ev in stored:
                self._sse_buffer.append(ev)

    def harvest_history_file(
        self, path: Path, fingerprint: str, offset: int | None = None
    ) -> tuple[int, int]:
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

        ``offset`` overrides the stored one (``mool backfill`` passes 0 to
        re-read a file whose offset was shared with another file, #50).

        Returns ``(events_parsed, events_inserted)``. Parse errors propagate.
        """
        if offset is None:
            offset = self._stored_offset(fingerprint, path) or 0
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
                str(path), new_offset, historical=True,
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
