"""Low-level kqueue file watcher for macOS."""

from __future__ import annotations

import os
import select
import sys
from pathlib import Path


class KqueueWatcher:
    """Event-driven file watcher using macOS kqueue. Zero polling overhead."""

    def __init__(self):
        if sys.platform not in ("darwin", "freebsd"):
            raise RuntimeError("kqueue is only available on macOS/BSD")
        self._kq: select.kqueue = select.kqueue()
        self._fd_to_path: dict[int, Path] = {}
        self._path_to_fd: dict[Path, int] = {}

    def register(self, path: Path) -> bool:
        """Register a file for monitoring. Returns True on success."""
        if path in self._path_to_fd:
            return True
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return False

        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
        )
        self._kq.control([ev], 0, 0)
        self._fd_to_path[fd] = path
        self._path_to_fd[path] = fd
        return True

    def unregister(self, path: Path) -> None:
        """Remove a file from monitoring."""
        fd = self._path_to_fd.pop(path, None)
        if fd is not None:
            self._fd_to_path.pop(fd, None)
            try:
                os.close(fd)
            except OSError:
                pass

    def poll(self, timeout: float = 1.0) -> list[Path]:
        """Block up to timeout seconds, return list of changed file paths."""
        if not self._fd_to_path:
            return []
        try:
            events = self._kq.control(None, 32, timeout)
        except OSError:
            return []
        changed: list[Path] = []
        for ev in events:
            path = self._fd_to_path.get(ev.ident)
            if path and path not in changed:
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
        return len(self._fd_to_path)

    def close(self) -> None:
        """Close all file descriptors and the kqueue."""
        for fd in list(self._fd_to_path):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fd_to_path.clear()
        self._path_to_fd.clear()
        try:
            self._kq.close()
        except OSError:
            pass
