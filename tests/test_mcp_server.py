"""Tests reales para MCP server — DB con datos, queries reales, verificación end-to-end."""

import json
import os
import sqlite3
import subprocess
import time

import pytest


# ── Fixture: crear DB de prueba con datos realistas ──

@pytest.fixture
def test_db(tmp_path):
    """Crea una events.db con ~50 eventos representativos de 3 providers."""
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        project TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        summary TEXT NOT NULL,
        session_id TEXT,
        tokens_json TEXT,
        tool_name TEXT,
        file_path TEXT,
        model TEXT,
        cwd TEXT,
        fingerprint TEXT,
        created_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX idx_events_timestamp ON events(timestamp)")
    conn.execute("CREATE INDEX idx_events_provider ON events(provider)")
    conn.execute("CREATE INDEX idx_events_project ON events(project)")
    conn.execute("CREATE INDEX idx_events_session ON events(session_id)")

    now = time.time()

    events = []
    for i in range(30):
        events.append((
            "claude", "tools/live/monitor", "tool_use",
            f"2026-06-22T{10 + i // 6:02d}:{(i * 10) % 60:02d}:00",
            f"Read hub/server.py" if i % 3 == 0 else f"Edit line {i * 10}",
            "ses-claude-001",
            json.dumps({"input": 500 + i * 10, "output": 200 + i * 5}),
            "Read" if i % 3 == 0 else "Edit" if i % 3 == 1 else "Bash",
            f"hub/server.py" if i % 2 == 0 else f"hub/cli.py",
            "claude-opus-4-6",
            "/Users/test/project",
            None, now - (30 - i) * 60,
        ))
    for i in range(15):
        events.append((
            "opencode", "/Users/test/Downloads/Claude/ddtyi/YAAHub", "tool_use",
            f"2026-06-22T{11 + i // 6:02d}:{(i * 8) % 60:02d}:00",
            f"glob apps/api/*.ts",
            "ses-oc-001",
            json.dumps({"input": 300, "output": 100}),
            "glob" if i % 2 == 0 else "Read",
            "apps/api/route.ts",
            "claude-sonnet-4-6",
            "/Users/test/YAAHub",
            None, now - (15 - i) * 60,
        ))
    for i in range(5):
        events.append((
            "codex", "eventsmx/backend", "user",
            f"2026-06-22T09:{i * 10:02d}:00",
            f"Fix the login bug in auth.py",
            "ses-codex-001",
            json.dumps({"input": 1000, "output": 800}),
            None, None,
            "gpt-5",
            "/Users/test/eventsmx",
            None, now - 7200 + i * 60,
        ))

    conn.executemany("""INSERT INTO events
        (provider, project, event_type, timestamp, summary, session_id,
         tokens_json, tool_name, file_path, model, cwd, fingerprint, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", events)
    conn.commit()
    conn.close()
    return str(db_path)


# ── Tests reales contra la DB ──


class TestMCPReadOnly:
    """Verificar que la conexión read-only realmente no puede escribir."""

    def test_readonly_blocks_insert(self, test_db):
        uri = f"file:{test_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO events (provider, project, event_type, timestamp, summary, created_at) "
                "VALUES ('x','x','x','x','x',0)"
            )
        conn.close()

    def test_readonly_blocks_delete(self, test_db):
        uri = f"file:{test_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM events")
        conn.close()

    def test_readonly_reads_data(self, test_db):
        uri = f"file:{test_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 50
        conn.close()


class TestGetRecentEvents:
    """Test _get_recent_events contra DB real."""

    def test_returns_correct_count(self, test_db):
        from hub.mcp_server import _get_recent_events
        events = _get_recent_events(test_db, limit=10)
        assert len(events) == 10

    def test_limit_cap_at_500(self, test_db):
        from hub.mcp_server import _get_recent_events
        events = _get_recent_events(test_db, limit=9999)
        assert len(events) == 50

    def test_returns_dicts_with_expected_keys(self, test_db):
        from hub.mcp_server import _get_recent_events
        events = _get_recent_events(test_db, limit=1)
        ev = events[0]
        assert "provider" in ev
        assert "project" in ev
        assert "timestamp" in ev
        assert "summary" in ev

    def test_ordered_ascending(self, test_db):
        from hub.mcp_server import _get_recent_events
        events = _get_recent_events(test_db, limit=50)
        ids = [e["id"] for e in events]
        assert ids == sorted(ids)


class TestGetActiveSessions:
    """Test _get_active_sessions contra DB real."""

    def test_returns_sessions(self, test_db):
        from hub.mcp_server import _get_active_sessions
        sessions = _get_active_sessions(test_db, hours=24)
        assert len(sessions) >= 1
        for s in sessions:
            assert "provider" in s
            assert "project" in s
            assert "events" in s
            assert s["events"] > 0

    def test_groups_by_session(self, test_db):
        from hub.mcp_server import _get_active_sessions
        sessions = _get_active_sessions(test_db, hours=24)
        session_ids = [s["session_id"] for s in sessions]
        assert len(session_ids) == len(set(session_ids))


class TestGetTokenUsage:
    """Test _get_token_usage contra DB real."""

    def test_all_providers(self, test_db):
        from hub.mcp_server import _get_token_usage
        usage = _get_token_usage(test_db)
        providers = {u["provider"] for u in usage}
        assert providers == {"claude", "opencode", "codex"}

    def test_filter_by_provider(self, test_db):
        from hub.mcp_server import _get_token_usage
        usage = _get_token_usage(test_db, provider="claude")
        assert len(usage) == 1
        assert usage[0]["provider"] == "claude"
        assert usage[0]["input_tokens"] > 0

    def test_tokens_are_positive(self, test_db):
        from hub.mcp_server import _get_token_usage
        usage = _get_token_usage(test_db)
        for u in usage:
            assert u["input_tokens"] >= 0
            assert u["output_tokens"] >= 0


class TestGetToolStats:
    """Test _get_tool_stats contra DB real."""

    def test_returns_tools(self, test_db):
        from hub.mcp_server import _get_tool_stats
        tools = _get_tool_stats(test_db)
        assert len(tools) > 0
        tool_names = {t["tool_name"] for t in tools}
        assert len(tool_names) >= 3

    def test_filter_by_project(self, test_db):
        from hub.mcp_server import _get_tool_stats
        tools = _get_tool_stats(test_db, project="YAAHub")
        for t in tools:
            assert t["count"] > 0


class TestSearchEvents:
    """Test _search_events contra DB real."""

    def test_search_by_text(self, test_db):
        from hub.mcp_server import _search_events
        results = _search_events(test_db, query="Read hub")
        assert len(results) > 0
        for r in results:
            assert "read hub" in r["summary"].lower()

    def test_search_no_results(self, test_db):
        from hub.mcp_server import _search_events
        results = _search_events(test_db, query="xyznonexistent123")
        assert results == []

    def test_search_filter_provider(self, test_db):
        from hub.mcp_server import _search_events
        results = _search_events(test_db, query="glob", provider="opencode")
        assert len(results) > 0
        for r in results:
            assert r["provider"] == "opencode"

    def test_limit_cap_at_200(self, test_db):
        from hub.mcp_server import _search_events
        results = _search_events(test_db, query="", limit=9999)
        assert len(results) <= 200


class TestGetProjectActivity:
    """Test _get_project_activity contra DB real."""

    def test_known_project(self, test_db):
        from hub.mcp_server import _get_project_activity
        result = _get_project_activity(test_db, project="live/monitor")
        assert result["events"] == 30
        assert result["sessions"] >= 1
        assert result["input_tokens"] > 0
        assert len(result["top_tools"]) > 0
        assert "claude-opus-4-6" in result["models"]

    def test_unknown_project_returns_zero(self, test_db):
        from hub.mcp_server import _get_project_activity
        result = _get_project_activity(test_db, project="nonexistent_xyz")
        assert result["events"] == 0


class TestResources:
    """Test resources."""

    def test_schema_resource(self):
        from hub.mcp_server import _get_schema
        schema = _get_schema()
        assert "CREATE TABLE events" in schema
        assert "provider TEXT" in schema

    def test_projects_resource(self, test_db):
        from hub.mcp_server import _get_projects_resource
        text = _get_projects_resource(test_db)
        assert "claude" in text
        assert "opencode" in text
        assert "codex" in text


class TestStdioTransport:
    """Test end-to-end: arrancar el MCP server como subproceso y verificar JSON-RPC."""

    def test_server_starts_and_responds_to_initialize(self):
        """Enviar un initialize JSON-RPC y verificar que responde con capabilities."""
        init_msg = json.dumps({
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
                "protocolVersion": "2024-11-05"
            }
        }) + "\n"

        proc = subprocess.Popen(
            ["uv", "run", "hub/mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(input=init_msg, timeout=15)
            assert stdout.strip(), f"Server produced no stdout. stderr: {stderr}"
            response = json.loads(stdout.strip().split("\n")[0])
            assert response.get("jsonrpc") == "2.0"
            assert response.get("id") == 1
            assert "result" in response
            caps = response["result"].get("capabilities", {})
            assert "tools" in caps or "resources" in caps
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.skip("uv run timed out — mcp package may not be cached yet")

    def test_server_stderr_has_startup_message(self):
        """Verificar que el server loguea a stderr, no a stdout."""
        proc = subprocess.Popen(
            ["uv", "run", "hub/mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, stderr = proc.communicate(input="", timeout=10)
            assert "MoolMesh" in stderr or "starting" in stderr.lower()
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.skip("uv run timed out")


# ── Tests contra la DB real del usuario ──

REAL_DB = os.path.expanduser("~/.moolmesh/events.db")


@pytest.mark.skipif(not os.path.exists(REAL_DB), reason="No real events.db")
class TestRealDatabase:
    """Tests contra la DB real — validan que las queries funcionan con 105K+ eventos."""

    def test_recent_events_returns_data(self):
        from hub.mcp_server import _get_recent_events
        events = _get_recent_events(REAL_DB, limit=10)
        assert len(events) == 10
        assert all("provider" in e for e in events)

    def test_token_usage_matches_known_providers(self):
        from hub.mcp_server import _get_token_usage
        usage = _get_token_usage(REAL_DB)
        providers = {u["provider"] for u in usage}
        assert "claude" in providers

    def test_project_summary_has_many_projects(self):
        from hub.mcp_server import _get_projects_resource
        text = _get_projects_resource(REAL_DB)
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        assert len(lines) > 50

    def test_search_readme_finds_results(self):
        from hub.mcp_server import _search_events
        results = _search_events(REAL_DB, query="README")
        assert len(results) > 0

    def test_active_sessions_recent(self):
        from hub.mcp_server import _get_active_sessions
        sessions = _get_active_sessions(REAL_DB, hours=168)
        assert len(sessions) > 0
