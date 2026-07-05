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


class TestDaemonizeWindows:
    def test_exits_on_windows(self, monkeypatch):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        with pytest.raises(SystemExit, match="1"):
            daemon.daemonize("0.0.0.0", 9876, None, None)

    def test_prints_message_on_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
        with pytest.raises(SystemExit):
            daemon.daemonize("0.0.0.0", 9876, None, None)
        assert "not available on Windows" in capsys.readouterr().err


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
