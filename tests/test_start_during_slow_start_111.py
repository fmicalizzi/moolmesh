"""`mool daemon start` tells a starting daemon from a stopping one (#111).

A live PID whose ``/health`` is down is shutting down only when a stop was
requested for it (``~/.moolmesh/moolmesh.stopping`` names it); otherwise it
is still starting — e.g. a migration backing up events.db — and ``start``
waits for it and answers "already running". PID liveness, ``/health`` and
the clock are simulated (``hub.daemon.time`` is replaced): no real sleep, no
process is signalled, no launchctl / systemctl runs.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hub import autostart, cli, daemon

PY = "/home/u/.local/pipx/venvs/moolmesh/bin/python"
PID = 4242
OTHER_PID = 7373
NEW_PID = 5151


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return 1_000_000.0 + self.now


class World:
    """PID, /health and clock. ``alive_until``/``serves_at`` are seconds on
    the fake clock (None = never exits / never serves)."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.alive_until: float | None = None
        self.serves_at: float | None = None
        self.serving_pid = PID
        self.others_alive: set[int] = set()

    def alive(self, pid: int) -> bool:
        if pid == PID:
            return self.alive_until is None or self.clock.now < self.alive_until
        return pid in self.others_alive

    def health(self, host, port):
        if self.serves_at is not None and self.clock.now >= self.serves_at:
            return {"status": "healthy", "pid": self.serving_pid}
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".moolmesh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(daemon, "CONFIG_DIR", h / ".moolmesh")
    monkeypatch.setattr(daemon, "PID_FILE", h / ".moolmesh" / "moolmesh.pid")
    monkeypatch.setattr(daemon, "STOPPING_FILE", h / ".moolmesh" / "moolmesh.stopping")
    monkeypatch.setattr(daemon, "LOG_FILE", h / ".moolmesh" / "daemon.log")
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "pipx")
    monkeypatch.setattr(daemon, "_is_supervised", lambda: False)
    monkeypatch.setattr(daemon, "_pid_is_supervised", lambda pid: False)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon, "_IS_WINDOWS", False)
    clock = FakeClock()
    monkeypatch.setattr(daemon, "time", clock)
    w = World(clock)
    monkeypatch.setattr(daemon, "_pid_alive", w.alive)
    monkeypatch.setattr(daemon, "_fetch_health", w.health)
    daemon.PID_FILE.write_text(str(PID), encoding="utf-8")
    return w


def _run(command: str) -> int:
    ns = argparse.Namespace(daemon_command=command, host="localhost", port=None,
                            project=None, providers=None, foreground=False)
    try:
        cli.cmd_daemon(ns)
    except SystemExit as exc:
        return exc.code or 0
    return 0


@pytest.fixture
def no_launch(monkeypatch):
    monkeypatch.setattr(cli, "_launch_daemon",
                        lambda args, restarted: pytest.fail("second daemon"))


# ── start during a slow start ───────────────────────────────────────

class TestStartDuringSlowStart:
    def test_waits_past_the_stop_timeout_and_says_already_running(
            self, world, no_launch, capsys):
        """The issue's repro: healthy at 45 s, past the old 30 s give-up."""
        world.serves_at = 45.0
        assert _run("start") == 0
        out = capsys.readouterr()
        assert f"already running (PID {PID})" in out.out
        assert "is starting" in out.out
        assert "migrating databases" in out.out           # slow notice, once
        assert out.out.count("migrating databases") == 1
        assert "shutting down" not in out.out + out.err
        assert 45 <= world.clock.now < 46

    def test_quick_start_answers_without_the_slow_notice(
            self, world, no_launch, capsys):
        world.serves_at = 3.0
        assert _run("start") == 0
        out = capsys.readouterr().out
        assert "already running" in out and "migrating" not in out

    def test_start_that_dies_goes_on_with_a_normal_start(
            self, world, monkeypatch, capsys):
        world.alive_until = 20.0
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        assert _run("start") == 0
        assert launched == [False]
        assert not daemon.PID_FILE.exists()
        assert "already running" not in capsys.readouterr().out

    def test_start_that_never_answers_gives_up_at_the_slow_deadline(
            self, world, no_launch, capsys):
        assert _run("start") == 1
        err = capsys.readouterr().err
        assert f"PID {PID}" in err and "did not answer" in err
        assert "shutting down" not in err
        assert world.clock.now >= daemon.START_SLOW_TIMEOUT

    def test_a_stop_arriving_mid_start_switches_to_the_shutdown_wait(
            self, world, monkeypatch, capsys):
        world.alive_until = 12.0
        real_alive = world.alive

        def alive(pid):
            if world.clock.now >= 5.0 and not daemon.STOPPING_FILE.exists() \
                    and world.clock.now < 6.0:
                daemon.mark_stop_requested(PID)
            return real_alive(pid)
        monkeypatch.setattr(daemon, "_pid_alive", alive)
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        assert _run("start") == 0
        assert launched == [False]
        assert "shutting down" in capsys.readouterr().out

    def test_launchd_start_during_slow_start_does_not_kickstart(
            self, world, monkeypatch, capsys):
        calls = []

        def runner(cmd, *, timeout=30):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(autostart, "_run", runner)
        monkeypatch.setattr(autostart, "detect_mechanism", lambda platform=None: "launchd")
        monkeypatch.setattr(autostart.sys, "executable", PY)
        world.alive_until = 0.0          # the install sees no daemon
        assert cli.cmd_daemon_install(argparse.Namespace(
            host="localhost", port=5200, dry_run=False, force=False)) == 0
        calls.clear()
        daemon.PID_FILE.write_text(str(PID), encoding="utf-8")
        world.alive_until = None
        world.serves_at = world.clock.now + 40.0
        assert _run("start") == 0
        assert calls == []
        assert f"already running (PID {PID})" in capsys.readouterr().out


# ── start during a shutdown: unchanged (#96) ────────────────────────

class TestStartDuringShutdown:
    def test_stopping_daemon_is_waited_out_then_started(
            self, world, monkeypatch, capsys):
        daemon.mark_stop_requested(PID)
        world.alive_until = 8.0
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        assert _run("start") == 0
        assert launched == [False]
        out = capsys.readouterr().out
        assert "shutting down" in out and "is starting" not in out
        assert not daemon.STOPPING_FILE.exists()   # cleared with the pidfile

    def test_stopping_daemon_that_hangs_says_still_shutting_down(
            self, world, no_launch, capsys):
        daemon.mark_stop_requested(PID)
        assert _run("start") == 1
        err = capsys.readouterr().err
        assert "still shutting down" in err and f"PID {PID}" in err
        # Gave up at the #96 timeout, not the slow-start one.
        assert world.clock.now < daemon.STOP_TIMEOUT + 1


# ── the marker never blocks a future start ──────────────────────────

class TestOrphanMarker:
    def test_marker_for_a_dead_pid_is_ignored_and_removed(
            self, world, no_launch, capsys):
        daemon.STOPPING_FILE.write_text(str(OTHER_PID), encoding="utf-8")
        world.serves_at = 2.0
        assert _run("start") == 0
        assert "already running" in capsys.readouterr().out
        assert not daemon.STOPPING_FILE.exists()

    def test_marker_for_a_dead_pid_with_nothing_running_starts(
            self, world, monkeypatch):
        world.alive_until = 0.0
        daemon.STOPPING_FILE.write_text(str(PID), encoding="utf-8")
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        assert _run("start") == 0
        assert launched == [False]
        assert not daemon.STOPPING_FILE.exists()

    def test_garbage_marker_is_removed(self, world):
        daemon.STOPPING_FILE.write_text("not a pid", encoding="utf-8")
        assert daemon.stop_requested(PID) is False
        assert not daemon.STOPPING_FILE.exists()

    def test_marker_naming_another_live_pid_is_kept_but_not_ours(self, world):
        world.others_alive.add(OTHER_PID)
        daemon.mark_stop_requested(OTHER_PID)
        assert daemon.stop_requested(PID) is False
        assert daemon.stop_requested(OTHER_PID) is True

    def test_reused_pid_does_not_inherit_a_stale_marker(self, world):
        daemon.mark_stop_requested(PID)
        daemon.write_pid(PID)                 # a new daemon got the same PID
        assert daemon.stop_requested(PID) is False

    def test_clear_only_removes_its_own_pid(self, world):
        daemon.mark_stop_requested(PID)
        daemon.clear_stop_requested(OTHER_PID)
        assert daemon.STOPPING_FILE.exists()
        daemon.clear_stop_requested(PID)
        assert not daemon.STOPPING_FILE.exists()
        daemon.clear_stop_requested(PID)      # missing: tolerated


# ── who writes the marker ───────────────────────────────────────────

class TestMarkerWriters:
    def test_stop_daemon_marks_then_clears(self, world, monkeypatch):
        seen = []
        real = daemon._pid_alive

        def alive(pid):
            seen.append(daemon.STOPPING_FILE.exists())
            return real(pid)
        world.alive_until = 1.0
        monkeypatch.setattr(daemon, "_pid_alive", alive)
        assert daemon.stop_daemon() is True
        assert any(seen)                       # marked while it was dying
        assert not daemon.STOPPING_FILE.exists()

    def test_sigterm_handler_marks_the_daemon_itself(self, world, monkeypatch):
        """A stop sent by launchd/systemd directly still marks the PID."""
        handlers = {}
        monkeypatch.setattr(daemon.signal, "signal",
                            lambda sig, fn: handlers.__setitem__(sig, fn))
        monkeypatch.setattr("hub.log.setup", lambda **kw: None)
        monitor = {}

        def serve():
            with pytest.raises(KeyboardInterrupt):
                handlers[daemon.signal.SIGTERM](daemon.signal.SIGTERM, None)
            monitor["marked"] = daemon.STOPPING_FILE.read_text(encoding="utf-8")
        server = MagicMock()
        server.start.side_effect = serve
        monkeypatch.setattr("hub.dashboard.server.DashboardServer",
                            lambda **kw: server)
        daemon.write_pid(daemon.os.getpid())
        daemon._run_server("127.0.0.1", 5299, None, None)
        assert monitor["marked"] == str(daemon.os.getpid())
        assert not daemon.STOPPING_FILE.exists()   # cleared on exit

    def test_supervised_stop_marks_the_old_pid(self, world, monkeypatch, capsys):
        calls = []

        def runner(cmd, *, timeout=30):
            calls.append(list(cmd))
            if "kill" in cmd:
                assert daemon.stop_requested(PID) is True
                world.alive_until = world.clock.now + 3.0
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(autostart, "_run", runner)
        monkeypatch.setattr(autostart, "detect_mechanism", lambda platform=None: "launchd")
        monkeypatch.setattr(autostart.sys, "executable", PY)
        world.alive_until = 0.0
        assert cli.cmd_daemon_install(argparse.Namespace(
            host="localhost", port=5200, dry_run=False, force=False)) == 0
        calls.clear()
        daemon.PID_FILE.write_text(str(PID), encoding="utf-8")
        world.alive_until = None
        assert _run("stop") == 0
        assert [c[1] for c in calls] == ["kill"]
