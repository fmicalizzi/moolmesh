"""`mool daemon stop` waits for the PID; `start` waits out a shutdown (#96).

The daemon PID, its liveness and ``/health`` are simulated: no process is
signalled, no launchctl / systemctl runs (``autostart._run`` is a recorder)
and ``time.sleep`` in ``hub.daemon`` is a no-op, so the waits are counted in
polls, not seconds. The last class measures that every harvester/watcher
loop now stops at once instead of sleeping out its interval.
"""

from __future__ import annotations

import argparse
import collections
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hub import autostart, cli, daemon

PY = "/home/u/.local/pipx/venvs/moolmesh/bin/python"
OLD_PID = 4242
NEW_PID = 5151


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=30):
        self.calls.append(list(cmd))
        if cmd[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(cmd, 0, "Linger=yes\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class FakeDaemon:
    """A daemon PID that stays alive for ``alive_polls`` liveness probes.

    ``alive_polls=None`` never exits. ``/health`` answers only once
    ``serving`` is set (a new daemon that came up) — the dying one never
    serves, which is exactly the window the issue describes.
    """

    def __init__(self, alive_polls: int | None):
        self.alive_polls = alive_polls
        self.probes = 0
        self.serving: dict | None = None

    def alive(self, pid: int) -> bool:
        if pid != OLD_PID:
            return False
        self.probes += 1
        return self.alive_polls is None or self.probes <= self.alive_polls

    def health(self, host, port):
        return self.serving


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".moolmesh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(daemon, "PID_FILE", h / ".moolmesh" / "moolmesh.pid")
    monkeypatch.setattr(daemon, "LOG_FILE", h / ".moolmesh" / "daemon.log")
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "pipx")
    monkeypatch.setattr(daemon, "_is_supervised", lambda: False)
    monkeypatch.setattr(daemon, "_pid_is_supervised", lambda pid: False)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)
    # Never signal anything real.
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon, "_IS_WINDOWS", False)
    daemon.PID_FILE.write_text(str(OLD_PID), encoding="utf-8")
    return h


def _fake(monkeypatch, alive_polls: int | None) -> FakeDaemon:
    fake = FakeDaemon(alive_polls)
    monkeypatch.setattr(daemon, "_pid_alive", fake.alive)
    monkeypatch.setattr(daemon, "_fetch_health", fake.health)
    return fake


def _ns(command: str, **kw) -> argparse.Namespace:
    ns = argparse.Namespace(daemon_command=command, host="localhost", port=None,
                            project=None, providers=None, foreground=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _run(command: str, **kw) -> int:
    try:
        cli.cmd_daemon(_ns(command, **kw))
    except SystemExit as exc:
        return exc.code or 0
    return 0


@pytest.fixture
def launchd(home, monkeypatch):
    """`mool daemon install` done with launchd; commands only recorded."""
    runner = FakeRunner()
    monkeypatch.setattr(autostart, "_run", runner)
    monkeypatch.setattr(autostart, "detect_mechanism", lambda platform=None: "launchd")
    monkeypatch.setattr(autostart.sys, "executable", PY)
    # A daemon is "running" while installing; let the install see none.
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
    assert cli.cmd_daemon_install(argparse.Namespace(
        host="localhost", port=5200, dry_run=False, force=False)) == 0
    runner.calls.clear()
    daemon.PID_FILE.write_text(str(OLD_PID), encoding="utf-8")
    return runner


# ── stop waits for the PID ──────────────────────────────────────────

class TestStopWaits:
    def test_unsupervised_stop_returns_only_after_the_pid_is_gone(
            self, home, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=8)
        assert _run("stop") == 0
        assert fake.probes > 8          # it kept polling until the exit
        assert "daemon stopped" in capsys.readouterr().out
        assert not daemon.PID_FILE.exists()

    def test_unsupervised_stop_reports_a_process_that_will_not_die(
            self, home, monkeypatch, capsys):
        _fake(monkeypatch, alive_polls=None)
        assert _run("stop") == 1
        err = capsys.readouterr().err
        assert "still shutting down" in err and str(OLD_PID) in err

    def test_launchd_stop_waits_for_the_exit(self, launchd, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=20)
        assert _run("stop") == 0
        assert launchd.calls == [
            ["launchctl", "kill", "SIGTERM", "gui/501/dev.moolmesh.daemon"]]
        assert fake.probes > 20
        assert "stopped via launchd" in capsys.readouterr().out

    def test_launchd_stop_timeout_says_still_shutting_down(
            self, launchd, monkeypatch, capsys):
        _fake(monkeypatch, alive_polls=None)
        assert _run("stop") == 1
        captured = capsys.readouterr()
        assert "stopped via" not in captured.out
        assert "still shutting down" in captured.err
        assert f"PID {OLD_PID}" in captured.err


# ── start during the shutdown window ────────────────────────────────

class TestStartDuringShutdown:
    def test_unsupervised_start_waits_then_launches(self, home, monkeypatch, capsys):
        _fake(monkeypatch, alive_polls=10)
        launched = []
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: launched.append(restarted))
        assert _run("start") == 0
        assert launched == [False]
        assert "already running" not in capsys.readouterr().out

    def test_unsupervised_start_with_a_serving_daemon_is_already_running(
            self, home, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=None)
        fake.serving = {"status": "healthy", "pid": OLD_PID}
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: pytest.fail("second daemon"))
        assert _run("start") == 0
        assert f"already running (PID {OLD_PID})" in capsys.readouterr().out

    def test_degraded_still_counts_as_running(self, home, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=None)
        fake.serving = {"status": "degraded", "pid": OLD_PID}
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: pytest.fail("second daemon"))
        assert _run("start") == 0
        assert "already running" in capsys.readouterr().out

    def test_unsupervised_start_gives_up_on_a_stuck_pid(self, home, monkeypatch, capsys):
        _fake(monkeypatch, alive_polls=None)
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: pytest.fail("must not start"))
        assert _run("start") == 1
        captured = capsys.readouterr()
        assert "already running" not in captured.out
        assert "still shutting down" in captured.err

    def test_launchd_start_during_shutdown_ends_with_a_running_daemon(
            self, launchd, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=15)

        def kickstart(cmd, *, timeout=30):
            launchd.calls.append(list(cmd))
            fake.serving = {"status": "healthy", "pid": NEW_PID}
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(autostart, "_run", kickstart)

        assert _run("start") == 0
        assert launchd.calls == [["launchctl", "kickstart", "gui/501/dev.moolmesh.daemon"]]
        out = capsys.readouterr().out
        assert "already running" not in out
        assert "started via launchd" in out and f"(PID {NEW_PID})" in out

    def test_launchd_stop_then_start_back_to_back(self, launchd, monkeypatch, capsys):
        """The reported sequence: stop, then start right away (#96)."""
        fake = _fake(monkeypatch, alive_polls=30)

        def supervisor(cmd, *, timeout=30):
            launchd.calls.append(list(cmd))
            if "kickstart" in cmd:
                fake.serving = {"status": "healthy", "pid": NEW_PID}
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(autostart, "_run", supervisor)

        assert _run("stop") == 0
        assert _run("start") == 0
        assert [c[1] for c in launchd.calls] == ["kill", "kickstart"]
        assert f"PID {NEW_PID}" in capsys.readouterr().out


class TestRestart:
    def test_launchd_restart_ignores_the_old_pid_answering(
            self, launchd, monkeypatch, capsys):
        fake = _fake(monkeypatch, alive_polls=5)
        answers = iter([{"status": "healthy", "pid": OLD_PID}] * 3
                       + [{"status": "healthy", "pid": NEW_PID}] * 50)
        monkeypatch.setattr(daemon, "_fetch_health", lambda h, p: next(answers))
        assert _run("restart") == 0
        assert fake.probes > 5
        out = capsys.readouterr().out
        assert "restarted via launchd" in out and f"(PID {NEW_PID})" in out

    def test_unsupervised_restart_reports_a_stuck_stop(self, home, monkeypatch, capsys):
        _fake(monkeypatch, alive_polls=None)
        monkeypatch.setattr(cli, "_launch_daemon",
                            lambda args, restarted: pytest.fail("must not start"))
        assert _run("restart") == 1
        assert "still shutting down" in capsys.readouterr().err


# ── the pidfile belongs to whoever wrote it last (round 2) ──────────

class _Handover(FakeDaemon):
    """OLD_PID exits after ``alive_polls``; at poll ``handover`` a new
    daemon (a concurrent ``start``) has already rewritten the pidfile."""

    def __init__(self, alive_polls: int, handover: int):
        super().__init__(alive_polls)
        self.handover = handover

    def alive(self, pid: int) -> bool:
        if pid == NEW_PID:
            return True
        result = super().alive(pid)
        if self.probes == self.handover:
            daemon.write_pid(NEW_PID)
        return result


class TestPidfileOwnership:
    def test_stop_does_not_delete_the_next_daemons_pidfile(
            self, home, monkeypatch, capsys):
        """The orchestrator's repro: stop of A in the background, start of
        B meanwhile; A's stop finishing must not orphan B."""
        fake = _Handover(alive_polls=10, handover=4)
        monkeypatch.setattr(daemon, "_pid_alive", fake.alive)
        assert daemon.stop_daemon() is True
        assert daemon.PID_FILE.read_text(encoding="utf-8") == str(NEW_PID)
        assert daemon.read_pid() == NEW_PID      # a later stop still finds B

    def test_forced_stop_does_not_delete_the_next_daemons_pidfile(
            self, home, monkeypatch):
        fake = _Handover(alive_polls=60, handover=55)   # past STOP_GRACE
        monkeypatch.setattr(daemon, "_pid_alive", fake.alive)
        assert daemon.stop_daemon() is True
        assert daemon.PID_FILE.read_text(encoding="utf-8") == str(NEW_PID)

    def test_start_wait_does_not_delete_the_next_daemons_pidfile(
            self, home, monkeypatch):
        fake = _Handover(alive_polls=6, handover=3)
        monkeypatch.setattr(daemon, "_pid_alive", fake.alive)
        monkeypatch.setattr(daemon, "_fetch_health", lambda h, p: None)
        assert daemon.wait_out_shutdown(OLD_PID, "localhost", 5299) == "exited"
        assert daemon.PID_FILE.read_text(encoding="utf-8") == str(NEW_PID)

    def test_read_pid_of_a_dead_pid_keeps_a_rewritten_file(self, home, monkeypatch):
        def alive(pid):
            daemon.write_pid(NEW_PID)   # rewritten between read and check
            return False
        monkeypatch.setattr(daemon, "_pid_alive", alive)
        assert daemon.read_pid() is None
        assert daemon.PID_FILE.read_text(encoding="utf-8") == str(NEW_PID)

    def test_exiting_daemon_leaves_a_newer_pidfile(self, home, monkeypatch):
        server = MagicMock()
        server.start.side_effect = lambda: daemon.write_pid(NEW_PID)
        monkeypatch.setattr("hub.dashboard.server.DashboardServer",
                            lambda **kw: server)
        monkeypatch.setattr("hub.log.setup", lambda **kw: None)
        monkeypatch.setattr(daemon.signal, "signal", lambda *a: None)
        daemon._run_server("127.0.0.1", 5299, None, None)
        assert daemon.PID_FILE.read_text(encoding="utf-8") == str(NEW_PID)

    def test_clear_pidfile_only_removes_its_own_pid(self, home):
        daemon.write_pid(OLD_PID)
        daemon._clear_pidfile(NEW_PID)
        assert daemon.PID_FILE.exists()
        daemon._clear_pidfile(OLD_PID)
        assert not daemon.PID_FILE.exists()
        daemon._clear_pidfile(OLD_PID)            # missing: tolerated
        daemon.PID_FILE.write_text("garbage", encoding="utf-8")
        daemon._clear_pidfile(OLD_PID)            # unreadable: left alone
        assert daemon.PID_FILE.exists()

    def test_write_pid_leaves_no_temp_file(self, home):
        daemon.write_pid(NEW_PID)
        assert [p.name for p in daemon.PID_FILE.parent.iterdir()
                if p.name.startswith("moolmesh.pid")] == ["moolmesh.pid"]


# ── the loops wake on stop (the ~26 s shutdown) ─────────────────────

class _IdleHarvester:
    """Factory for a BaseHarvester with nothing to watch."""

    @staticmethod
    def make(tmp_path):
        from hub.watchers.base import BaseHarvester

        class Idle(BaseHarvester):
            POLL_INTERVAL = 120.0
            CATCHUP = False

            @property
            def provider_name(self) -> str:
                return "idle"

            def discover_files(self, since=None, skip_dir=None):
                return []

            def _parse_and_adapt(self, path, offset):
                return [], offset

        return Idle(MagicMock(), collections.deque())


def _stop_seconds(component) -> float:
    component.start()
    time.sleep(0.2)  # let every loop reach its wait
    began = time.monotonic()
    component.stop()
    return time.monotonic() - began


class TestLoopsWakeOnStop:
    """Before #96 each of these slept its interval and stop() waited the
    5 s join timeout: ~5 s git + 3×5 s GitHub + 5 s workspace ≈ 25 s."""

    def test_github_harvester_stops_at_once(self):
        from hub.harvesters.github_harvester import GitHubHarvester
        store = MagicMock()
        store.list_repos.return_value = []
        h = GitHubHarvester(store, MagicMock())
        assert _stop_seconds(h) < 1.0
        assert not any(t.is_alive() for t in h._threads)

    def test_git_harvester_stops_at_once(self):
        from hub.harvesters.git_harvester import GitHarvester
        store = MagicMock()
        store.list_repos.return_value = []
        h = GitHarvester(store)
        assert _stop_seconds(h) < 1.0
        assert not h._thread.is_alive()

    def test_workspace_watcher_stops_at_once(self, monkeypatch):
        from hub.watchers.workspace_watcher import WorkspaceWatcher
        w = WorkspaceWatcher(MagicMock(), [object()])
        monkeypatch.setattr(w, "scan_root", lambda root: 0)
        assert _stop_seconds(w) < 1.0
        assert not w._thread.is_alive()

    def test_provider_watcher_stops_at_once(self, tmp_path):
        w = _IdleHarvester.make(tmp_path)
        assert _stop_seconds(w) < 1.0
        assert not w.alive

    def test_a_restarted_component_still_waits_its_interval(self):
        """stop() then start() clears the event: the loop sleeps again."""
        from hub.harvesters.git_harvester import GitHarvester
        store = MagicMock()
        store.list_repos.return_value = []
        h = GitHarvester(store)
        h.start()
        h.stop()
        h.start()
        try:
            time.sleep(0.3)
            assert h._thread.is_alive()
            assert store.list_repos.call_count == 2   # one cycle per start
        finally:
            h.stop()
