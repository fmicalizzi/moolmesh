"""Retention for ``~/.moolmesh/backups/`` (issue #52).

Every pass that rewrites existing rows first copies its database there with
sqlite3's online backup API (``hub.backfill.backup_events_db``,
``hub.cache.workspace_store.backup_workspace_db``). Nothing pruned them, so a
user who ran ``mool backfill --reparse`` a few times kept every full copy of
``events.db``. After each new backup the oldest copies of the same kind are
deleted, keeping the newest ``BACKUP_KEEP``.

Only files named exactly like a MoolMesh backup (``<kind>-YYYYmmdd-HHMMSS.db``
or ``<kind>-YYYYmmdd-HHMMSS-N.db``) are candidates: anything else a user put in
the directory is never touched, and the backup just made never is either.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

_log = logging.getLogger("hub.backups")

# Newest copies kept per kind (events-, workspace-).
BACKUP_KEEP = 5


def _pattern(kind: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(kind)}-\d{{8}}-\d{{6}}(?:-\d+)?\.db$")


def prune_backups(backup_dir: Path, kind: str, keep: int = BACKUP_KEEP,
                  protect: Path | None = None) -> list[Path]:
    """Delete all but the newest ``keep`` ``<kind>-*.db`` backups; return them.

    Newest = latest mtime (the name breaks ties). ``protect`` — the backup just
    made — is never deleted and counts toward ``keep``. A file that cannot be
    deleted is logged and left; pruning never raises.
    """
    keep = max(1, keep)
    pat = _pattern(kind)
    found: list[tuple[float, str, Path]] = []
    try:
        entries = list(Path(backup_dir).iterdir())
    except OSError:
        return []
    for p in entries:
        if not pat.match(p.name):
            continue
        try:
            if not p.is_file() or p.is_symlink():
                continue
            found.append((p.stat().st_mtime, p.name, p))
        except OSError:
            continue
    found.sort(reverse=True)
    protected = Path(protect).resolve() if protect else None
    kept = 0
    removed: list[Path] = []
    survivors: list[Path] = []
    for _mtime, _name, p in found:
        if protected is not None and p.resolve() == protected:
            continue
        survivors.append(p)
    # The protected copy always takes one slot.
    slots = keep - (1 if protected is not None and protected.exists() else 0)
    for p in survivors:
        if kept < slots:
            kept += 1
            continue
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            _log.warning("no pude borrar el respaldo viejo %s", p, exc_info=True)
    if removed:
        _log.info("respaldos: borré %d copias viejas de %s (quedan las %d más nuevas)",
                  len(removed), kind, keep)
    return removed
