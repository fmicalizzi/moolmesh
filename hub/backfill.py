"""Backfill is no longer needed — harvesters handle it automatically.

The unified harvester pattern reads each file from its last SQLite offset
(or from byte 0 for new files). The first harvest cycle IS the backfill.

This module is kept as a stub for backwards compatibility with any scripts
that import from it. The functions return immediately with zero counts.
"""

from __future__ import annotations

from hub.cache.event_store import EventStore


def backfill(store: EventStore | None = None, **kwargs) -> dict[str, int]:
    """No-op stub. Harvesters handle backfill automatically."""
    return {"claude": 0, "codex": 0, "qwen": 0, "total": 0}


def gap_fill(store: EventStore | None = None, **kwargs) -> dict[str, int]:
    """No-op stub. Harvesters handle gap fill automatically."""
    return {"claude": 0, "codex": 0, "qwen": 0, "total": 0}
