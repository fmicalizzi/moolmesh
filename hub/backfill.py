"""Historical session ingestion — ``mool backfill`` (issue #45).

The live watchers only tail files modified within ``MAX_AGE_HOURS`` (12 h), so
history from before the install — or from a daemon outage longer than the
window — was never read. ``run_backfill`` walks ALL session files of the
file-based providers (claude, codex, qwen; opencode/cursor read a whole SQLite
DB and are unaffected) and ingests them through the watcher's own path:
``_parse_and_adapt`` → ``store_with_offset`` with offsets in ``file_registry``.

* Resumable: each file restarts from its stored offset (0 new bytes = no-op),
  so an interrupted run simply continues on the next invocation.
* Idempotent: the event fingerprint + ``INSERT OR IGNORE``.
* No clash with a running daemon: only files OLDER than the live window are
  processed; the daemon owns everything newer.
* No SSE: this is a separate process and never touches ``sse_buffer``.
* Zero-cloud: cloud-only placeholders are skipped (never opened) and
  reported, and dataless directories are never listed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from hub.cache.event_store import EventStore, file_fingerprint
from hub.cloudfiles import PlaceholderSkipper, is_cloud_placeholder

_log = logging.getLogger("moolmesh.backfill")

FILE_PROVIDERS = ("claude", "codex", "qwen")


def backfill(store: EventStore | None = None, **kwargs) -> dict[str, int]:
    """Legacy no-op stub kept for scripts importing it; use ``run_backfill``."""
    return {"claude": 0, "codex": 0, "qwen": 0, "total": 0}


def gap_fill(store: EventStore | None = None, **kwargs) -> dict[str, int]:
    """Legacy no-op stub kept for scripts importing it; use ``run_backfill``."""
    return {"claude": 0, "codex": 0, "qwen": 0, "total": 0}


@dataclass
class ProviderReport:
    provider: str
    seen: int = 0              # files discovered (any age, after --since)
    processed: int = 0         # files that yielded new data (or would, dry-run)
    up_to_date: int = 0        # offset already at the end of the file
    in_window: int = 0         # inside the daemon's live window — left to it
    skipped_cloud: int = 0     # cloud-only placeholders (never opened)
    skipped_error: int = 0     # unreadable / failed to parse
    skipped_empty: int = 0     # 0-byte files
    cloud_dirs: int = 0        # dataless directories not listed
    fingerprint_collisions: int = 0  # distinct files sharing a 1 KB fingerprint
    pending_bytes: int = 0     # bytes past the stored offsets (processed files)
    events_parsed: int = 0
    events_inserted: int = 0
    sessions_before: int = 0
    sessions_after: int = 0
    max_txn_seconds: float = 0.0
    limit_reached: bool = False
    skipped_paths: list[tuple[str, str]] = field(default_factory=list)  # (reason, path)
    processed_paths: list[str] = field(default_factory=list)

    @property
    def new_sessions(self) -> int:
        return max(0, self.sessions_after - self.sessions_before)


@dataclass
class BackfillReport:
    providers: list[ProviderReport] = field(default_factory=list)
    dry_run: bool = False
    window_hours: int = 12
    elapsed: float = 0.0
    interrupted: bool = False


class _ReadOnlyOffsets:
    """Offset lookups for ``--dry-run`` without opening events.db for writing.

    ``EventStore()`` creates tables and applies migrations on open, which a
    dry-run must not do; this reads ``file_registry`` through ``mode=ro``.
    """

    def __init__(self, db_path: Path):
        self._conn: sqlite3.Connection | None = None
        if db_path.exists():
            try:
                self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                self._conn.execute("SELECT 1 FROM file_registry LIMIT 1")
            except sqlite3.Error:
                self._conn = None

    def get_offset(self, fingerprint: str) -> int | None:
        if self._conn is None or not fingerprint:
            return None
        row = self._conn.execute(
            "SELECT last_offset FROM file_registry WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row[0] if row else None

    def session_count(self, provider: str) -> int:
        if self._conn is None:
            return 0
        try:
            return self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE provider = ?", (provider,)
            ).fetchone()[0]
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


def make_watcher(provider: str, store: EventStore | None, bases: dict[str, Path | None]):
    """Build the provider's watcher (no SSE buffer) — the ingestion path owner."""
    if provider == "claude":
        from hub.watchers.claude_watcher import ClaudeWatcher
        return ClaudeWatcher(store, None, claude_base=bases.get("claude"))
    if provider == "codex":
        from hub.watchers.codex_watcher import CodexWatcher
        return CodexWatcher(store, None, codex_base=bases.get("codex"))
    if provider == "qwen":
        from hub.watchers.qwen_watcher import QwenWatcher
        return QwenWatcher(store, None, qwen_base=bases.get("qwen"))
    raise ValueError(f"unsupported provider for backfill: {provider}")


def _session_count(store: EventStore, provider: str) -> int:
    with store._lock:
        return store._get_conn().execute(
            "SELECT COUNT(*) FROM sessions WHERE provider = ?", (provider,)
        ).fetchone()[0]


def _sorted_by_mtime(files: Iterable[Path]) -> list[tuple[Path, os.stat_result | None]]:
    """Deterministic order (oldest first, then path) with the stat reused."""
    out: list[tuple[Path, os.stat_result | None]] = []
    for f in files:
        try:
            out.append((f, f.stat()))
        except OSError:
            out.append((f, None))
    out.sort(key=lambda t: (t[1].st_mtime if t[1] else 0.0, str(t[0])))
    return out


def run_backfill(
    store: EventStore | None,
    providers: Iterable[str] = FILE_PROVIDERS,
    since: float | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    db_path: Path | None = None,
    bases: dict[str, Path | None] | None = None,
    now: float | None = None,
    progress: Callable[[ProviderReport], None] | None = None,
    report: BackfillReport | None = None,
) -> BackfillReport:
    """Ingest every historical session file older than the live window.

    ``store`` is required unless ``dry_run`` (a dry-run reads offsets from
    ``db_path`` read-only and writes nothing). ``limit`` caps the number of
    files that actually carry new bytes, so a re-run with the same limit
    advances instead of re-visiting up-to-date files. Pass a ``report`` to have
    it filled in place (the CLI keeps the partial totals on Ctrl-C).
    """
    from hub.cache.event_store import DEFAULT_DB_PATH

    t_start = time.monotonic()
    bases = bases or {}
    now = time.time() if now is None else now
    report = report if report is not None else BackfillReport()
    report.dry_run = dry_run
    ro = _ReadOnlyOffsets(db_path or DEFAULT_DB_PATH) if dry_run else None
    if not dry_run and store is None:
        raise ValueError("run_backfill needs a store unless dry_run")
    remaining = limit if limit and limit > 0 else None

    try:
        for provider in providers:
            watcher = make_watcher(provider, None if dry_run else store, bases)
            report.window_hours = watcher.MAX_AGE_HOURS
            window_start = now - watcher.MAX_AGE_HOURS * 3600
            rep = ProviderReport(provider=provider)
            report.providers.append(rep)
            rep.sessions_before = (
                ro.session_count(provider) if ro else _session_count(store, provider)
            )
            offsets = ro if ro else store

            skipper = PlaceholderSkipper()
            try:
                files = watcher.discover_files(since=since or 0.0, skip_dir=skipper)
            except OSError:
                _log.warning("backfill: discovery failed for %s", provider, exc_info=True)
                files = []
            rep.cloud_dirs = len(skipper.skipped)
            rep.skipped_paths.extend(("nube (directorio)", str(d)) for d in skipper.skipped)
            seen_fp: dict[str, Path] = {}

            for path, st in _sorted_by_mtime(files):
                rep.seen += 1
                if st is None:
                    rep.skipped_error += 1
                    rep.skipped_paths.append(("error", str(path)))
                    continue
                if st.st_mtime >= window_start:
                    rep.in_window += 1
                    continue
                if is_cloud_placeholder(path):
                    rep.skipped_cloud += 1
                    rep.skipped_paths.append(("nube", str(path)))
                    continue
                if st.st_size == 0:
                    rep.skipped_empty += 1
                    continue
                fp = file_fingerprint(path)
                if not fp:
                    rep.skipped_error += 1
                    rep.skipped_paths.append(("error", str(path)))
                    continue
                if fp in seen_fp and seen_fp[fp] != path:
                    # Same first 1 KB as another file: both share ONE offset row
                    # (a pre-existing file_registry limitation). Counted so it is
                    # visible; processed like the live watcher would.
                    rep.fingerprint_collisions += 1
                seen_fp.setdefault(fp, path)

                offset = offsets.get_offset(fp) or 0
                if offset >= st.st_size:
                    rep.up_to_date += 1
                    continue
                if remaining is not None and remaining <= 0:
                    rep.limit_reached = True
                    break

                if dry_run:
                    rep.processed += 1
                    rep.pending_bytes += st.st_size - offset
                    rep.processed_paths.append(str(path))
                    if remaining is not None:
                        remaining -= 1
                    continue

                try:
                    parsed, inserted = watcher.harvest_history_file(path, fp)
                except Exception:
                    # One bad file never aborts the run: log it and move on.
                    _log.warning("backfill: failed to ingest %s", path, exc_info=True)
                    rep.skipped_error += 1
                    rep.skipped_paths.append(("error", str(path)))
                    continue
                if parsed == 0 and inserted == 0 and (offsets.get_offset(fp) or 0) == offset:
                    # Only a trailing partial line past the offset: nothing to do yet.
                    rep.up_to_date += 1
                    continue
                rep.processed += 1
                rep.pending_bytes += st.st_size - offset
                rep.events_parsed += parsed
                rep.events_inserted += inserted
                rep.processed_paths.append(str(path))
                if remaining is not None:
                    remaining -= 1
                if progress and rep.processed % 100 == 0:
                    progress(rep)

            rep.max_txn_seconds = watcher.max_history_txn_seconds
            rep.sessions_after = (
                ro.session_count(provider) if ro else _session_count(store, provider)
            )
            if progress:
                progress(rep)
            if remaining is not None and remaining <= 0 and rep.limit_reached:
                break
    except KeyboardInterrupt:
        # A chunk may have been mid-transaction: roll it back before anything
        # else can commit it (the offset of that file stays at its old value).
        if store is not None:
            conn = store._get_conn()
            if conn.in_transaction:
                conn.rollback()
        report.interrupted = True
        if store is not None and report.providers:
            last = report.providers[-1]
            last.sessions_after = _session_count(store, last.provider)
    finally:
        if ro:
            ro.close()
        report.elapsed = time.monotonic() - t_start
    return report
