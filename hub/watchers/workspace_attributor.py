"""Scheduled session→workspace attribution — MoolMesh #39.

The portfolio is not built from ``events.db`` directly: it is built from the
``path_attributions`` pass plus the derived rollup / delivery-candidate /
classification tables in ``workspace.db``. Before #39 nothing scheduled that
pass (only ``mool workspace backfill``), so ``/portfolio`` silently drifted
stale. This background thread runs it on the daemon's cycle:

  * **Incremental attribution** (``WorkspaceStore.attribute_incremental``) over
    ``events.id > cursor`` — events.db is only ever opened ``mode=ro``.
  * **Refresh** of the derived tables (``build_rollup`` →
    ``detect_delivery_candidates`` → ``classify_workspaces``, the CLI order)
    when the pass attributed something OR the last refresh is older than
    ``refresh_max_age`` — the rollup also folds ``path_touches`` and github.db
    commits, so gating only on attributions would leave fs/git activity stale.

Standalone like ``WorkspaceWatcher``: it never imports ``EventStore`` as a
writer and never touches the SSE buffer / hot path. Waits use
``threading.Event.wait`` (never ``time.sleep``) so ``stop()`` returns promptly.
Zero dependencies (stdlib only).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hub.log import get as get_logger

_log = get_logger("WorkspaceAttributor")


class WorkspaceAttributor:
    """Periodically attributes new sessions to workspaces and refreshes the
    portfolio's derived tables. ``run_once()`` is the unit-test entry point."""

    # Seconds between cycles.
    INTERVAL: float = 300.0
    # Delay before the first cycle, so it does not compete with daemon startup.
    INITIAL_DELAY: float = 30.0
    # Refresh derived tables at least this often even with no new attributions.
    REFRESH_MAX_AGE: float = 3600.0

    def __init__(
        self,
        store,
        events_db_path: str | Path,
        github_db_path: str | Path | None = None,
        *,
        interval: float | None = None,
        initial_delay: float | None = None,
        refresh_max_age: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._store = store
        self._events_db_path = events_db_path
        self._github_db_path = github_db_path
        self._interval = self.INTERVAL if interval is None else interval
        self._initial_delay = (
            self.INITIAL_DELAY if initial_delay is None else initial_delay
        )
        self._refresh_max_age = (
            self.REFRESH_MAX_AGE if refresh_max_age is None else refresh_max_age
        )
        self._clock = clock
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # None → never refreshed in this process: the first cycle refreshes.
        self._last_refresh: float | None = None
        # Set when attributions landed; cleared only by a SUCCESSFUL refresh, so
        # a refresh that fails after the cursor advanced is retried next cycle.
        self._refresh_pending: bool = False

        # Freshness surface (read by the dashboard meta endpoint, #39).
        self.last_attribution_at: str | None = None
        self.last_attribution_error: str | None = None
        self.last_result: dict[str, Any] | None = None

    # --- lifecycle (mirrors WorkspaceWatcher / BaseHarvester) ---

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="WorkspaceAttributor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        if self._stop_event.wait(self._initial_delay):
            return
        while not self._stop_event.is_set():
            self.run_once()
            if self._stop_event.wait(self._interval):
                return

    # --- one cycle ---

    def run_once(self) -> dict[str, Any]:
        """Run one attribution (+ gated refresh) cycle. Never raises.

        On failure the error is logged to hub.log with its traceback and only the
        exception TYPE is kept for the meta endpoint — messages can carry paths,
        which would leak folder names past ``hide_project_names``.
        """
        started = self._clock()
        try:
            res = self._store.attribute_incremental(self._events_db_path)
        except Exception as exc:  # noqa: BLE001 — the loop must survive any failure
            _log.error("workspace attribution cycle failed", exc_info=True)
            self.last_attribution_error = type(exc).__name__
            self.last_result = {"ok": False, "stage": "attribution"}
            return self.last_result

        attributed = int(res.get("attributed", 0))
        if attributed > 0:
            self._refresh_pending = True
        self.last_attribution_at = datetime.now(timezone.utc).isoformat()

        refreshed = False
        now = self._clock()
        due = (
            self._refresh_pending
            or self._last_refresh is None
            or now - self._last_refresh >= self._refresh_max_age
        )
        if due:
            try:
                self._store.build_rollup(self._github_db_path)
                self._store.detect_delivery_candidates(
                    self._events_db_path, self._github_db_path
                )
                self._store.classify_workspaces(self._events_db_path)
            except Exception as exc:  # noqa: BLE001
                _log.error("workspace portfolio refresh failed", exc_info=True)
                self.last_attribution_error = type(exc).__name__
                self.last_result = {
                    "ok": False, "stage": "refresh", "attributed": attributed,
                }
                return self.last_result
            self._last_refresh = self._clock()
            self._refresh_pending = False
            refreshed = True

        self.last_attribution_error = None
        elapsed = self._clock() - started
        _log.info(
            "workspace attribution cycle: attributed=%d (events %d→%d%s) "
            "refreshed=%s in %.1fs",
            attributed, res.get("cursor_from", 0), res.get("cursor_to", 0),
            ", cursor reset" if res.get("reset") else "", refreshed, elapsed,
        )
        self.last_result = {
            "ok": True, "attributed": attributed, "refreshed": refreshed,
            "cursor_from": res.get("cursor_from"), "cursor_to": res.get("cursor_to"),
            "reset": bool(res.get("reset")), "seconds": round(elapsed, 3),
        }
        return self.last_result
