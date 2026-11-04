"""Read-only connections to databases MoolMesh does not own (issues #51, #54).

Provider databases — Codex ``state_5.sqlite``, ``opencode.db``, Cursor's
``state.vscdb`` — are observed, never written. A plain ``sqlite3.connect`` is
read-write: it takes write locks on another tool's DB and, as the last
connection to close a WAL database, checkpoints the WAL into the provider's
main file — a real write to their data. ``mode=ro`` never does either.

Zero footprint (#54). ``mode=ro`` alone still leaves files behind: on a WAL
database whose owner is not running (no ``-wal``/``-shm`` next to it) and
whose directory is writable, SQLite creates an empty ``-wal`` and a 32 KB
``-shm`` to read it, and a read-only connection cannot remove them on close
(verified with SQLite 3.45 on Linux and 3.53 on macOS). If they cannot be
created (read-only directory) the read fails with ``OperationalError``.

Two fixes were measured on a synthetic WAL database (owner closed):

* a consistent copy with the online backup API — rejected: the backup must
  open the SOURCE first, and that ``mode=ro`` open creates the same
  ``-wal``/``-shm``; it only moves the problem and adds a full copy per read.
* ``immutable=1`` — leaves nothing, and also reads from a read-only
  directory. Chosen, under one exact condition: the file header says WAL
  (bytes 18-19 == 2) AND neither ``-wal`` nor ``-shm`` exists. That is the
  owner-closed, fully checkpointed state, where the main file holds every
  committed row. With either file present (owner running, or a stand-in left
  by an earlier read) the plain ``mode=ro`` open is used, which sees the WAL
  frames; an immutable open there would silently miss them. A database in
  rollback-journal mode never gets ``immutable``: a reader creates nothing.

The accepted race: the owner starts DURING an immutable read. In WAL mode
its new writes go to a fresh ``-wal``, not the main file, so the immutable
reader keeps a consistent (slightly old) view and the next cycle — which
sees the ``-wal`` — opens ``mode=ro`` and reads the new rows. The main file
changes only on a checkpoint, which needs the owner to write and checkpoint
within one short read; then a query may fail with ``DatabaseError``, which
every caller already logs and retries next cycle. Nothing is ever written to
the provider's files either way. There is deliberately NO fallback to a
read-write connection.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# SQLite header: bytes 18 (write version) and 19 (read version) are 2 in WAL mode.
_WAL_VERSION = 2


def ro_uri(path: str | Path) -> str:
    """``file:`` URI opening ``path`` read-only (percent-encoded, Windows-safe)."""
    return Path(path).absolute().as_uri() + "?mode=ro"


def is_closed_wal_db(path: str | Path) -> bool:
    """True when ``path`` is a WAL database with no ``-wal`` and no ``-shm`` (#54).

    The owner-closed, fully checkpointed state in which an ``immutable`` read
    is both complete and footprint-free. Reads the 100-byte header only; an
    unreadable or short file is not reported (the open handles the error).
    """
    p = str(path)
    if os.path.exists(p + "-wal") or os.path.exists(p + "-shm"):
        return False
    try:
        with open(p, "rb") as f:
            header = f.read(100)
    except OSError:
        return False
    return (len(header) == 100 and header.startswith(b"SQLite format 3\x00")
            and header[18] == _WAL_VERSION and header[19] == _WAL_VERSION)


def zero_footprint_uri(path: str | Path) -> str:
    """Read-only URI that leaves no file next to ``path`` (see module notes)."""
    uri = ro_uri(path)
    if is_closed_wal_db(path):
        uri += "&immutable=1"
    return uri


def connect_ro(path: str | Path, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a database MoolMesh doesn't own, read-only. Raises ``sqlite3.Error``."""
    return sqlite3.connect(zero_footprint_uri(path), uri=True, timeout=timeout)
