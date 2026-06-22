"""Polling-based file watcher — cross-platform fallback."""

from __future__ import annotations

import time
from pathlib import Path


class PollingWatcher:
    """Stat-based file watcher. Same interface as KqueueWatcher but cross-platform.

    Checks file modification times on each poll(). Less efficient than kqueue
    but works on Linux, Windows, and any other platform.
    """

    def __init__(self):
        # path -> last known mtime
        self._watched: dict[Path, float] = {}

    def register(self, path: Path) -> bool:
        """Register a file for monitoring. Returns True on success."""
        if path in self._watched:
            return True
        try:
            self._watched[path] = path.stat().st_mtime
            return True
        except OSError:
            return False

    def unregister(self, path: Path) -> None:
        """Remove a file from monitoring."""
        self._watched.pop(path, None)

    def poll(self, timeout: float = 1.0) -> list[Path]:
        """Sleep up to timeout seconds, then return list of changed file paths.

        Unlike kqueue (which wakes immediately on change), this always sleeps
        the full timeout before checking. Latency is ~timeout seconds.
        """
        time.sleep(timeout)
        changed: list[Path] = []
        for path, old_mtime in list(self._watched.items()):
            try:
                current_mtime = path.stat().st_mtime
            except OSError:
                continue
            if current_mtime != old_mtime:
                self._watched[path] = current_mtime
                changed.append(path)
        return changed

    def register_directory_contents(
        self, directory: Path, glob_pattern: str = "*.jsonl"
    ) -> int:
        """Register all matching files in a directory. Returns count registered."""
        count = 0
        if not directory.is_dir():
            return 0
        for p in directory.rglob(glob_pattern):
            if p.is_file() and self.register(p):
                count += 1
        return count

    @property
    def watched_count(self) -> int:
        return len(self._watched)

    def close(self) -> None:
        """Clean up (nothing to close, but matches KqueueWatcher interface)."""
        self._watched.clear()
