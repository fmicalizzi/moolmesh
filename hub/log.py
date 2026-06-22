"""Logging module para MoolMesh — zero dependencies, stdlib only."""
from __future__ import annotations

import logging
import sys
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
