"""Read-only connections to databases MoolMesh does not own (issue #51).

Provider databases — Codex ``state_5.sqlite``, ``opencode.db``, Cursor's
``state.vscdb`` — are observed, never written. A plain ``sqlite3.connect`` is
read-write: it takes write locks on another tool's DB and, as the last
connection to close a WAL database, checkpoints the WAL into the provider's
main file — a real write to their data. ``mode=ro`` never does either.

What ``mode=ro`` does NOT prevent (verified with SQLite 3.53 on macOS): on a
WAL database whose owner is not running (no ``-wal``/``-shm`` present) and
whose directory is writable, SQLite creates an empty ``-wal`` and a 32 KB
``-shm`` to read it, and a read-only connection cannot remove them on close.
They hold no data and the owner reuses them on its next open. If they cannot
be created (read-only directory), the read fails with ``OperationalError``;
callers log it and skip that read for the cycle. There is deliberately NO
fallback to a read-write connection, and ``immutable=1`` is not used: it is
unsafe if the provider starts writing mid-read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def ro_uri(path: str | Path) -> str:
    """``file:`` URI opening ``path`` read-only (percent-encoded, Windows-safe)."""
    return Path(path).absolute().as_uri() + "?mode=ro"


def connect_ro(path: str | Path, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a database MoolMesh doesn't own, read-only. Raises ``sqlite3.Error``."""
    return sqlite3.connect(ro_uri(path), uri=True, timeout=timeout)
