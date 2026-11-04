"""Run-at-login registration for the MoolMesh daemon (#76).

``mool daemon install`` registers the daemon with the platform supervisor so
it comes back after a reboot; ``mool daemon uninstall`` removes exactly what
``install`` created. Opt-in: nothing here runs unless the user asks for it.

- macOS: a LaunchAgent, ``~/Library/LaunchAgents/dev.moolmesh.daemon.plist``
  (label ``dev.moolmesh.daemon``), loaded with ``launchctl bootstrap``.
- Linux: a systemd ``--user`` unit, ``~/.config/systemd/user/moolmesh.service``.
- Windows: a Task Scheduler task ``MoolMesh`` that runs at logon. A task has
  no file of its own, so ``install`` records it in
  ``~/.moolmesh/autostart-task.json`` (the marker ``uninstall`` checks).

The supervised process always runs in the foreground (``daemon start
--foreground``): a double fork under ``KeepAlive``/``Restart=`` would make the
supervisor see an exit and relaunch, and the port fallback could leave
duplicate daemons.

Every file carries ``MARKER``; a file without it was written by hand and is
never overwritten (without ``--force``, which backs it up first) nor removed.
All supervisor commands go through ``_run`` so tests can mock them.
"""

from __future__ import annotations

import difflib
import json
import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MARKER = "managed by mool daemon install"
LAUNCHD_LABEL = "dev.moolmesh.daemon"
SYSTEMD_UNIT = "moolmesh.service"
WINDOWS_TASK = "MoolMesh"
# schtasks /TR accepts at most 261 characters.
SCHTASKS_TR_MAX = 261

MECHANISM_NAMES = {
    "launchd": "launchd (LaunchAgent)",
    "systemd": "systemd --user",
    "schtasks": "Task Scheduler",
}


def detect_mechanism(platform: str | None = None) -> str | None:
    """``launchd`` | ``systemd`` | ``schtasks`` for the platform, or None."""
    p = platform or sys.platform
    if p == "darwin":
        return "launchd"
    if p.startswith("linux"):
        return "systemd"
    if p.startswith("win"):
        return "schtasks"
    return None


def service_path(mechanism: str, home: Path | None = None) -> Path:
    """Where ``install`` writes its file for ``mechanism``."""
    home = home or Path.home()
    if mechanism == "launchd":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if mechanism == "systemd":
        return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    if mechanism == "schtasks":
        return home / ".moolmesh" / "autostart-task.json"
    raise ValueError(f"unknown autostart mechanism: {mechanism}")


def log_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".moolmesh" / "daemon.log"


def interpreter_for(mechanism: str, python: str | None = None) -> str:
    """The interpreter the supervisor launches.

    Windows uses ``pythonw.exe`` from the same environment, so the logon task
    never opens a console (#78); elsewhere ``sys.executable`` as-is.
    """
    python = python or sys.executable
    if mechanism == "schtasks":
        p = Path(python)
        if p.name.lower() == "python.exe":
            candidate = p.with_name("pythonw.exe")
            if candidate.exists():
                return str(candidate)
    return python


def daemon_argv(python: str, host: str, port: int) -> list[str]:
    """The supervised command: always ``--foreground`` (see module docstring)."""
    return [python, "-m", "hub.cli", "daemon", "start", "--foreground",
            "--host", host, "--port", str(port)]


# ── Renderers ───────────────────────────────────────────────────────

def render_plist(argv: list[str], path_env: str, home: Path) -> str:
    """LaunchAgent plist (``plistlib``) with ``MARKER`` as an XML comment.

    ``KeepAlive.SuccessfulExit = false``: relaunch after a crash, not after a
    clean stop (SIGTERM makes the foreground daemon exit 0).
    """
    log = str(log_path(home))
    data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "WorkingDirectory": str(home),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PATH": path_env,
            "PYTHONIOENCODING": "utf-8",
        },
        "ProcessType": "Background",
    }
    text = plistlib.dumps(data, sort_keys=False).decode("utf-8")
    head, sep, rest = text.partition("<plist")
    return f"{head}<!-- {MARKER} — `mool daemon uninstall` removes this file -->\n{sep}{rest}"


def _systemd_escape(arg: str) -> str:
    """One ExecStart/Environment word: specifiers and ``$`` escaped, quoted when needed."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if arg and not any(c in arg for c in ' \t"\\\''):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(argv: list[str], path_env: str) -> str:
    """systemd ``--user`` unit; logs go to the journal (``journalctl --user -u``)."""
    exec_start = " ".join(_systemd_escape(a) for a in argv)
    return (
        f"# {MARKER} — `mool daemon uninstall` removes this file.\n"
        "[Unit]\n"
        "Description=MoolMesh daemon (dashboard, watchers, harvesters)\n"
        "StartLimitIntervalSec=300\n"
        "StartLimitBurst=5\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "WorkingDirectory=%h\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"Environment={_systemd_escape('PATH=' + path_env)}\n"
        "Environment=PYTHONIOENCODING=utf-8\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def schtasks_create_argv(argv: list[str]) -> list[str]:
    return ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", WINDOWS_TASK,
            "/TR", windows_command_line(argv), "/F"]


def render_task_record(argv: list[str]) -> str:
    """The marker file standing in for the Windows task."""
    return json.dumps({
        "marker": MARKER,
        "task": WINDOWS_TASK,
        "argv": argv,
        "command": windows_command_line(argv),
    }, indent=2) + "\n"


# ── Plan ─────────────────────────────────────────────────────────────

def _gui_domain() -> str:
    return f"gui/{os.getuid()}"  # macOS only


@dataclass
class InstallPlan:
    mechanism: str
    path: Path
    content: str
    argv: list[str]
    activate: list[list[str]]
    # Commands tried when the first activation command fails (launchd only).
    fallback: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_install_plan(
    mechanism: str,
    host: str,
    port: int,
    *,
    python: str | None = None,
    path_env: str | None = None,
    home: Path | None = None,
) -> InstallPlan:
    home = home or Path.home()
    python = interpreter_for(mechanism, python)
    argv = daemon_argv(python, host, port)
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    # The PATH of the install moment (launchd / systemd --user start with a
    # minimal one: without `gh` on it the GitHub sync silently loses its
    # token). Duplicates dropped, order kept.
    path_env = os.pathsep.join(dict.fromkeys(p for p in path_env.split(os.pathsep) if p))
    path = service_path(mechanism, home)

    if mechanism == "launchd":
        domain = _gui_domain()
        return InstallPlan(
            mechanism, path, render_plist(argv, path_env, home), argv,
            activate=[
                ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                ["launchctl", "bootstrap", domain, str(path)],
            ],
            fallback=[
                ["launchctl", "unload", "-w", str(path)],
                ["launchctl", "load", "-w", str(path)],
            ],
        )
    if mechanism == "systemd":
        return InstallPlan(
            mechanism, path, render_systemd_unit(argv, path_env), argv,
            activate=[
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", SYSTEMD_UNIT],
                ["systemctl", "--user", "restart", SYSTEMD_UNIT],
            ],
        )
    if mechanism == "schtasks":
        notes = []
        tr = windows_command_line(argv)
        if len(tr) > SCHTASKS_TR_MAX:
            notes.append(
                f"the task command is {len(tr)} characters; schtasks accepts "
                f"at most {SCHTASKS_TR_MAX}"
            )
        return InstallPlan(
            mechanism, path, render_task_record(argv), argv,
            activate=[
                schtasks_create_argv(argv),
                ["schtasks", "/Run", "/TN", WINDOWS_TASK],
            ],
            notes=notes,
        )
    raise ValueError(f"unknown autostart mechanism: {mechanism}")


def uninstall_commands(mechanism: str, path: Path) -> tuple[list[list[str]], list[list[str]]]:
    """``(commands, fallback)`` that deactivate the registration."""
    if mechanism == "launchd":
        return ([["launchctl", "bootout", f"{_gui_domain()}/{LAUNCHD_LABEL}"]],
                [["launchctl", "unload", "-w", str(path)]])
    if mechanism == "systemd":
        # `daemon-reload` runs after the file is removed (see the CLI).
        return ([["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT]], [])
    if mechanism == "schtasks":
        return ([["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"]], [])
    raise ValueError(f"unknown autostart mechanism: {mechanism}")


def supervisor_commands(action: str, mechanism: str) -> list[list[str]] | None:
    """Commands that ``start``/``stop``/``restart`` delegate to (#76, 1b).

    None means "no supervisor command": Task Scheduler never revives a
    stopped daemon, so ``stop`` there stops the process directly.
    """
    if mechanism == "systemd":
        return [["systemctl", "--user", action, SYSTEMD_UNIT]]
    if mechanism == "launchd":
        target = f"{_gui_domain()}/{LAUNCHD_LABEL}"
        if action == "start":
            return [["launchctl", "kickstart", target]]
        if action == "restart":
            return [["launchctl", "kickstart", "-k", target]]
        if action == "stop":
            # SIGTERM → the daemon exits 0 → KeepAlive(SuccessfulExit=false)
            # leaves it stopped until the next login or `mool daemon start`.
            return [["launchctl", "kill", "SIGTERM", target]]
    if mechanism == "schtasks":
        if action in ("start", "restart"):
            return [["schtasks", "/Run", "/TN", WINDOWS_TASK]]
        return None
    raise ValueError(f"unsupported: {mechanism} {action}")


# ── Inspection ───────────────────────────────────────────────────────

@dataclass
class AutostartState:
    mechanism: str
    path: Path
    exists: bool
    managed: bool
    argv: list[str] | None = None

    @property
    def python(self) -> str | None:
        return self.argv[0] if self.argv else None

    @property
    def python_exists(self) -> bool | None:
        return None if not self.python else Path(self.python).exists()

    def _flag(self, name: str) -> str | None:
        if not self.argv or name not in self.argv:
            return None
        i = self.argv.index(name)
        return self.argv[i + 1] if i + 1 < len(self.argv) else None

    @property
    def host(self) -> str | None:
        return self._flag("--host")

    @property
    def port(self) -> int | None:
        value = self._flag("--port")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def as_dict(self) -> dict:
        return {
            "installed": self.exists,
            "managed": self.managed,
            "mechanism": self.mechanism,
            "path": str(self.path),
            "python": self.python,
            "python_exists": self.python_exists,
        }


def _parse_exec_start(text: str) -> list[str] | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            value = line[len("ExecStart="):].lstrip("-@:+!")
            try:
                words = shlex.split(value)
            except ValueError:
                return None
            return [w.replace("$$", "$").replace("%%", "%") for w in words]
    return None


def parse_argv(mechanism: str, text: str) -> list[str] | None:
    """The command a registration file launches, or None when unreadable."""
    try:
        if mechanism == "launchd":
            data = plistlib.loads(text.encode("utf-8"))
            argv = data.get("ProgramArguments")
            if not argv and data.get("Program"):
                argv = [data["Program"]]
            return [str(a) for a in argv] if argv else None
        if mechanism == "systemd":
            return _parse_exec_start(text)
        if mechanism == "schtasks":
            argv = json.loads(text).get("argv")
            return [str(a) for a in argv] if argv else None
    except (ValueError, plistlib.InvalidFileException, AttributeError, TypeError):
        return None
    return None


def inspect(mechanism: str | None = None, home: Path | None = None) -> AutostartState | None:
    """What is registered right now (file-based; no supervisor command runs)."""
    mechanism = mechanism or detect_mechanism()
    if mechanism is None:
        return None
    path = service_path(mechanism, home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AutostartState(mechanism, path, exists=False, managed=False)
    except (OSError, UnicodeDecodeError):
        return AutostartState(mechanism, path, exists=True, managed=False)
    return AutostartState(mechanism, path, exists=True, managed=MARKER in text,
                          argv=parse_argv(mechanism, text))


def managed_state(home: Path | None = None) -> AutostartState | None:
    """The registration ``install`` created, or None (absent or hand-written)."""
    state = inspect(home=home)
    if state is not None and state.exists and state.managed:
        return state
    return None


# ── Helpers for the CLI ──────────────────────────────────────────────

def _run(cmd: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run one supervisor command. Tests mock this — nothing real is called."""
    from hub.git_utils import no_window
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, **no_window())
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout:g}s")


def display(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def unified_diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{path} (current)", tofile=f"{path} (mool daemon install)",
    ))


def backup_path(path: Path, mechanism: str, home: Path | None = None,
                now: datetime | None = None) -> Path:
    """``<file>.bak-<date>``, next to the file — except for launchd.

    launchd treats every file in ``LaunchAgents`` as a candidate job; a copy
    there with the same label would collide, so that backup goes to
    ``~/.moolmesh`` instead.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    if mechanism == "launchd":
        return (home or Path.home()) / ".moolmesh" / f"{path.name}.bak-{stamp}"
    return path.with_name(f"{path.name}.bak-{stamp}")


def windows_task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", WINDOWS_TASK]).returncode == 0


def windows_task_xml() -> str:
    result = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/XML"])
    return result.stdout if result.returncode == 0 else ""


def linger_enabled(user: str | None = None) -> bool | None:
    """``loginctl show-user <user> -p Linger``: True/False, None when unknown.

    Read-only; ``install`` only reports it and never enables linger itself.
    """
    if user is None:
        import getpass
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            return None
    result = _run(["loginctl", "show-user", user, "-p", "Linger"])
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip().partition("=")[2].strip().lower()
    if value in ("yes", "no"):
        return value == "yes"
    return None
