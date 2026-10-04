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

# How long the parent waits for a just-launched daemon to answer /health (#55b).
DEFAULT_START_TIMEOUT = 10.0
_POLL_INTERVAL = 0.2


class DaemonStartError(RuntimeError):
    """The background daemon did not become healthy in the start window."""


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


def _pid_is_supervised(pid: int) -> bool:
    """Best-effort: was ``pid`` started by a supervisor?

    Linux: the process environment carries INVOCATION_ID (systemd) or
    NOTIFY_SOCKET. Other platforms expose no reliable portable marker, so
    this returns False there — callers use it only to warn, never to block.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return b"INVOCATION_ID=" in environ or b"NOTIFY_SOCKET=" in environ


def _probe_host(host: str) -> str:
    """Loopback when the daemon bound a wildcard address (#55b)."""
    return "127.0.0.1" if host in ("0.0.0.0", "") else host


def _pid_alive(pid: int) -> bool:
    """Liveness probe that never signals the process (os.kill on Windows would)."""
    if _IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _fetch_health(host: str, port: int) -> dict | None:
    """One GET /health; None when nothing healthy answers (or it is not JSON)."""
    import http.client
    import json
    from urllib.request import urlopen
    try:
        with urlopen(f"http://{host}:{port}/health", timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _terminate_pid(pid: int, *, wait_seconds: float = 3.0) -> None:
    """Best-effort stop of a specific pid (SIGTERM, then SIGKILL on Unix)."""
    if _IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def wait_for_daemon_ready(
    pid: int,
    host: str,
    port: int,
    *,
    timeout: float | None = None,
) -> None:
    """Wait until the just-launched daemon answers /health as itself (#55b).

    Success: ``/health`` reports ``status == "healthy"`` and ``pid == pid``
    (the health payload carries the serving process id). Failure: the process
    is gone, or ``timeout`` seconds (default ``DEFAULT_START_TIMEOUT``) pass
    without a healthy answer; on timeout the process is stopped so a slow
    start cannot leave a daemon running behind a reported failure.

    Raises ``DaemonStartError`` for the caller to report and exit 1 on.
    """
    if timeout is None:
        timeout = DEFAULT_START_TIMEOUT
    probe = _probe_host(host)
    deadline = time.monotonic() + timeout
    while True:
        health = _fetch_health(probe, port)
        if health and health.get("status") == "healthy" and health.get("pid") == pid:
            return
        if not _pid_alive(pid):
            raise DaemonStartError(
                f"the daemon exited before becoming healthy (port {port})"
            )
        if time.monotonic() >= deadline:
            _terminate_pid(pid)
            raise DaemonStartError(
                f"no healthy answer on port {port} within {timeout:g}s"
            )
        time.sleep(_POLL_INTERVAL)


def log_tail(max_lines: int = 10) -> str:
    """Last ``max_lines`` lines of the daemon log ("" when there is none)."""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"(could not read {LOG_FILE}: {exc})"
    return "\n".join(lines[-max_lines:])


def _read_pid_report(read_fd: int, *, timeout: float = 5.0) -> int | None:
    """Read the child's PID line from the launch pipe; None on EOF/timeout."""
    import select
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = select.select([read_fd], [], [], remaining)
        except OSError:
            return None
        if not ready:
            return None
        chunk = os.read(read_fd, 64)
        if not chunk:
            break
        buf += chunk
    try:
        return int(buf.split(b"\n", 1)[0])
    except ValueError:
        return None


def daemonize(
    host: str,
    port: int,
    project_filter: str | None,
    providers: list[str] | None,
    *,
    foreground: bool = False,
    fixed_port: bool = False,
) -> int:
    """Launch dashboard in background. Returns child PID.

    Unix: classic double-fork. Windows: subprocess with CREATE_NO_WINDOW.
    Supervised (systemd/Docker) or ``foreground=True``: stays in the
    foreground and blocks until the server stops. ``fixed_port`` forbids
    silent port auto-increment (explicit --port, supervised or foreground
    runs, #55).
    """
    if foreground or _is_supervised():
        return _run_foreground(host, port, project_filter, providers, fixed_port=fixed_port)

    if _IS_WINDOWS:
        return _daemonize_windows(host, port, project_filter, providers)

    return _daemonize_unix(host, port, project_filter, providers, fixed_port=fixed_port)


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


def _daemonize_unix(
    host: str,
    port: int,
    project_filter: str | None,
    providers: list[str] | None,
    *,
    fixed_port: bool = False,
) -> int:
    """Unix double-fork daemon. Returns the server (grandchild) PID.

    The grandchild reports its PID — and, with EOF, an early death — through a
    pipe before serving, so the parent never guesses from a pidfile race and
    reaps the intermediate process instead of leaving a zombie (#55b).
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        os.waitpid(pid, 0)
        try:
            daemon_pid = _read_pid_report(read_fd)
        finally:
            os.close(read_fd)
        if daemon_pid is None:
            raise DaemonStartError("the daemon process exited before reporting its PID")
        return daemon_pid

    os.close(read_fd)
    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())
    try:
        os.write(write_fd, f"{os.getpid()}\n".encode("ascii"))
    finally:
        os.close(write_fd)

    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    from hub.dashboard.server import DashboardStartError

    try:
        _run_server(host, port, project_filter, providers, fixed_port=fixed_port)
    except DashboardStartError:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _run_foreground(
    host: str,
    port: int,
    project_filter: str | None,
    providers: list[str] | None,
    *,
    fixed_port: bool = False,
) -> int:
    """Run in foreground for process supervisors (systemd, Docker).

    Blocks until the server stops; lets ``DashboardStartError`` propagate so
    the CLI can exit 1 when the dashboard could not start (#55).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())
    _run_server(host, port, project_filter, providers, fixed_port=fixed_port)
    return os.getpid()


def _run_server(
    host: str,
    port: int,
    project_filter: str | None,
    providers: list[str] | None,
    *,
    fixed_port: bool = False,
) -> None:
    """Start the dashboard server with signal handling."""
    from hub.dashboard.server import DashboardServer
    from hub.log import setup
    setup(level="INFO")

    server = DashboardServer(
        host=host,
        port=port,
        project_filter=project_filter,
        providers=providers,
        fixed_port=fixed_port or _is_supervised(),
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
