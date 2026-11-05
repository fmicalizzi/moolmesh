"""Logging module para MoolMesh — zero dependencies, stdlib only."""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
import threading
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

LOG_DIR = Path.home() / ".moolmesh"
LOG_FILE = LOG_DIR / "hub.log"

# Formato: [2026-04-18 10:30:15] [GitHarvester] WARNING — mensaje
LOG_FORMAT = "[%(asctime)s] [%(name)s] %(levelname)s — %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup(level: str = "INFO", log_to_file: bool = True) -> None:
    """Configura logging global una sola vez.

    Args:
        level: INFO, DEBUG, WARNING, ERROR
        log_to_file: Si True, agrega RotatingFileHandler a ~/.moolmesh/hub.log
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("hub")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Handler 1: stderr (siempre)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(stderr_handler)

    # Handler 2: archivo rotativo (opcional)
    if log_to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
            root.addHandler(file_handler)
        except OSError:
            root.warning("No se pudo crear archivo de log: %s", LOG_FILE)


def get(name: str) -> logging.Logger:
    """Obtiene un logger con namespace 'hub.<name>'.

    Uso:
        from hub.log import get
        log = get("GitHarvester")
        log.info("Fetching repo %s", path)
        log.warning("Error en repo", exc_info=True)
    """
    return logging.getLogger(f"hub.{name}")


_MIGRATION_LOCK = threading.Lock()


def _db_file(conn: sqlite3.Connection) -> Path | None:
    """The main database file of ``conn`` (None for an in-memory DB)."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return Path(file) if file else None
    except sqlite3.Error:
        pass
    return None


@contextlib.contextmanager
def migration_logging(conn: sqlite3.Connection) -> Iterator[None]:
    """Make the migration lines of ANY process reach ``hub.log`` (#136).

    Only the daemon and the dashboard call :func:`setup`; in the CLI and in
    MCP servers the ``hub`` logger has no handler and INFO lines are dropped,
    so a migration they applied left no trace. While this is active and
    logging is not configured, an append-only file handler at INFO writes to
    the ``hub.log`` next to the migrated database (``~/.moolmesh`` for the
    real ones; a test DB logs beside itself), tagged with the pid. Nothing is
    printed to stdout/stderr (CLI output and the MCP stdio stream stay
    clean). A configured process (daemon) or an unwritable log is a no-op.
    """
    root = logging.getLogger("hub")
    db = _db_file(conn)
    if _CONFIGURED or root.handlers or db is None:
        yield
        return
    with _MIGRATION_LOCK:
        try:
            handler = logging.FileHandler(db.parent / LOG_FILE.name,
                                          encoding="utf-8", delay=True)
        except OSError:
            yield
            return
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            LOG_FORMAT + " (pid %(process)d)", LOG_DATE_FORMAT))
        old_level = root.level
        root.addHandler(handler)
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        try:
            yield
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
            handler.close()
