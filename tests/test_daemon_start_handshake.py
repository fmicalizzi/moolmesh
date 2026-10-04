"""Tests for issue #55b — background start/restart only claims success when healthy.

No test leaves a dashboard running: readiness is exercised against real
ephemeral health servers (closed in the test) or mocked pollers, the Unix
fork test runs a mocked server that exits immediately, and the CLI runs
against patched launchers.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading

import pytest

from hub import daemon
from hub.daemon import DaemonStartError


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    payload: dict = {"status": "healthy", "pid": 0}

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(type(self).payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve_health(payload: dict):
    handler = type("Handler", (_HealthHandler,), {"payload": payload})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1], thread


def _stop_health(httpd, thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="select() on pipes is Unix-only")
class TestReadPidReport:
    def test_reads_pid_line(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"12345\n")
        os.close(write_fd)
        try:
            assert daemon._read_pid_report(read_fd, timeout=1) == 12345
        finally:
            os.close(read_fd)

    def test_eof_returns_none(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            assert daemon._read_pid_report(read_fd, timeout=1) is None
        finally:
            os.close(read_fd)

    def test_timeout_returns_none(self):
        read_fd, write_fd = os.pipe()
        try:
            assert daemon._read_pid_report(read_fd, timeout=0.05) is None
        finally:
            os.close(read_fd)
            os.close(write_fd)


class TestWaitForDaemonReady:
    def test_healthy_pid_match_returns(self):
        httpd, port, thread = _serve_health({"status": "healthy", "pid": os.getpid()})
        try:
            daemon.wait_for_daemon_ready(os.getpid(), "127.0.0.1", port, timeout=5)
        finally:
            _stop_health(httpd, thread)

    def test_dead_child_fails_fast(self, monkeypatch):
        monkeypatch.setattr(daemon, "_fetch_health", lambda host, port: None)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
        with pytest.raises(DaemonStartError, match="exited before becoming healthy"):
            daemon.wait_for_daemon_ready(4242, "127.0.0.1", 65501, timeout=5)

    def test_other_pid_times_out_and_terminates(self, monkeypatch):
        terminated: list[int] = []
        monkeypatch.setattr(
            daemon, "_fetch_health",
            lambda host, port: {"status": "healthy", "pid": 111},
        )
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(daemon, "_terminate_pid", lambda pid: terminated.append(pid))
        monkeypatch.setattr(daemon, "_POLL_INTERVAL", 0.01)

        with pytest.raises(DaemonStartError, match="no healthy answer"):
            daemon.wait_for_daemon_ready(4242, "127.0.0.1", 65500, timeout=0.05)
        assert terminated == [4242]

    def test_timeout_terminates_the_child(self, monkeypatch):
        terminated: list[int] = []
        monkeypatch.setattr(daemon, "_fetch_health", lambda host, port: None)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(daemon, "_terminate_pid", lambda pid: terminated.append(pid))
        monkeypatch.setattr(daemon, "_POLL_INTERVAL", 0.01)

        with pytest.raises(DaemonStartError, match="no healthy answer"):
            daemon.wait_for_daemon_ready(4242, "127.0.0.1", 65502, timeout=0.05)
        assert terminated == [4242]

    def test_wildcard_host_is_probed_on_loopback(self, monkeypatch):
        seen: dict = {}

        def fake_fetch(host, port):
            seen["host"] = host
            return {"status": "healthy", "pid": 4242}

        monkeypatch.setattr(daemon, "_fetch_health", fake_fetch)
        monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)

        daemon.wait_for_daemon_ready(4242, "0.0.0.0", 65503, timeout=5)
        assert seen["host"] == "127.0.0.1"


class TestCLIBackgroundHandshake:
    def _prepare(self, monkeypatch):
        import hub.cli as cli  # noqa: F401 (imported by the caller too)

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(daemon, "read_pid", lambda: None)
        monkeypatch.setattr(daemon, "daemonize", lambda **kwargs: 4242)

    def test_success_prints_started_after_health(self, monkeypatch, capsys):
        import hub.cli as cli

        self._prepare(monkeypatch)
        ready: list = []
        monkeypatch.setattr(
            daemon, "wait_for_daemon_ready",
            lambda pid, host, port: ready.append((pid, host, port)),
        )
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start", "--port", "5555"])

        cli.main()
        out = capsys.readouterr().out
        assert ready == [(4242, "localhost", 5555)]
        assert "MoolMesh daemon started (PID 4242)" in out
        assert "http://localhost:5555" in out

    def test_failed_child_exits_1_with_error_and_log(self, monkeypatch, capsys, tmp_path):
        import hub.cli as cli

        self._prepare(monkeypatch)
        log_file = tmp_path / "daemon.log"
        log_file.write_text("Port 5556 in use by another process\nboom\n", encoding="utf-8")
        monkeypatch.setattr(daemon, "LOG_FILE", log_file)

        def fail(pid, host, port):
            raise DaemonStartError("the daemon exited before becoming healthy (port 5556)")

        monkeypatch.setattr(daemon, "wait_for_daemon_ready", fail)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start", "--port", "5556"])

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert "daemon started" not in out
        assert "did not start" in err
        assert "exited before becoming healthy" in err
        assert "Port 5556 in use by another process" in err
        assert str(log_file) in err

    def test_launcher_error_exits_1(self, monkeypatch, capsys):
        import hub.cli as cli

        self._prepare(monkeypatch)

        def explode(**kwargs):
            raise DaemonStartError("the daemon process exited before reporting its PID")

        monkeypatch.setattr(daemon, "daemonize", explode)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start"])

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "daemon started" not in capsys.readouterr().out

    def test_restart_failure_exits_1_without_restarted(self, monkeypatch, capsys):
        import hub.cli as cli

        self._prepare(monkeypatch)
        monkeypatch.setattr(daemon, "daemon_status", lambda: None)
        monkeypatch.setattr(
            daemon, "wait_for_daemon_ready",
            lambda pid, host, port: (_ for _ in ()).throw(
                DaemonStartError("no healthy answer on port 5557 within 10s")
            ),
        )
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "restart", "--port", "5557"])

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert "restarted" not in out
        assert "did not start" in err

    def test_restart_success_prints_restarted(self, monkeypatch, capsys):
        import hub.cli as cli

        self._prepare(monkeypatch)
        monkeypatch.setattr(daemon, "daemon_status", lambda: None)
        monkeypatch.setattr(daemon, "wait_for_daemon_ready", lambda pid, host, port: None)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "restart", "--port", "5558"])

        cli.main()
        out = capsys.readouterr().out
        assert "MoolMesh daemon restarted (PID 4242)" in out
