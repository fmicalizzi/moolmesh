"""#107: every module logs under the ``hub`` namespace that ``setup()`` configures.

Before the fix several modules logged under ``moolmesh.*``, which is not a
child of ``hub``: their INFO records (e.g. the migration 6/8 row counts) had no
handler and never reached the log file.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hub import log

HUB_DIR = Path(__file__).resolve().parent.parent / "hub"

# Module-level loggers that used to live under ``moolmesh.*``.
MODULE_LOGGERS = [
    "hub.backups",
    "hub.backfill",
    "hub.cache.event_store",
    "hub.mcp_registry",
    "hub.mcp_server",
    "hub.watchers.base",
]


@pytest.fixture
def configured_log(tmp_path, monkeypatch):
    """Run ``log.setup()`` against a temp file; restore the ``hub`` logger after."""
    hub_logger = logging.getLogger("hub")
    saved_handlers = list(hub_logger.handlers)
    saved_level = hub_logger.level
    for h in saved_handlers:
        hub_logger.removeHandler(h)
    log_file = tmp_path / "hub.log"
    monkeypatch.setattr(log, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log, "LOG_FILE", log_file)
    monkeypatch.setattr(log, "_CONFIGURED", False)
    log.setup(level="INFO", log_to_file=True)
    try:
        yield log_file
    finally:
        for h in list(hub_logger.handlers):
            hub_logger.removeHandler(h)
            h.close()
        for h in saved_handlers:
            hub_logger.addHandler(h)
        hub_logger.setLevel(saved_level)


def _flush():
    for h in logging.getLogger("hub").handlers:
        h.flush()


@pytest.mark.parametrize("module", MODULE_LOGGERS)
def test_module_info_reaches_log_file(configured_log, module):
    import importlib
    mod = importlib.import_module(module)
    logger = mod._log
    assert logger.name.startswith("hub."), logger.name
    marker = f"INFO-107-{module}"
    logger.info(marker)
    _flush()
    content = configured_log.read_text(encoding="utf-8")
    assert content.count(marker) == 1  # reaches the file, exactly once


def test_supervisor_info_reaches_log_file(configured_log):
    # The watcher supervisor builds its logger inside the loop; same name.
    src = (HUB_DIR / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("hub.supervisor")' in src
    logging.getLogger("hub.supervisor").info("INFO-107-supervisor")
    _flush()
    assert configured_log.read_text(encoding="utf-8").count("INFO-107-supervisor") == 1


def test_event_store_migration_info_reaches_log_file(configured_log, tmp_path):
    """A real migration line from EventStore lands in the configured file."""
    from hub.cache.event_store import _log as es_log
    es_log.info("migración %d: %d eventos", 8, 3)
    _flush()
    assert "[hub.event_store] INFO — migración 8: 3 eventos" in \
        configured_log.read_text(encoding="utf-8")


_LITERAL_GETLOGGER = re.compile(r"""getLogger\(\s*f?(["'])([^"']*)\1""")


def test_guard_no_logger_outside_hub_namespace():
    """Guard: every literal logger name in hub/ is ``hub`` or ``hub.*``."""
    offenders = []
    for path in HUB_DIR.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _LITERAL_GETLOGGER.finditer(line):
                name = m.group(2)
                if name != "hub" and not name.startswith("hub."):
                    offenders.append(f"{path.relative_to(HUB_DIR.parent)}:{lineno}: {name}")
    assert not offenders, "loggers outside the hub namespace (#107):\n" + "\n".join(offenders)


def test_mcp_server_logging_never_writes_stdout(tmp_path):
    """The MCP stdio transport owns stdout: its logs must stay off it."""
    code = (
        "import logging, hub.mcp_server as m\n"
        "m._log.warning('WARN-107'); m._log.info('INFO-107')\n"
        "logging.getLogger('hub').warning('HUB-107')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        cwd=str(HUB_DIR.parent),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "WARN-107" in proc.stderr
