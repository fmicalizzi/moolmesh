"""Migrations applied by any process leave a line in hub.log (#136).

Only the daemon configures logging; the CLI and MCP servers must still leave
the migration trace, next to the migrated DB, without printing anything.
Synthetic DBs in tmp dirs only.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys

import pytest

import hub.log as hlog
from hub.cache.event_store import EventStore


@pytest.fixture
def unconfigured(monkeypatch):
    root = logging.getLogger("hub")
    monkeypatch.setattr(hlog, "_CONFIGURED", False)
    saved = root.handlers[:]
    for h in saved:
        root.removeHandler(h)
    yield
    for h in saved:
        root.addHandler(h)


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_event_store_logs_each_applied_migration(tmp_path, unconfigured, capsys):
    EventStore(db_path=tmp_path / "events.db").close()
    lines = _lines(tmp_path / "hub.log")
    applied = [l for l in lines if "Migration " in l and " applied: " in l]
    assert any("Migration 9 applied: parent_id" in l for l in applied)
    assert any("Migration 10 applied: dir_extra" in l for l in applied)
    assert all(f"(pid {os.getpid()})" in l for l in applied)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_reopen_logs_nothing_and_handler_is_removed(tmp_path, unconfigured):
    level = logging.getLogger("hub").level
    EventStore(db_path=tmp_path / "events.db").close()
    before = _lines(tmp_path / "hub.log")
    EventStore(db_path=tmp_path / "events.db").close()
    assert _lines(tmp_path / "hub.log") == before
    assert logging.getLogger("hub").handlers == []
    assert logging.getLogger("hub").level == level


def test_workspace_and_git_stores_log_too(tmp_path, unconfigured):
    from hub.cache.git_store import GitStore
    from hub.cache.workspace_store import WorkspaceStore
    WorkspaceStore(tmp_path / "workspace.db").close()
    GitStore(db_path=tmp_path / "github.db").close()
    text = "\n".join(_lines(tmp_path / "hub.log"))
    assert "[hub.WorkspaceStore] INFO — Migration 7 applied: live_catchup_day" in text
    assert "Migration 1 aplicada" in text


def test_configured_process_adds_no_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(hlog, "_CONFIGURED", True)
    conn = sqlite3.connect(tmp_path / "x.db")
    before = logging.getLogger("hub").handlers[:]
    with hlog.migration_logging(conn):
        assert logging.getLogger("hub").handlers == before
    conn.close()
    assert not (tmp_path / "hub.log").exists()


def test_in_memory_db_is_a_noop(unconfigured):
    conn = sqlite3.connect(":memory:")
    with hlog.migration_logging(conn):
        assert logging.getLogger("hub").handlers == []
    conn.close()


def test_cli_process_writes_hub_log_and_prints_nothing(tmp_path):
    """A fresh interpreter (like the CLI or an MCP server) opening events.db."""
    db = tmp_path / "events.db"
    code = ("from pathlib import Path; "
            "from hub.cache.event_store import EventStore; "
            f"EventStore(db_path=Path({str(db)!r})).close()")
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
    assert any("Migration 10 applied: dir_extra" in l
               for l in _lines(tmp_path / "hub.log"))
