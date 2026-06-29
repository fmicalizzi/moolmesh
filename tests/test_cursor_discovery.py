"""Tests for Cursor discovery."""

import json
import sqlite3
from pathlib import Path

from hub.discovery import ProjectDiscovery
from hub.models.base import Provider


def _make_cursor(base: Path) -> None:
    base.mkdir(parents=True)
    (base / "globalStorage").mkdir()
    (base / "globalStorage" / "state.vscdb").write_bytes(b"SQLite")
    ws = base / "workspaceStorage" / "ws1"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///home/u/dev/myproj"}))
    conn = sqlite3.connect(str(ws / "state.vscdb"))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)",
                 ("composer.composerData",
                  json.dumps({"allComposers": [{"composerId": "c1", "name": "Chat"}]})))
    conn.commit()
    conn.close()


def test_discover_cursor_finds_project(tmp_path):
    base = tmp_path / "User"
    _make_cursor(base)
    disc = ProjectDiscovery(cursor_base=base)
    projects = disc.discover_cursor()
    assert len(projects) == 1
    p = projects[0]
    assert p.provider == Provider.CURSOR
    assert p.name == "myproj"
    assert p.path == "/home/u/dev/myproj"


def test_discover_cursor_absent_is_empty(tmp_path):
    disc = ProjectDiscovery(cursor_base=tmp_path / "nope")
    assert disc.discover_cursor() == []


def test_discover_all_includes_cursor(tmp_path):
    base = tmp_path / "User"
    _make_cursor(base)
    disc = ProjectDiscovery(
        claude_base=tmp_path / "c", codex_base=tmp_path / "x",
        qwen_base=tmp_path / "q", opencode_base=tmp_path / "o.db",
        cursor_base=base,
    )
    providers = {p.provider for p in disc.discover_all()}
    assert Provider.CURSOR in providers
