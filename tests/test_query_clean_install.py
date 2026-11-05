"""`mool query` / `mool sessions` on a clean install and without the SDK (#153).

Each case runs the real CLI in a subprocess with a temporary HOME. The `mcp`
SDK is made unavailable deterministically with a stub package on PYTHONPATH
whose import fails the way a missing install does, whatever the venv carries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

QUERIES = [
    ["query", "events", "-n", "10"],
    ["query", "sessions", "--hours", "2"],
    ["query", "tokens", "--since", "2026-06-01"],
    ["query", "tools", "--project", "widget-services", "-n", "5"],
    ["query", "search", "daemon", "--provider", "claude"],
    ["query", "project", "widget-services"],
    ["query", "chain", "s-1"],
]


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def no_sdk(tmp_path):
    """A PYTHONPATH entry whose ``mcp`` import fails like a missing package."""
    stub = tmp_path / "nosdk" / "mcp"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'mcp'\", name='mcp')\n")
    return stub.parent


def _run(home, args, pythonpath=None, module="hub"):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MOOLMESH_")}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(pythonpath) if pythonpath else None, str(ROOT)) if p)
    env.pop("NO_COLOR", None)
    return subprocess.run([sys.executable, "-m", module, *args], cwd=str(ROOT),
                          env=env, capture_output=True, text=True, timeout=60)


def _seed_events_db(home):
    from hub.cache.event_store import EventStore
    db = home / ".moolmesh" / "events.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    EventStore(db_path=db).close()
    return db


class TestNoDatabaseYet:
    @pytest.mark.parametrize("args", QUERIES, ids=lambda a: a[1])
    def test_query_answers_json_error_and_creates_nothing(self, home, no_sdk, args):
        res = _run(home, args, pythonpath=no_sdk)
        assert res.returncode == 1
        assert "Traceback" not in res.stderr
        assert res.stderr == ""
        doc = json.loads(res.stdout)
        assert doc["error"] == "no_data"
        assert doc["hint"] == "mool daemon start"
        assert not (home / ".moolmesh" / "events.db").exists()

    def test_sessions_json(self, home, no_sdk):
        res = _run(home, ["sessions", "--json"], pythonpath=no_sdk)
        assert res.returncode == 1 and "Traceback" not in res.stderr
        assert json.loads(res.stdout)["error"] == "no_data"
        assert not (home / ".moolmesh" / "events.db").exists()

    def test_sessions_human(self, home, no_sdk):
        res = _run(home, ["sessions"], pythonpath=no_sdk)
        assert res.returncode == 1 and "Traceback" not in res.stderr
        assert res.stdout == ""
        assert "mool daemon start" in res.stderr
        assert not (home / ".moolmesh" / "events.db").exists()

    def test_unreadable_database_is_a_json_error_too(self, home, no_sdk):
        db = home / ".moolmesh" / "events.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a sqlite database at all" * 100)
        res = _run(home, ["query", "events"], pythonpath=no_sdk)
        assert res.returncode == 1 and "Traceback" not in res.stderr
        assert json.loads(res.stdout)["error"] == "db_unreadable"


class TestWithoutTheSdk:
    @pytest.mark.parametrize("args", QUERIES, ids=lambda a: a[1])
    def test_query_prints_json_and_nothing_on_stderr(self, home, no_sdk, args):
        _seed_events_db(home)
        res = _run(home, args, pythonpath=no_sdk)
        assert res.returncode == 0, res.stderr
        assert res.stderr == ""
        json.loads(res.stdout)

    def test_sessions_prints_nothing_on_stderr(self, home, no_sdk):
        _seed_events_db(home)
        res = _run(home, ["sessions", "--json"], pythonpath=no_sdk)
        assert res.returncode == 0, res.stderr
        assert res.stderr == ""
        assert json.loads(res.stdout) == []

    def test_mcp_server_still_says_the_sdk_is_missing(self, home, no_sdk):
        res = _run(home, [], pythonpath=no_sdk, module="hub.mcp_server")
        assert res.returncode == 1
        assert "mcp package not installed" in res.stderr
        assert "mool mcp setup --install-deps" in res.stderr
