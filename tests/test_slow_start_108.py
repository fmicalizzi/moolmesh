"""A start/restart whose new daemon is alive but slow to answer is not an error (#108).

The first start after an upgrade can run a migration that backs up a large
events.db before ``/health`` answers. Every wait here runs on a simulated
clock (``hub.daemon.time`` is replaced): no real sleep, no process is
signalled, no launchctl / systemctl runs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from hub import autostart, cli, daemon
from hub.daemon import DaemonStartError

PY = "/home/u/.local/pipx/venvs/moolmesh/bin/python"
NEW_PID = 5151
OLD_PID = 4242


class FakeClock:
    """``monotonic``/``sleep`` pair: sleeping advances the clock instantly."""

    def __init__(self):
        self.now = 0.0   # starts at 0 so elapsed() is exactly what the waits see

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def elapsed(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(daemon, "time", c)
    return c


def _health_after(clock: FakeClock, seconds: float, pid: int):
    """A /health that answers healthy for ``pid`` once ``seconds`` have passed."""
    def fetch(host, port):
        if clock.elapsed() >= seconds:
            return {"status": "healthy", "pid": pid}
        return None
    return fetch


# ── the shared wait ─────────────────────────────────────────────────

class TestWaitUntilServing:
    def test_alive_process_ready_at_20s_is_waited_for(self, clock):
        notices = []
        outcome = daemon.wait_until_serving(
            lambda: clock.elapsed() >= 20, lambda: True, timeout=15,
            on_slow=lambda: notices.append(clock.elapsed()))
        assert outcome == "ready"
        assert len(notices) == 1 and 15 <= notices[0] < 16   # warned once
        assert 20 <= clock.elapsed() < 21

    def test_process_gone_fails_fast(self, clock):
        assert daemon.wait_until_serving(
            lambda: False, lambda: False, timeout=15) == "exited"
        assert clock.elapsed() < 1

    def test_alive_but_never_ready_times_out_at_the_long_deadline(self, clock):
        notices = []
        outcome = daemon.wait_until_serving(
            lambda: False, lambda: True, timeout=15,
            on_slow=lambda: notices.append(1))
        assert outcome == "timeout"
        assert daemon.START_SLOW_TIMEOUT <= clock.elapsed() < daemon.START_SLOW_TIMEOUT + 1
        assert notices == [1]

    def test_no_known_process_times_out_at_the_short_deadline(self, clock):
        notices = []
        outcome = daemon.wait_until_serving(
            lambda: False, lambda: None, timeout=15,
            on_slow=lambda: notices.append(1))
        assert outcome == "timeout"
        assert 15 <= clock.elapsed() < 16
        assert notices == []


# ── background start without a supervisor ───────────────────────────

class TestWaitForDaemonReady:
    @pytest.fixture
    def terminated(self, monkeypatch):
        killed: list[int] = []
        monkeypatch.setattr(daemon, "_terminate_pid", killed.append)
        return killed

    def test_health_at_20s_with_process_alive_succeeds(self, clock, monkeypatch, terminated):
        monkeypatch.setattr(daemon, "_fetch_health", _health_after(clock, 20, NEW_PID))
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
        notices = []
        daemon.wait_for_daemon_ready(NEW_PID, "localhost", 5600,
                                     on_slow=lambda: notices.append(1))
        assert notices == [1] and terminated == []
        assert clock.elapsed() >= 20

    def test_dead_process_fails_fast(self, clock, monkeypatch, terminated):
        monkeypatch.setattr(daemon, "_fetch_health", lambda h, p: None)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
        with pytest.raises(DaemonStartError, match="exited before becoming healthy"):
            daemon.wait_for_daemon_ready(NEW_PID, "localhost", 5601)
        assert clock.elapsed() < 1 and terminated == []

    def test_long_deadline_fails_and_stops_the_child(self, clock, monkeypatch, terminated):
        monkeypatch.setattr(daemon, "_fetch_health", lambda h, p: None)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
        with pytest.raises(DaemonStartError, match="within 180s"):
            daemon.wait_for_daemon_ready(NEW_PID, "localhost", 5602)
        assert terminated == [NEW_PID]
        assert clock.elapsed() >= daemon.START_SLOW_TIMEOUT


class TestCLIUnsupervisedStart:
    @pytest.fixture
    def launched(self, clock, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(daemon, "read_pid", lambda: None)
        monkeypatch.setattr(daemon, "daemonize", lambda **kw: NEW_PID)
        monkeypatch.setattr(daemon, "_terminate_pid",
                            lambda pid: pytest.fail("a slow start must not be stopped"))
        monkeypatch.setattr(cli, "_autostart_delegates", lambda args: False)

    @pytest.mark.parametrize("command,verb", [("start", "started"),
                                              ("restart", "restarted")])
    def test_slow_start_returns_0_with_one_notice(
            self, clock, launched, monkeypatch, capsys, command, verb):
        monkeypatch.setattr(daemon, "daemon_status", lambda: None)
        monkeypatch.setattr(daemon, "_fetch_health", _health_after(clock, 20, NEW_PID))
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", command, "--port", "5610"])
        cli.main()   # no SystemExit → exit code 0
        out = capsys.readouterr().out
        assert out.count(daemon.SLOW_START_NOTICE) == 1
        assert f"MoolMesh daemon {verb} (PID {NEW_PID})" in out

    def test_child_that_dies_exits_1(self, clock, launched, monkeypatch, capsys):
        monkeypatch.setattr(daemon, "_fetch_health", lambda h, p: None)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start", "--port", "5611"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert clock.elapsed() < 1
        assert "exited before becoming healthy" in capsys.readouterr().err


# ── start/restart delegated to a supervisor (launchd / systemd) ─────

class _Runner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=30):
        self.calls.append(list(cmd))
        if cmd[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(cmd, 0, "Linger=yes\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(params=["launchd", "systemd"])
def supervised(request, tmp_path, monkeypatch):
    """`mool daemon install` done; supervisor commands only recorded."""
    h = tmp_path / "home"
    (h / ".moolmesh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(daemon, "PID_FILE", h / ".moolmesh" / "moolmesh.pid")
    monkeypatch.setattr(daemon, "LOG_FILE", h / ".moolmesh" / "daemon.log")
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "pipx")
    monkeypatch.setattr(daemon, "_is_supervised", lambda: False)
    monkeypatch.setattr(daemon, "_pid_is_supervised", lambda pid: False)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon, "_IS_WINDOWS", False)
    runner = _Runner()
    monkeypatch.setattr(autostart, "_run", runner)
    monkeypatch.setattr(autostart, "detect_mechanism", lambda platform=None: request.param)
    monkeypatch.setattr(autostart.sys, "executable", PY)
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
    assert cli.cmd_daemon_install(argparse.Namespace(
        host="localhost", port=5200, dry_run=False, force=False)) == 0
    runner.calls.clear()

    def no_second_daemon(*a, **k):
        raise AssertionError("a second daemon would be started")
    monkeypatch.setattr(daemon, "daemonize", no_second_daemon)
    return request.param


class SupervisedDaemon:
    """The daemon the supervisor spawns: pidfile at ``spawn_at``, gone at ``dies_at``."""

    def __init__(self, clock: FakeClock, *, spawn_at=1.0, serve_at=None, dies_at=None):
        self.clock, self.spawn_at = clock, spawn_at
        self.serve_at, self.dies_at = serve_at, dies_at
        self.old_alive = True   # the restarted daemon, until the first wait poll

    def _up(self) -> bool:
        t = self.clock.elapsed()
        return t >= self.spawn_at and (self.dies_at is None or t < self.dies_at)

    def read_pid(self):
        if self._up():
            return NEW_PID
        return OLD_PID if self.old_alive else None

    def pid_alive(self, pid):
        if pid == OLD_PID:
            alive, self.old_alive = self.old_alive, False
            return alive
        return pid == NEW_PID and self._up()

    def health(self, host, port):
        t = self.clock.elapsed()
        if self.serve_at is not None and t >= self.serve_at and self._up():
            return {"status": "healthy", "pid": NEW_PID}
        return None


def _run(command: str) -> int:
    ns = argparse.Namespace(daemon_command=command, host="localhost", port=None,
                            project=None, providers=None, foreground=False)
    try:
        cli.cmd_daemon(ns)
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _use(monkeypatch, fake: SupervisedDaemon) -> None:
    monkeypatch.setattr(daemon, "read_pid", fake.read_pid)
    monkeypatch.setattr(daemon, "_pid_alive", fake.pid_alive)
    monkeypatch.setattr(daemon, "_fetch_health", fake.health)


class TestSupervisedSlowStart:
    @pytest.mark.parametrize("command", ["start", "restart"])
    def test_health_at_20s_with_process_alive_returns_0(
            self, supervised, clock, monkeypatch, capsys, command):
        fake = SupervisedDaemon(clock, serve_at=20)
        if command == "start":
            fake.old_alive = False   # nothing running before a start
        _use(monkeypatch, fake)
        assert _run(command) == 0
        out = capsys.readouterr().out
        assert out.count(daemon.SLOW_START_NOTICE) == 1
        assert f"PID {NEW_PID}" in out
        assert clock.elapsed() >= 20

    def test_process_that_dies_fails_fast(self, supervised, clock, monkeypatch, capsys):
        fake = SupervisedDaemon(clock, serve_at=None, dies_at=3)
        _use(monkeypatch, fake)
        assert _run("restart") == 1
        assert clock.elapsed() < 5
        err = capsys.readouterr().err
        assert f"(PID {NEW_PID}) exited before answering" in err

    def test_long_deadline_fails(self, supervised, clock, monkeypatch, capsys):
        fake = SupervisedDaemon(clock, serve_at=None)
        _use(monkeypatch, fake)
        assert _run("restart") == 1
        assert clock.elapsed() >= daemon.START_SLOW_TIMEOUT
        out, err = capsys.readouterr()
        assert out.count(daemon.SLOW_START_NOTICE) == 1
        assert "within 180s" in err

    def test_no_new_process_fails_at_the_short_deadline(
            self, supervised, clock, monkeypatch, capsys):
        fake = SupervisedDaemon(clock, spawn_at=10_000)   # never appears
        fake.old_alive = False
        _use(monkeypatch, fake)
        assert _run("start") == 1
        assert clock.elapsed() < cli.SUPERVISED_START_TIMEOUT + 1
        assert "within 15s" in capsys.readouterr().err
