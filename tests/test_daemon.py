"""Tests for hub.daemon — OS resilience (Windows graceful degradation)."""

from __future__ import annotations

import signal
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from hub import daemon


class TestReadPidEncoding:
    def test_read_text_uses_utf8(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        with patch("os.kill"):
            assert daemon.read_pid() == 12345

    def test_write_text_uses_utf8(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        pid_file = tmp_path / "moolmesh.pid"
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        daemon.write_pid(99999)
        assert pid_file.read_text(encoding="utf-8").strip() == "99999"

    def test_stale_pid_oserror_clears_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("42", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        with patch("os.kill", side_effect=OSError("[WinError 11]")):
            assert daemon.read_pid() is None
        assert not pid_file.exists()


class TestDaemonizeWindows:
    def test_launches_background_process(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "moolmesh.pid")
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "daemon.log")

        mock_proc = MagicMock()
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = daemon.daemonize("0.0.0.0", 9876, None, None)

        assert pid == 12345
        assert (tmp_path / "moolmesh.pid").read_text(encoding="utf-8") == "12345"
        call_args = mock_popen.call_args
        assert "dashboard" in call_args[0][0]
        assert call_args[1]["creationflags"] == 0x08000000
        assert call_args[1]["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_passes_project_and_providers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "moolmesh.pid")
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "daemon.log")

        mock_proc = MagicMock()
        mock_proc.pid = 999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            daemon.daemonize("127.0.0.1", 5200, "myproject", ["claude", "codex"])

        cmd = mock_popen.call_args[0][0]
        assert "--project" in cmd
        assert "myproject" in cmd
        assert "--providers" in cmd
        assert "claude,codex" in cmd


class TestStopDaemonWindows:
    def test_uses_taskkill_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("42", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)

        call_count = 0
        def fake_kill(pid, sig):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # read_pid's os.kill(pid, 0) probe
            raise ProcessLookupError

        monkeypatch.setattr("os.kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

        with patch("subprocess.run") as mock_run:
            daemon.stop_daemon()
            mock_run.assert_called_once_with(
                ["taskkill", "/PID", "42"], capture_output=True
            )

    def test_uses_taskkill_force_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("42", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)

        def fake_kill(pid, sig):
            if sig == 0:
                return  # process is "alive"

        monkeypatch.setattr("os.kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

        with patch("subprocess.run") as mock_run:
            result = daemon.stop_daemon()
            assert result is True
            force_call = [c for c in mock_run.call_args_list if "/F" in c[0][0]]
            assert len(force_call) == 1
            assert force_call[0][0][0] == ["taskkill", "/F", "/PID", "42"]


class TestSignalHandlerOSError:
    def test_signal_sigterm_oserror_is_caught(self, monkeypatch, tmp_path):
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("1", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)

        original_signal = signal.signal

        def failing_signal(signum, handler):
            if signum == signal.SIGTERM:
                raise OSError("not supported")
            return original_signal(signum, handler)

        mock_server = MagicMock()
        mock_server.start.return_value = None

        with patch("signal.signal", side_effect=failing_signal), \
             patch("hub.dashboard.server.DashboardServer", return_value=mock_server), \
             patch("hub.log.setup"):
            daemon._run_server("127.0.0.1", 9876, None, None)
        mock_server.start.assert_called_once()


class TestStopDaemonUnix:
    def test_uses_sigkill_on_unix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", False)
        pid_file = tmp_path / "moolmesh.pid"
        pid_file.write_text("42", encoding="utf-8")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)

        kill_signals = []
        def fake_kill(pid, sig):
            kill_signals.append(sig)
            if sig == 0:
                return
            if sig == signal.SIGTERM:
                return
            if sig == signal.SIGKILL:
                return

        monkeypatch.setattr("os.kill", fake_kill)
        monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

        daemon.stop_daemon()
        assert signal.SIGKILL in kill_signals
