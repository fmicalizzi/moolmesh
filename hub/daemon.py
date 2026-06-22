"""Daemon management for MoolMesh dashboard."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".moolmesh"
PID_FILE = CONFIG_DIR / "moolmesh.pid"
LOG_FILE = CONFIG_DIR / "daemon.log"


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


def write_pid(pid: int) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def daemonize(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> int:
    """Double-fork to detach from terminal. Returns child PID to the caller."""
    pid = os.fork()
    if pid > 0:
        # First parent — wait briefly for child to write PID, then return it
        time.sleep(0.3)
        return read_pid() or pid

    # First child — new session
    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Second child — the actual daemon
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())

    # Redirect stdout/stderr to log file
    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # Redirect stdin from /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    # Run the dashboard
    from hub.dashboard.server import DashboardServer
    from hub.log import setup
    setup(level="INFO")

    server = DashboardServer(
        host=host,
        port=port,
        project_filter=project_filter,
        providers=providers,
    )

    def _handle_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        server.start()
    finally:
        PID_FILE.unlink(missing_ok=True)

    os._exit(0)


def stop_daemon() -> bool:
    """Stop a running daemon. Returns True if stopped successfully."""
    pid = read_pid()
    if pid is None:
        return False

    os.kill(pid, signal.SIGTERM)

    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)
            return True

    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
    return True


def daemon_status() -> dict | None:
    """Return daemon info dict or None if not running."""
    pid = read_pid()
    if pid is None:
        return None

    info: dict = {"pid": pid}

    # Uptime from PID file mtime
    try:
        started = PID_FILE.stat().st_mtime
        info["uptime_seconds"] = int(time.time() - started)
    except OSError:
        info["uptime_seconds"] = 0

    # Log file size
    try:
        info["log_size"] = LOG_FILE.stat().st_size
    except OSError:
        info["log_size"] = 0

    return info
