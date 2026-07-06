"""Daemon management for MoolMesh dashboard."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform.startswith("win")

CONFIG_DIR = Path.home() / ".moolmesh"
PID_FILE = CONFIG_DIR / "moolmesh.pid"
LOG_FILE = CONFIG_DIR / "daemon.log"


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return None


def write_pid(pid: int) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _is_supervised() -> bool:
    """Detect if running under a process supervisor (systemd, launchd, Docker)."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("NOTIFY_SOCKET"))


def daemonize(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> int:
    """Launch dashboard in background. Returns child PID.

    Unix: classic double-fork. Windows: subprocess with CREATE_NO_WINDOW.
    Supervised (systemd/Docker): stays in foreground.
    """
    if _is_supervised():
        return _run_foreground(host, port, project_filter, providers)

    if _IS_WINDOWS:
        return _daemonize_windows(host, port, project_filter, providers)

    return _daemonize_unix(host, port, project_filter, providers)


def _daemonize_windows(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> int:
    """Windows background process via subprocess.Popen + CREATE_NO_WINDOW."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "hub.cli", "dashboard",
           "--host", host, "--port", str(port)]
    if project_filter:
        cmd += ["--project", project_filter]
    if providers:
        cmd += ["--providers", ",".join(providers)]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    CREATE_NO_WINDOW = 0x08000000
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )
    write_pid(proc.pid)
    return proc.pid


def _daemonize_unix(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> int:
    """Unix double-fork daemon."""
    pid = os.fork()
    if pid > 0:
        time.sleep(0.3)
        return read_pid() or pid

    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())

    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    _run_server(host, port, project_filter, providers)
    os._exit(0)


def _run_foreground(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> int:
    """Run in foreground for process supervisors (systemd, Docker)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())
    _run_server(host, port, project_filter, providers)
    return os.getpid()


def _run_server(host: str, port: int, project_filter: str | None, providers: list[str] | None) -> None:
    """Start the dashboard server with signal handling."""
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

    try:
        signal.signal(signal.SIGTERM, _handle_term)
    except OSError:
        pass

    try:
        server.start()
    finally:
        PID_FILE.unlink(missing_ok=True)


def stop_daemon() -> bool:
    """Stop a running daemon. Returns True if stopped successfully."""
    pid = read_pid()
    if pid is None:
        return False

    if _IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)

    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            PID_FILE.unlink(missing_ok=True)
            return True

    # Force kill
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
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
