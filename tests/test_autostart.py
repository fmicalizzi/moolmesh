"""`mool daemon install` / `uninstall` and supervisor delegation (#76).

Every test runs in a temporary HOME with ``autostart._run`` mocked: nothing
is written to a real LaunchAgents / systemd directory and no launchctl,
systemctl, loginctl or schtasks runs.
"""

from __future__ import annotations

import argparse
import hashlib
import plistlib
import subprocess
from pathlib import Path

import pytest

from hub import autostart, cli, daemon

PY = "/home/u/.local/pipx/venvs/moolmesh/bin/python"
PATH_ENV = "/opt/acme/bin:/usr/local/bin:/usr/bin:/bin"


class FakeRunner:
    """Records supervisor commands; answers each with a configurable result."""

    def __init__(self, linger: str = "yes"):
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()   # command words that make a call fail
        self.linger = linger

    def __call__(self, cmd, *, timeout=30):
        self.calls.append(list(cmd))
        if any(word in self.fail for word in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(cmd, 0, f"Linger={self.linger}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, *words) -> bool:
        return any(all(w in c for w in words) for c in self.calls)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(daemon, "PID_FILE", h / ".moolmesh" / "moolmesh.pid")
    monkeypatch.setattr(daemon, "LOG_FILE", h / ".moolmesh" / "daemon.log")
    monkeypatch.setenv("PATH", PATH_ENV)
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "pipx")
    monkeypatch.setattr(daemon, "_is_supervised", lambda: False)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    return h


@pytest.fixture
def runner(monkeypatch):
    r = FakeRunner()
    monkeypatch.setattr(autostart, "_run", r)
    return r


def _use(monkeypatch, mechanism: str, python: str = PY):
    monkeypatch.setattr(autostart, "detect_mechanism", lambda platform=None: mechanism)
    monkeypatch.setattr(autostart.sys, "executable", python)


def _install(dry_run=False, force=False, host="localhost", port=5200) -> int:
    return cli.cmd_daemon_install(argparse.Namespace(
        host=host, port=port, dry_run=dry_run, force=force))


def _uninstall(dry_run=False) -> int:
    return cli.cmd_daemon_uninstall(argparse.Namespace(dry_run=dry_run))


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ── Generated files ─────────────────────────────────────────────────

class TestGeneratedFiles:
    def test_command_is_always_foreground(self):
        argv = autostart.daemon_argv(PY, "0.0.0.0", 5201)
        assert argv == [PY, "-m", "hub.cli", "daemon", "start", "--foreground",
                        "--host", "0.0.0.0", "--port", "5201"]

    def test_plist_keys(self, tmp_path):
        home = tmp_path / "Users" / "u"
        argv = autostart.daemon_argv(PY, "localhost", 5200)
        text = autostart.render_plist(argv, PATH_ENV, home)
        assert autostart.MARKER in text
        data = plistlib.loads(text.encode())
        assert data["Label"] == autostart.LAUNCHD_LABEL == "dev.moolmesh.daemon"
        assert data["ProgramArguments"] == argv
        assert "--foreground" in data["ProgramArguments"]
        assert data["ProgramArguments"][0] == PY
        assert data["RunAtLoad"] is True
        assert data["KeepAlive"] == {"SuccessfulExit": False}
        log = str(home / ".moolmesh" / "daemon.log")
        assert data["StandardOutPath"] == data["StandardErrorPath"] == log
        assert data["EnvironmentVariables"] == {"PATH": PATH_ENV,
                                                "PYTHONIOENCODING": "utf-8"}

    def test_systemd_unit_keys(self):
        argv = autostart.daemon_argv(PY, "localhost", 5200)
        text = autostart.render_systemd_unit(argv, PATH_ENV)
        assert text.startswith(f"# {autostart.MARKER}")
        lines = text.splitlines()
        assert f"ExecStart={' '.join(argv)}" in lines
        assert "Restart=on-failure" in lines
        assert f"Environment=PATH={PATH_ENV}" in lines
        assert "Environment=PYTHONIOENCODING=utf-8" in lines
        assert "WantedBy=default.target" in lines
        assert autostart.parse_argv("systemd", text) == argv

    def test_systemd_quotes_spaces_and_escapes_specifiers(self):
        python = "/home/u/My Envs/100%/bin/python"
        argv = autostart.daemon_argv(python, "localhost", 5200)
        text = autostart.render_systemd_unit(argv, "/a b:/usr/bin")
        assert 'ExecStart="/home/u/My Envs/100%%/bin/python" -m hub.cli' in text
        assert 'Environment="PATH=/a b:/usr/bin"' in text
        assert autostart.parse_argv("systemd", text)[0] == python

    def test_schtasks_uses_pythonw_and_foreground(self, tmp_path):
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        (scripts / "python.exe").write_text("")
        (scripts / "pythonw.exe").write_text("")
        plan = autostart.build_install_plan(
            "schtasks", "localhost", 5200, python=str(scripts / "python.exe"),
            path_env="", home=tmp_path)
        create = plan.activate[0]
        assert create[:7] == ["schtasks", "/Create", "/SC", "ONLOGON", "/TN",
                              "MoolMesh", "/TR"]
        assert create[-1] == "/F"
        tr = create[7]
        assert "pythonw.exe" in tr and "--foreground" in tr
        assert plan.argv[0].endswith("pythonw.exe")
        assert autostart.MARKER in plan.content

    def test_path_of_the_install_moment_is_recorded_without_duplicates(self, tmp_path):
        plan = autostart.build_install_plan(
            "systemd", "localhost", 5200, python=PY,
            path_env="/opt/gh/bin:/usr/bin:/opt/gh/bin", home=tmp_path)
        assert "Environment=PATH=/opt/gh/bin:/usr/bin\n" in plan.content


# ── install / uninstall ─────────────────────────────────────────────

class TestInstallSystemd:
    def test_install_writes_unit_and_enables_it(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _install() == 0
        unit = home / ".config" / "systemd" / "user" / "moolmesh.service"
        assert autostart.MARKER in unit.read_text()
        assert runner.ran("systemctl", "--user", "daemon-reload")
        assert runner.ran("systemctl", "--user", "enable", "moolmesh.service")
        assert runner.ran("systemctl", "--user", "restart", "moolmesh.service")

    def test_install_twice_leaves_one_registration(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _install() == 0
        first = (home / ".config/systemd/user/moolmesh.service").read_text()
        assert _install() == 0
        files = list((home / ".config" / "systemd" / "user").iterdir())
        assert [f.name for f in files] == ["moolmesh.service"]
        assert files[0].read_text() == first

    def test_dry_run_writes_and_runs_nothing(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd")
        before = _tree_hash(home)
        assert _install(dry_run=True) == 0
        assert _tree_hash(home) == before
        assert runner.calls == []
        out = capsys.readouterr().out
        assert "--foreground" in out and "systemctl --user enable moolmesh.service" in out

    def test_hand_written_unit_is_not_overwritten(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd")
        unit = home / ".config" / "systemd" / "user" / "moolmesh.service"
        unit.parent.mkdir(parents=True)
        own = "[Service]\nExecStart=/usr/local/bin/mool daemon start --foreground\n"
        unit.write_text(own)
        assert _install() == 1
        assert unit.read_text() == own
        assert runner.calls == []
        err = capsys.readouterr().err
        assert str(unit) in err and "--force" in err
        assert "+ExecStart=" in err  # the diff against what it would write

    def test_force_backs_up_then_replaces(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        unit = home / ".config" / "systemd" / "user" / "moolmesh.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("hand written\n")
        assert _install(force=True, host="0.0.0.0") == 0
        backups = list(unit.parent.glob("moolmesh.service.bak-*"))
        assert len(backups) == 1 and backups[0].read_text() == "hand written\n"
        assert autostart.MARKER in unit.read_text()
        assert "--host 0.0.0.0" in unit.read_text()

    def test_linger_off_warns_and_runs_nothing_about_it(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd")
        runner.linger = "no"
        assert _install() == 0
        out = capsys.readouterr().out
        assert 'loginctl enable-linger "$USER"' in out
        assert runner.ran("loginctl", "show-user")
        assert not runner.ran("enable-linger")
        assert not any("sudo" in c for c in runner.calls)

    def test_a_manual_daemon_is_stopped_before_the_supervisor_starts(
            self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        stopped = []
        monkeypatch.setattr(daemon, "read_pid", lambda: 4242)
        monkeypatch.setattr(daemon, "_pid_is_supervised", lambda pid: False)
        monkeypatch.setattr(daemon, "stop_daemon", lambda: stopped.append(True) or True)
        assert _install() == 0
        assert stopped == [True]

    def test_dev_venv_interpreter_warns(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd")
        monkeypatch.setattr(cli, "_detect_install_method", lambda: "venv")
        assert _install(dry_run=True) == 0
        assert "development virtualenv" in capsys.readouterr().out

    def test_failed_systemctl_is_an_error(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        runner.fail = {"enable"}
        assert _install() == 1

    def test_uninstall_removes_only_its_own_file(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _install() == 0
        runner.calls.clear()
        assert _uninstall() == 0
        assert not (home / ".config/systemd/user/moolmesh.service").exists()
        assert runner.ran("systemctl", "--user", "disable", "--now", "moolmesh.service")
        assert runner.ran("daemon-reload")

    def test_uninstall_leaves_a_hand_written_unit_alone(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        unit = home / ".config" / "systemd" / "user" / "moolmesh.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("hand written\n")
        assert _uninstall() == 1
        assert unit.read_text() == "hand written\n"
        assert runner.calls == []

    def test_uninstall_dry_run_changes_nothing(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _install() == 0
        runner.calls.clear()
        before = _tree_hash(home)
        assert _uninstall(dry_run=True) == 0
        assert _tree_hash(home) == before and runner.calls == []

    def test_uninstall_when_nothing_is_installed(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _uninstall() == 0
        assert runner.calls == []


class TestInstallLaunchd:
    def test_install_bootstraps_the_agent(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        assert _install() == 0
        plist = home / "Library" / "LaunchAgents" / "dev.moolmesh.daemon.plist"
        data = plistlib.loads(plist.read_bytes())
        assert "--foreground" in data["ProgramArguments"]
        assert runner.ran("launchctl", "bootstrap", "gui/501", str(plist))

    def test_bootstrap_failure_falls_back_to_load_w(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        runner.fail = {"bootstrap"}
        assert _install() == 0
        assert runner.ran("launchctl", "load", "-w")

    def test_install_twice_leaves_one_agent(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        assert _install() == 0 and _install() == 0
        agents = list((home / "Library" / "LaunchAgents").iterdir())
        assert [a.name for a in agents] == ["dev.moolmesh.daemon.plist"]

    def test_force_backup_goes_outside_launchagents(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "dev.moolmesh.daemon.plist").write_text("<plist/>")
        assert _install() == 1
        assert _install(force=True) == 0
        assert [a.name for a in agents.iterdir()] == ["dev.moolmesh.daemon.plist"]
        assert list((home / ".moolmesh").glob("dev.moolmesh.daemon.plist.bak-*"))

    def test_uninstall_boots_out(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        assert _install() == 0
        assert _uninstall() == 0
        assert runner.ran("launchctl", "bootout", "gui/501/dev.moolmesh.daemon")
        assert not (home / "Library/LaunchAgents/dev.moolmesh.daemon.plist").exists()


class TestInstallWindows:
    def test_install_creates_the_logon_task(self, home, runner, monkeypatch):
        _use(monkeypatch, "schtasks", python="C:\\u\\pipx\\venvs\\moolmesh\\Scripts\\python.exe")
        runner.fail = {"/Query"}  # no task yet
        assert _install() == 0
        assert runner.ran("schtasks", "/Create", "/SC", "ONLOGON", "/TN", "MoolMesh")
        assert runner.ran("schtasks", "/Run", "/TN", "MoolMesh")
        record = home / ".moolmesh" / "autostart-task.json"
        assert autostart.MARKER in record.read_text()

    def test_a_foreign_task_is_not_replaced(self, home, runner, monkeypatch):
        _use(monkeypatch, "schtasks", python="C:\\u\\Scripts\\python.exe")
        assert _install() == 1          # /Query succeeds: a task exists, no marker
        assert not runner.ran("/Create")


# ── start / stop / restart with autostart installed (1b) ────────────

class TestDelegation:
    @pytest.fixture
    def installed(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        assert _install(port=5207) == 0
        runner.calls.clear()

        def no_second_daemon(*a, **k):
            raise AssertionError("a second daemon would be started")
        monkeypatch.setattr(daemon, "daemonize", no_second_daemon)
        monkeypatch.setattr(daemon, "stop_daemon", no_second_daemon)
        monkeypatch.setattr(daemon, "read_pid", lambda: None)
        monkeypatch.setattr(daemon, "_fetch_health",
                            lambda host, port: {"status": "healthy", "pid": 777, "port": port})
        return runner

    def _daemon(self, command, **kw):
        ns = argparse.Namespace(daemon_command=command, host="localhost", port=None,
                                project=None, providers=None, foreground=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_daemon(ns)
        return exc.value.code

    @pytest.mark.parametrize("action", ["start", "stop", "restart"])
    def test_delegates_to_systemctl(self, installed, action):
        assert self._daemon(action) == 0
        assert installed.calls == [["systemctl", "--user", action, "moolmesh.service"]]

    def test_start_waits_on_the_registered_port(self, installed, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(daemon, "_fetch_health",
                            lambda host, port: seen.append(port) or {"status": "healthy", "pid": 9})
        assert self._daemon("start") == 0
        assert seen == [5207]
        assert "PID 9" in capsys.readouterr().out

    def test_foreground_start_is_the_supervised_process_not_delegated(
            self, installed, monkeypatch):
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        cli.cmd_daemon(argparse.Namespace(
            daemon_command="start", host="localhost", port=5207, project=None,
            providers=None, foreground=True))
        assert launched == [False]
        assert installed.calls == []

    def test_supervisor_failure_is_reported(self, installed):
        installed.fail = {"restart"}
        assert self._daemon("restart") == 1

    def test_launchd_uses_kickstart_and_kill(self, home, runner, monkeypatch):
        _use(monkeypatch, "launchd")
        assert _install() == 0
        assert autostart.supervisor_commands("restart", "launchd") == [
            ["launchctl", "kickstart", "-k", "gui/501/dev.moolmesh.daemon"]]
        assert autostart.supervisor_commands("stop", "launchd") == [
            ["launchctl", "kill", "SIGTERM", "gui/501/dev.moolmesh.daemon"]]

    def test_without_autostart_start_is_unchanged(self, home, runner, monkeypatch):
        _use(monkeypatch, "systemd")
        launched = []
        monkeypatch.setattr(daemon, "read_pid", lambda: None)
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        cli.cmd_daemon(argparse.Namespace(
            daemon_command="start", host="localhost", port=None, project=None,
            providers=None, foreground=False))
        assert launched == [False] and runner.calls == []


# ── status / doctor ─────────────────────────────────────────────────

class TestStatusAndDoctor:
    def test_doctor_flags_a_unit_whose_interpreter_is_gone(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd", python="/home/u/gone/venv/bin/python")
        assert _install() == 0
        info = cli._autostart_summary()
        assert info["python_exists"] is False
        assert cli._doctor_autostart(info) == 1
        out = capsys.readouterr().out
        assert "no longer exists" in out and "mool daemon install" in out

    def test_doctor_flags_a_plist_whose_interpreter_is_gone(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "launchd", python="/Users/u/old/bin/python3")
        assert _install() == 0
        assert cli._doctor_autostart(cli._autostart_summary()) == 1

    def test_doctor_accepts_a_live_interpreter(self, home, runner, monkeypatch, tmp_path):
        python = tmp_path / "python"
        python.write_text("")
        _use(monkeypatch, "systemd", python=str(python))
        assert _install() == 0
        assert cli._doctor_autostart(cli._autostart_summary()) == 0

    def test_status_shows_autostart(self, home, runner, monkeypatch, capsys):
        _use(monkeypatch, "systemd")
        monkeypatch.setattr(daemon, "daemon_status", lambda: None)
        cli._print_daemon_status()
        assert "Autostart: not installed" in capsys.readouterr().out
        assert _install() == 0
        capsys.readouterr()
        cli._print_daemon_status()
        out = capsys.readouterr().out
        assert "Autostart:" in out and "moolmesh.service" in out
