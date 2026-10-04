"""Tests for issue #55 — failed starts exit non-zero, fixed ports never move.

No test boots a real dashboard: the port logic runs against ephemeral sockets
(bound and closed in the test) or a patched ThreadingHTTPServer, and the CLI
runs against a fake DashboardServer. See tests/conftest.py for the wall-clock
guardrail and the home isolation these rely on.
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
from unittest.mock import MagicMock

import pytest

from hub.dashboard.server import (
    AlreadyRunningError,
    DashboardServer,
    PortUnavailableError,
)


def _bare_server(port: int, *, fixed_port: bool) -> DashboardServer:
    """DashboardServer with only the bind-loop attributes (no stores/threads)."""
    srv = object.__new__(DashboardServer)
    srv.host = "127.0.0.1"
    srv.port = port
    srv.fixed_port = fixed_port
    return srv


def _occupy_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _raise_oserror(*args, **kwargs):
    raise OSError("address already in use")


def _fake_dashboard_server(record: dict, error: Exception | None = None):
    class _FakeServer:
        def __init__(self, **kwargs):
            record.update(kwargs)

        def start(self) -> None:
            if error is not None:
                raise error

    return _FakeServer


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        body = json.dumps({"status": "healthy"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestFixedPort:
    def test_busy_fixed_port_fails_without_trying_next(self, capsys):
        sock, port = _occupy_port()
        try:
            srv = _bare_server(port, fixed_port=True)
            with pytest.raises(PortUnavailableError):
                srv._bind_http_server(MagicMock())
            assert srv.port == port
            out = capsys.readouterr().out
            assert "is in use by another process" in out
            assert "trying next" not in out
        finally:
            sock.close()

    def test_single_attempt_is_made_on_fixed_port(self, monkeypatch):
        attempts = []

        def failing_httpd(*args, **kwargs):
            attempts.append(1)
            raise OSError("address already in use")

        monkeypatch.setattr(http.server, "ThreadingHTTPServer", failing_httpd)
        monkeypatch.setattr("urllib.request.urlopen", _raise_oserror)

        srv = _bare_server(51000, fixed_port=True)
        with pytest.raises(PortUnavailableError):
            srv._bind_http_server(MagicMock())
        assert attempts == [1]
        assert srv.port == 51000


class TestAlreadyRunning:
    def test_healthy_moolmesh_message_and_error(self, capsys):
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            srv = _bare_server(port, fixed_port=True)
            with pytest.raises(AlreadyRunningError):
                srv._bind_http_server(MagicMock())
            out = capsys.readouterr().out
            assert f"MoolMesh is already running on port {port}" in out
            assert "Use 'mool daemon stop' to stop it" in out
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestAutoIncrement:
    def test_interactive_retries_and_announces_final_port(self, monkeypatch, capsys):
        fake_httpd = MagicMock()
        calls = []

        def bind_once_then_fail(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("address already in use")
            return fake_httpd

        monkeypatch.setattr(http.server, "ThreadingHTTPServer", bind_once_then_fail)
        monkeypatch.setattr("urllib.request.urlopen", _raise_oserror)

        srv = _bare_server(5200, fixed_port=False)
        bound = srv._bind_http_server(MagicMock())
        assert bound is fake_httpd
        assert srv.port == 5201
        out = capsys.readouterr().out
        assert "Port 5200 in use, trying next" in out
        assert "Dashboard → http://127.0.0.1:5201" in out

    def test_retries_exhausted_raises(self, monkeypatch, capsys):
        monkeypatch.setattr(http.server, "ThreadingHTTPServer", _raise_oserror)
        monkeypatch.setattr("urllib.request.urlopen", _raise_oserror)

        srv = _bare_server(5200, fixed_port=False)
        with pytest.raises(PortUnavailableError):
            srv._bind_http_server(MagicMock())
        assert srv.port == 5210
        assert "Could not find an available port" in capsys.readouterr().out


class TestRunServerSupervised:
    def test_supervised_forces_fixed_port(self, monkeypatch):
        from hub import daemon
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(server_mod, "DashboardServer", _fake_dashboard_server(record))
        monkeypatch.setenv("INVOCATION_ID", "test-invocation")

        daemon._run_server("127.0.0.1", 5312, None, None, fixed_port=False)
        assert record["fixed_port"] is True

    def test_pid_is_supervised_unreadable_pid_is_false(self):
        from hub import daemon

        assert daemon._pid_is_supervised(999_999_999) is False


class TestDashboardCLIExitCodes:
    def _run(self, monkeypatch, capsys, argv: list[str], error: Exception):
        import hub.cli as cli
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(
            server_mod, "DashboardServer", _fake_dashboard_server(record, error)
        )
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(sys, "argv", argv)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        return exc.value.code, capsys.readouterr(), record

    def test_no_available_port_exits_1(self, monkeypatch, capsys):
        code, streams, _ = self._run(
            monkeypatch,
            capsys,
            ["mool", "dashboard"],
            PortUnavailableError("no available port in 5200-5209"),
        )
        assert code == 1
        assert "dashboard did not start" in streams.err
        assert "no available port" in streams.err

    def test_already_running_exits_1(self, monkeypatch, capsys):
        code, streams, _ = self._run(
            monkeypatch,
            capsys,
            ["mool", "dashboard"],
            AlreadyRunningError("MoolMesh is already running on port 5200"),
        )
        assert code == 1
        assert "already running" in streams.err

    def test_fixed_port_busy_exits_1(self, monkeypatch, capsys):
        code, streams, record = self._run(
            monkeypatch,
            capsys,
            ["mool", "dashboard", "--port", "5311"],
            PortUnavailableError("port 5311 is in use by another process"),
        )
        assert code == 1
        assert record["fixed_port"] is True
        assert "5311 is in use" in streams.err

    def test_interactive_without_port_keeps_auto_increment(self, monkeypatch, capsys):
        import hub.cli as cli
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(server_mod, "DashboardServer", _fake_dashboard_server(record))
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(sys, "argv", ["mool", "dashboard"])

        cli.main()
        assert record["port"] == 5200
        assert record["fixed_port"] is False


class TestDaemonCLIExitCodes:
    def test_supervised_start_failure_exits_1_and_never_says_started(
        self, monkeypatch, capsys
    ):
        import hub.cli as cli
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(
            server_mod,
            "DashboardServer",
            _fake_dashboard_server(record, PortUnavailableError("port 5312 is in use")),
        )
        monkeypatch.setenv("INVOCATION_ID", "test-invocation")
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start", "--port", "5312"])

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert record["fixed_port"] is True
        out, err = capsys.readouterr()
        assert "in the foreground" in out
        assert "daemon started" not in out
        assert "did not start" in err

    def test_supervised_start_reports_stop_not_start(self, monkeypatch, capsys):
        import hub.cli as cli
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(server_mod, "DashboardServer", _fake_dashboard_server(record))
        monkeypatch.setenv("INVOCATION_ID", "test-invocation")
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start"])

        cli.main()
        out = capsys.readouterr().out
        assert "in the foreground" in out
        assert "MoolMesh daemon stopped" in out
        assert "daemon started" not in out
        assert out.index("starting in the foreground") < out.index("daemon stopped")

    def test_foreground_flag_without_invocation_id_is_fixed(self, monkeypatch, capsys):
        import hub.cli as cli
        from hub.dashboard import server as server_mod

        record: dict = {}
        monkeypatch.setattr(
            server_mod,
            "DashboardServer",
            _fake_dashboard_server(record, PortUnavailableError("port 5314 is in use")),
        )
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "start", "--foreground"])

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert record["fixed_port"] is True
        assert "in the foreground" in capsys.readouterr().out

    def test_supervised_stop_warns_about_systemctl(self, monkeypatch, capsys):
        import hub.cli as cli
        import hub.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "read_pid", lambda: 4242)
        monkeypatch.setattr(daemon_mod, "_pid_is_supervised", lambda pid: pid == 4242)
        monkeypatch.setattr(daemon_mod, "stop_daemon", lambda: True)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "stop"])

        cli.main()
        out = capsys.readouterr().out
        assert "systemctl --user stop moolmesh" in out
        assert "MoolMesh daemon stopped" in out

    def test_supervised_restart_warns_about_systemctl(self, monkeypatch, capsys):
        import hub.cli as cli
        import hub.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "daemon_status", lambda: {"pid": 4242})
        monkeypatch.setattr(daemon_mod, "_pid_is_supervised", lambda pid: pid == 4242)
        monkeypatch.setattr(daemon_mod, "stop_daemon", lambda: True)
        monkeypatch.setattr(daemon_mod, "daemonize", lambda **kwargs: 12345)
        monkeypatch.setattr(daemon_mod, "wait_for_daemon_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.setattr(sys, "argv", ["mool", "daemon", "restart"])

        cli.main()
        out = capsys.readouterr().out
        assert "systemctl --user restart moolmesh" in out
        assert "MoolMesh daemon restarted" in out
