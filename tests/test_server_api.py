"""Tests for DashboardServer API endpoints."""

import json
import threading
import time
import urllib.request
import urllib.error

import pytest

from hub.cache.event_store import EventStore
from hub.dashboard.server import DashboardServer


@pytest.fixture
def server(tmp_path):
    """Start a DashboardServer on a random port, yield it, then shut down."""
    db = tmp_path / "events.db"
    store = EventStore(db)

    srv = DashboardServer(host="127.0.0.1", port=0)
    srv.event_store = store

    # Start server in background thread
    handler = srv._make_handler()
    import http.server
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    yield srv, port

    httpd.shutdown()


def _get(srv_port, path):
    """Fetch JSON from a local endpoint."""
    url = f"http://127.0.0.1:{srv_port}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


class TestDashboardAPI:
    def test_api_stats_returns_dict(self, server):
        """GET /api/stats should return a dict with expected fields."""
        srv, port = server
        data = _get(port, "/api/stats")
        assert isinstance(data, dict)
        assert "total_events" in data
        assert "total_input_tokens" in data
        assert "total_output_tokens" in data
        assert "tool_calls" in data
        assert "active_providers" in data
        assert "watched_files" in data

    def test_api_recent_returns_dict_with_events_and_max_id(self, server):
        """GET /api/recent should return {events: [...], max_id: int}."""
        srv, port = server
        data = _get(port, "/api/recent")
        assert isinstance(data, dict)
        assert "events" in data
        assert "max_id" in data
        assert isinstance(data["events"], list)
        assert isinstance(data["max_id"], int)

    def test_api_sessions_returns_list(self, server):
        """GET /api/sessions should return a list of projects."""
        srv, port = server
        data = _get(port, "/api/sessions")
        assert isinstance(data, list)

    def test_api_tools_returns_list(self, server):
        """GET /api/tools should return a list."""
        srv, port = server
        data = _get(port, "/api/tools")
        assert isinstance(data, list)

    def test_api_provider_tokens_returns_dict(self, server):
        """GET /api/provider-tokens should return a dict."""
        srv, port = server
        data = _get(port, "/api/provider-tokens")
        assert isinstance(data, dict)

    def test_api_analytics_returns_dict(self, server):
        """GET /api/analytics?period=day should return analytics dict."""
        srv, port = server
        data = _get(port, "/api/analytics?period=day")
        assert isinstance(data, dict)
        assert "total_events" in data
        assert "by_provider" in data
        assert "by_type" in data
        assert "tokens" in data
        assert "period" in data
        assert data["period"] == "day"

    def test_api_analytics_period_full(self, server):
        """GET /api/analytics?period=full should return full period."""
        srv, port = server
        data = _get(port, "/api/analytics?period=full")
        assert data["period"] == "full"

    def test_api_db_stats_returns_dict(self, server):
        """GET /api/db-stats should return a dict with summary stats."""
        srv, port = server
        data = _get(port, "/api/db-stats")
        assert isinstance(data, dict)
        assert "total_events" in data
        assert "by_provider" in data
        assert "top_projects" in data

    def test_api_with_events(self, server):
        """API should return correct data after events are stored."""
        srv, port = server
        # Reset stats to use only our stored events
        srv.stats["total_events"] = 0
        srv.tracker = type(srv.tracker)()
        # Store a fresh event
        srv.event_store.store({
            "provider": "claude", "project": "testproj",
            "event_type": "user", "timestamp": "2026-04-08T10:00:00",
            "summary": "hello", "session_id": "s1",
            "tokens": {"input": 100, "output": 50},
        })

        # Use db-stats which reads from EventStore directly
        data = _get(port, "/api/db-stats")
        assert data["total_events"] == 1
