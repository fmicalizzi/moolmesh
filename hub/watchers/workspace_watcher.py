"""Filesystem workspace watcher — MoolMesh #21 (Workspace axis, Phase B).

Makes a project visible even when *no agent and no git* ever touched it: a
materials-gathering folder, a non-CLI tool whose output is only observable by
path (e.g. Pencil ``.pen`` files), or non-software work. It observes **marked
roots** (opt-in) and emits a *path-touch* for every file that changed since the
last scan, attributed to its owning workspace via the Phase A resolver (#20)
and written to ``workspace.db`` — never ``events.db``.

Design (mirrors the session watchers' ``discover → cursor → emit → sleep`` loop,
``watchers/base.py``, but is a standalone class — it never imports
``EventStore`` or touches the SSE hot path):

  * **Mechanism:** bounded recursive polling + an **mtime cursor** per root.
    NO watch-per-file / inotify-per-file, so there is no file-descriptor ceiling.
  * **Cursor (incremental, no re-emit):** each scan emits files with
    ``mtime > cursor``, then advances the cursor to ``scan_start - 1s`` (not
    ``max(mtime)`` — see ``WorkspaceStore.set_cursor``). First scan (cursor 0)
    emits everything under the root: the first cycle IS the backfill.
  * **Containment = correctness:** built-in default excludes (VCS internals,
    dependency dirs, build outputs, sync/cache folders) pruned *in-place* during
    the walk so we never descend into them, plus a root's extra excludes.
    Bounded by ``max_depth``. Symlinked dirs are not followed (loop-safe; root
    ``/`` is a supported case).

Zero dependencies (stdlib only). Read-only over the observed filesystem — it
only ``lstat``s files; it never modifies the user's files.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

from hub.config import WorkspaceRoot
from hub.correlation.workspace_resolver import WorkspaceIdentity, resolve_dir
from hub.log import get as get_logger

_log = get_logger("WorkspaceWatcher")


# Directory *basenames* pruned everywhere (matched on the folder name). These are
# the sources that drown the signal or carry no project meaning: VCS internals,
# dependency trees, build outputs, and OS/cloud sync + cache folders.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    # VCS internals
    ".git", ".svn", ".hg", ".bzr", "CVS",
    # dependency dirs
    "node_modules", ".venv", "venv", "env", "__pycache__", "vendor",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle",
    ".cargo", "bower_components", ".yarn", ".pnpm-store", "Pods",
    # build outputs
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit",
    ".parcel-cache", ".turbo", "coverage", ".terraform",
    # OS / cloud sync + caches (these otherwise flood the touch stream)
    ".cache", "Cache", "Caches", ".Trash", ".Trashes", ".Spotlight-V100",
    ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems",
    "Dropbox", "OneDrive", ".dropbox.cache",
    "Mobile Documents", "com~apple~CloudDocs",
})

# Absolute paths pruned only when a root scan reaches them — relevant to the
# root ``/`` (autonomous-agent server) case. Matched by full path, not basename,
# so a project's own ``dev/`` folder is never mistaken for ``/dev``.
DEFAULT_EXCLUDE_ABS: frozenset[str] = frozenset({
    "/proc", "/sys", "/dev", "/private/var/vm", "/System/Volumes",
})


class WorkspaceWatcher:
    """Polls marked filesystem roots and records path-touches to workspace.db.

    Opt-in: with an empty ``roots`` list it observes nothing. Mirrors the
    session harvesters' lifecycle (``start``/``stop``, daemon thread,
    ``watched_count``) without any coupling to the events hot path.
    """

    # Seconds between full scans of every marked root.
    SCAN_INTERVAL: float = 30.0

    def __init__(self, store, roots: list[WorkspaceRoot]):
        self._store = store
        self._roots = list(roots)
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._touch_count: int = 0

    # --- lifecycle (mirrors BaseHarvester) ---

    def start(self) -> None:
        """Start scanning in a daemon thread. No-op if no roots are marked."""
        if not self._roots:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def watched_count(self) -> int:
        return len(self._roots)

    @property
    def touch_count(self) -> int:
        return self._touch_count

    # --- loop ---

    def _scan_loop(self) -> None:
        while self._running:
            swept = 0
            for root in self._roots:
                if not self._running:
                    break
                try:
                    swept += self.scan_root(root)
                except Exception:  # noqa: BLE001 — one bad root must not kill the loop
                    _log.warning("scan error on root %s", root.path, exc_info=True)
            # Refresh the portfolio rollup (issue #22) only when the sweep found
            # new touches — build_rollup re-reads github.db, so we don't fire it
            # every idle 30s cycle (gate on real change, not the clock).
            if swept > 0:
                try:
                    self._store.build_rollup()
                    self._store.detect_delivery_candidates()
                except Exception:  # noqa: BLE001
                    _log.warning("rollup/delivery build failed", exc_info=True)
            time.sleep(self.SCAN_INTERVAL)

    # --- one root scan (also the unit-test entry point) ---

    def scan_root(self, root: WorkspaceRoot) -> int:
        """Scan one marked root once; return the number of touches emitted.

        Emits files with ``mtime > cursor`` and then advances the cursor to
        ``scan_start - 1s`` so files written mid-scan are re-seen next cycle
        instead of being skipped (the ``max(mtime)`` cursor bug). Re-emitting a
        path is harmless — ``record_touch`` upserts on ``path``.
        """
        root_path = root.path
        if not os.path.isdir(root_path):
            return 0

        cursor = self._store.get_cursor(root_path)
        scan_start = time.time()
        excludes = DEFAULT_EXCLUDE_DIRS | {e for e in root.excludes if e}
        max_depth = int(root.max_depth)

        dir_cache: dict[str, WorkspaceIdentity] = {}
        emitted = 0

        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            depth = self._depth(root_path, dirpath)

            # Prune in-place so we never descend into excluded / too-deep dirs.
            dirnames[:] = [
                d for d in dirnames
                if d not in excludes
                and os.path.join(dirpath, d) not in DEFAULT_EXCLUDE_ABS
            ]
            if depth >= max_depth:
                dirnames[:] = []

            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(fpath)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue  # skip symlinks (loop-safe) and non-regular files
                if st.st_mtime <= cursor:
                    continue

                ident = dir_cache.get(dirpath)
                if ident is None:
                    ident = resolve_dir(dirpath)
                    dir_cache[dirpath] = ident
                try:
                    self._store.record_touch(fpath, st.st_mtime, ident)
                except Exception:  # noqa: BLE001
                    _log.warning("record_touch failed for %s", fpath, exc_info=True)
                    continue
                emitted += 1

        # Anchor to the scan start, not the newest mtime seen (see docstring).
        self._store.set_cursor(root_path, scan_start - 1.0)
        self._touch_count += emitted
        return emitted

    @staticmethod
    def _depth(root_path: str, dirpath: str) -> int:
        """Depth of ``dirpath`` relative to the root (root itself = 0)."""
        try:
            rel = Path(dirpath).relative_to(root_path)
        except ValueError:
            return 0
        return 0 if rel == Path(".") else len(rel.parts)
