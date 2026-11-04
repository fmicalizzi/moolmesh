"""Daemon management for MoolMesh dashboard."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

_IS_WINDOWS = sys.platform.startswith("win")

CONFIG_DIR = Path.home() / ".moolmesh"
PID_FILE = CONFIG_DIR / "moolmesh.pid"
# Names the PID a stop was requested for (#111): a live PID that does not
# answer /health is shutting down when this names it, starting otherwise.
STOPPING_FILE = CONFIG_DIR / "moolmesh.stopping"
LOG_FILE = CONFIG_DIR / "daemon.log"

# How long the parent waits for a just-launched daemon to answer /health (#55b).
DEFAULT_START_TIMEOUT = 10.0
_POLL_INTERVAL = 0.2
# A start whose new process is still alive past that window is not failed
# (#108): a first start after an upgrade can run a migration that backs up a
# large events.db before the server answers. It is waited for up to this
# long, with SLOW_START_NOTICE printed once.
START_SLOW_TIMEOUT = 180.0
SLOW_START_NOTICE = "starting (migrating databases?) …"

# How long ``stop`` (every mechanism) and a ``start`` that finds a daemon
# shutting down wait for the old PID to exit (#96). Shutdown takes well under
# a second now that the loops wake on their stop event; the margin covers a
# daemon still running pre-#96 code (~26 s) right after an upgrade.
STOP_TIMEOUT = 30.0
# Unsupervised stop: SIGTERM grace before escalating to SIGKILL / taskkill /F.
STOP_GRACE = 10.0

# daemon.log stays bounded (#76): past LOG_MAX_BYTES it is renamed to
# daemon.log.1 (replacing the previous one) at start and every
# LOG_CHECK_INTERVAL seconds while the daemon runs.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_CHECK_INTERVAL = 600.0

# Environment variables removed before the daemon spawns anything (#76): a
# macOS daemon.log reached 55 MB of "Python(<pid>) MallocStackLogging: can't
# turn off malloc stack logging because it was not enabled." — libmalloc
# prints that in every process started with an inherited MallocStackLogging*
# variable. Dropping them here keeps every child (git, gh, Python) quiet.
_SCRUB_ENV_PREFIXES = ("MallocStackLogging",)
_scrubbed_env: list[str] = []


class DaemonStartError(RuntimeError):
    """The background daemon did not become healthy in the start window."""


class DaemonStopTimeout(RuntimeError):
    """The daemon process was still alive when the stop wait ran out (#96)."""

    def __init__(self, pid: int, timeout: float):
        super().__init__(f"PID {pid} still shutting down after {timeout:g}s")
        self.pid = pid
        self.timeout = timeout


def read_pid() -> int | None:
    """PID from the pidfile if that process is alive, else None (and clears it).

    Liveness goes through ``_pid_alive``: ``os.kill(pid, 0)`` would terminate
    the daemon on Windows (#55).
    """
    try:
        text = PID_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        pid = int(text.strip())
    except ValueError:
        # Garbage, not a half-written file: write_pid replaces atomically.
        PID_FILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        _clear_pidfile(pid)
        return None
    return pid


def _clear_pidfile(pid: int) -> None:
    """Remove the pidfile only if it still names ``pid`` (#96).

    A ``start`` that waited out a shutdown may already have launched the
    next daemon, which rewrote the pidfile; an unconditional unlink by the
    old stop (or the old daemon's own exit) would orphan the new one.
    A missing or unreadable pidfile is left alone.
    """
    try:
        current = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if current == pid:
        PID_FILE.unlink(missing_ok=True)
    clear_stop_requested(pid)


def mark_stop_requested(pid: int) -> None:
    """Record that ``pid`` was asked to stop (#111). Never raises.

    Written by every stop path we drive (``stop_daemon``, the supervised
    stop/restart) and by the daemon's own SIGTERM handler, which also covers
    a stop sent by launchd/systemd directly. The marker only ever refines a
    wait: a live PID it does not name is waited for as a start, which ends
    the same way once the process exits.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STOPPING_FILE.with_name(f"{STOPPING_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(str(pid), encoding="utf-8")
        os.replace(tmp, STOPPING_FILE)
    except OSError:
        # Without the marker a later start waits this stop out as a start:
        # longer, but it still ends once the process exits.
        from hub.log import get
        get("daemon").warning("could not write %s", STOPPING_FILE, exc_info=True)


def clear_stop_requested(pid: int) -> None:
    """Remove the stop marker if it names ``pid``; other content is kept."""
    try:
        current = int(STOPPING_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if current == pid:
        STOPPING_FILE.unlink(missing_ok=True)


def stop_requested(pid: int) -> bool:
    """Was a stop requested for the live process ``pid``? (#111)

    A marker that names another PID, a dead PID, or holds garbage is stale
    (a daemon killed hard, a crash): it is removed and never blocks a start.
    """
    try:
        text = STOPPING_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        marked = int(text.strip())
    except ValueError:
        STOPPING_FILE.unlink(missing_ok=True)
        return False
    if marked == pid and _pid_alive(pid):
        return True
    if not _pid_alive(marked):
        clear_stop_requested(marked)
    return False


def write_pid(pid: int) -> None:
    """Write the pidfile atomically, so a reader never sees it half-written."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PID_FILE.with_name(f"{PID_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(str(pid), encoding="utf-8")
    os.replace(tmp, PID_FILE)
    # A reused PID must not inherit a marker left by a daemon killed hard.
    clear_stop_requested(pid)


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


def wait_for_pid_exit(pid: int, timeout: float | None = None) -> bool:
    """Poll until ``pid`` is gone; False if it is still alive after ``timeout``.

    Counts polls rather than reading the clock, so a test that stubs
    ``time.sleep`` finishes at once instead of spinning for ``timeout``.
    """
    if timeout is None:
        timeout = STOP_TIMEOUT
    polls = max(1, round(timeout / _POLL_INTERVAL))
    for _ in range(polls):
        if not _pid_alive(pid):
            return True
        time.sleep(_POLL_INTERVAL)
    return not _pid_alive(pid)


def daemon_serves(pid: int, host: str, port: int) -> bool:
    """Does ``/health`` on ``host:port`` answer as a serving MoolMesh? (#96)

    ``healthy`` or ``degraded`` both count. A payload naming another PID
    does not: that is some other daemon, not ``pid``. A payload without a
    ``pid`` (older daemons) is taken at face value.
    """
    health = _fetch_health(_probe_host(host), port)
    if not health or health.get("status") not in ("healthy", "degraded"):
        return False
    other = health.get("pid")
    return other is None or other == pid


def wait_out_shutdown(pid: int, host: str, port: int,
                      timeout: float | None = None) -> str:
    """Classify a live PID found by ``start`` (#96).

    Returns ``"running"`` as soon as it answers ``/health`` (a daemon that
    is up, or still starting), ``"exited"`` once the process is gone (it was
    shutting down), or ``"stuck"`` if neither happens within ``timeout``.
    """
    if timeout is None:
        timeout = STOP_TIMEOUT
    polls = max(1, round(timeout / _POLL_INTERVAL))
    for _ in range(polls):
        if not _pid_alive(pid):
            _clear_pidfile(pid)
            return "exited"
        if daemon_serves(pid, host, port):
            return "running"
        time.sleep(_POLL_INTERVAL)
    if not _pid_alive(pid):
        _clear_pidfile(pid)
        return "exited"
    return "stuck"


def wait_out_start(pid: int, host: str, port: int, *,
                   timeout: float | None = None,
                   slow_timeout: float | None = None,
                   on_slow: Callable[[], None] | None = None) -> str:
    """Wait for a live PID that is starting, not stopping (#111).

    Uses ``wait_until_serving``: past ``timeout`` (default
    ``DEFAULT_START_TIMEOUT``) a process still alive is a slow start (a
    migration) and is waited for up to ``slow_timeout``. Returns
    ``"running"`` once it answers ``/health``, ``"exited"`` if it dies,
    ``"stopping"`` if a stop is requested meanwhile, ``"stuck"`` otherwise.
    """
    if timeout is None:
        timeout = DEFAULT_START_TIMEOUT
    stopping = False

    def alive() -> bool:
        nonlocal stopping
        if not _pid_alive(pid):
            return False
        if stop_requested(pid):
            stopping = True
            return False
        return True

    outcome = wait_until_serving(lambda: daemon_serves(pid, host, port), alive,
                                 timeout=timeout, slow_timeout=slow_timeout,
                                 on_slow=on_slow)
    if outcome == "ready":
        return "running"
    if outcome == "exited":
        if stopping:
            return "stopping"
        _clear_pidfile(pid)
        return "exited"
    return "stuck"


def _terminate_pid(pid: int, *, wait_seconds: float = 3.0) -> None:
    """Best-effort stop of a specific pid.

    Windows: ``taskkill /PID`` — no signals, since ``os.kill`` would terminate
    the process and ``signal.SIGKILL`` does not exist there. Unix: SIGTERM,
    then SIGKILL after ``wait_seconds``.
    """
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


def wait_until_serving(
    ready: Callable[[], bool],
    alive: Callable[[], bool | None],
    *,
    timeout: float,
    slow_timeout: float | None = None,
    on_slow: Callable[[], None] | None = None,
) -> str:
    """Poll ``ready()`` for a starting daemon, extending while it lives (#108).

    ``alive()`` answers True (the new process is up), False (it was up and is
    gone) or None (not known yet — a supervisor may not have spawned it).
    Returns ``"ready"``; ``"exited"`` as soon as ``alive()`` is False; or
    ``"timeout"`` when nothing is ready after ``timeout`` seconds with no live
    process, or after ``slow_timeout`` (default ``START_SLOW_TIMEOUT``) with
    one. ``on_slow`` runs once, when the wait goes past ``timeout`` because
    the process is alive.
    """
    if slow_timeout is None:
        slow_timeout = START_SLOW_TIMEOUT
    start = time.monotonic()
    slow = False
    while True:
        if ready():
            return "ready"
        state = alive()
        if state is False:
            return "exited"
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            if state is not True or elapsed >= slow_timeout:
                return "timeout"
            if not slow:
                slow = True
                if on_slow is not None:
                    on_slow()
        time.sleep(_POLL_INTERVAL)


def wait_for_daemon_ready(
    pid: int,
    host: str,
    port: int,
    *,
    timeout: float | None = None,
    slow_timeout: float | None = None,
    on_slow: Callable[[], None] | None = None,
) -> None:
    """Wait until the just-launched daemon answers /health as itself (#55b).

    Success: ``/health`` reports ``status == "healthy"`` and ``pid == pid``
    (the health payload carries the serving process id). Failure: the process
    is gone, or it is still not healthy after ``slow_timeout`` (default
    ``START_SLOW_TIMEOUT``). Past ``timeout`` (default
    ``DEFAULT_START_TIMEOUT``) a live process is a slow start — a migration —
    and is waited for, with ``on_slow`` called once (#108). On the final
    timeout the process is stopped so a hung start cannot leave a daemon
    running behind a reported failure.

    Raises ``DaemonStartError`` for the caller to report and exit 1 on.
    """
    if timeout is None:
        timeout = DEFAULT_START_TIMEOUT
    if slow_timeout is None:
        slow_timeout = START_SLOW_TIMEOUT
    probe = _probe_host(host)

    def ready() -> bool:
        health = _fetch_health(probe, port)
        return bool(health and health.get("status") == "healthy"
                    and health.get("pid") == pid)

    outcome = wait_until_serving(ready, lambda: _pid_alive(pid), timeout=timeout,
                                 slow_timeout=slow_timeout, on_slow=on_slow)
    if outcome == "ready":
        return
    if outcome == "exited":
        raise DaemonStartError(
            f"the daemon exited before becoming healthy (port {port})"
        )
    _terminate_pid(pid)
    raise DaemonStartError(
        f"no healthy answer on port {port} within {max(timeout, slow_timeout):g}s"
    )


def log_tail(max_lines: int = 10) -> str:
    """Last ``max_lines`` lines of the daemon log ("" when there is none)."""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"(could not read {LOG_FILE}: {exc})"
    return "\n".join(lines[-max_lines:])


def scrub_child_env(environ=None) -> list[str]:
    """Remove ``MallocStackLogging*`` from ``environ`` (default ``os.environ``).

    Returns the removed names. Children inherit ``os.environ``, so scrubbing
    the daemon's own environment covers every subprocess it starts.
    """
    environ = os.environ if environ is None else environ
    removed = [k for k in list(environ) if k.startswith(_SCRUB_ENV_PREFIXES)]
    for key in removed:
        environ.pop(key, None)
    return removed


def rotate_log_if_needed(log_file: Path | None = None,
                         max_bytes: int | None = None) -> bool:
    """Rename ``daemon.log`` to ``daemon.log.1`` when it exceeds ``max_bytes``.

    One generation is kept (the previous ``.1`` is replaced). Size-based
    rotation by rename rather than ``RotatingFileHandler``: the file is not
    written by a logging handler but by raw stdout/stderr — the daemon's and
    its children's, or launchd's ``StandardOutPath`` — so the writers'
    descriptors are re-pointed by ``reopen_stdio`` instead. Never raises.
    """
    log_file = LOG_FILE if log_file is None else log_file
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    try:
        if log_file.stat().st_size <= max_bytes:
            return False
        os.replace(log_file, log_file.with_name(log_file.name + ".1"))
    except OSError:
        # Missing file, or (Windows) still held open by a running daemon.
        return False
    return True


def reopen_stdio(log_file: Path | None = None, fds: tuple[int, ...] = (1, 2)) -> bool:
    """Point fd 1/2 at a fresh ``log_file`` if they still write the rotated one.

    Covers launchd (which opened ``StandardOutPath`` before the daemon ran)
    and the background daemon after an in-process rotation. Only descriptors
    that point at ``<log>.1`` are touched; a terminal or the systemd journal
    is left alone. Returns True when something was re-pointed.
    """
    if _IS_WINDOWS:
        return False
    log_file = LOG_FILE if log_file is None else log_file
    try:
        rotated = log_file.with_name(log_file.name + ".1").stat()
    except OSError:
        return False
    targets = []
    for fd in fds:
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if (st.st_ino, st.st_dev) == (rotated.st_ino, rotated.st_dev):
            targets.append(fd)
    if not targets:
        return False
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    try:
        new_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        return False
    try:
        for fd in targets:
            os.dup2(new_fd, fd)
    finally:
        os.close(new_fd)
    return True


def _log_rotation_loop(stop: "threading.Event") -> None:
    while not stop.wait(LOG_CHECK_INTERVAL):
        if rotate_log_if_needed():
            reopen_stdio()


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
    global _scrubbed_env
    _scrubbed_env = scrub_child_env()
    rotate_log_if_needed()

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
    # launchd opened daemon.log for us before the start-time rotation.
    reopen_stdio()
    if sys.stdout is None or sys.stderr is None:
        # pythonw.exe (Task Scheduler, #76/#78) has no stdio: log to the file.
        log_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_fh
        if sys.stderr is None:
            sys.stderr = log_fh
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
    import threading
    from hub.dashboard.server import DashboardServer
    from hub.log import get, setup
    setup(level="INFO")
    if _scrubbed_env:
        get("daemon").info(
            "removed from the child-process environment: %s", ", ".join(_scrubbed_env))
    rotation_stop = threading.Event()
    threading.Thread(target=_log_rotation_loop, args=(rotation_stop,),
                     name="LogRotation", daemon=True).start()

    server = DashboardServer(
        host=host,
        port=port,
        project_filter=project_filter,
        providers=providers,
        fixed_port=fixed_port or _is_supervised(),
    )

    def _handle_term(signum, frame):
        # Covers a stop sent by launchd/systemd directly (#111).
        mark_stop_requested(os.getpid())
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handle_term)
    except OSError:
        pass

    try:
        server.start()
    finally:
        rotation_stop.set()
        _clear_pidfile(os.getpid())


def stop_daemon() -> bool:
    """Stop a running daemon and wait until its PID is gone.

    Returns False when no daemon was running, True once the process has
    exited. SIGTERM (Windows: ``taskkill /PID``) gets ``STOP_GRACE`` seconds,
    then SIGKILL (``taskkill /F``); the force kill also waits for the exit
    and raises ``DaemonStopTimeout`` if the process is somehow still there
    (#96), so callers never report "stopped" for a live process. Liveness is
    always probed with ``_pid_alive``, never ``os.kill`` — on Windows that
    would terminate the process (#55).
    """
    pid = read_pid()
    if pid is None:
        return False

    mark_stop_requested(pid)
    if _IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _clear_pidfile(pid)
            return True

    if wait_for_pid_exit(pid, STOP_GRACE):
        _clear_pidfile(pid)
        return True

    # Force kill
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not wait_for_pid_exit(pid, 5.0):
        raise DaemonStopTimeout(pid, STOP_GRACE + 5.0)
    _clear_pidfile(pid)
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
