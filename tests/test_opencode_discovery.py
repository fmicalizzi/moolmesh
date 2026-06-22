"""Tests for OpenCode discovery."""

import json
import sqlite3
from pathlib import Path

from hub.discovery import ProjectDiscovery
from hub.models.base import Provider


def _create_opencode_db(db_path: Path, directories: list[tuple[str, str]] | None = None) -> None:
    """Create an OpenCode SQLite DB optionally populated with sessions."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE project (
            id TEXT PRIMARY KEY, worktree TEXT, name TEXT,
            time_created DATETIME, time_updated DATETIME
        );
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, title TEXT, version TEXT, model TEXT, agent TEXT,
            cost REAL, tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER,
            tokens_cache_write INTEGER, time_created DATETIME, time_updated DATETIME
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT,
            time_created DATETIME, time_updated DATETIME, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created DATETIME, time_updated DATETIME, data TEXT
        );
    """)

    if directories:
        for i, (directory, project_name) in enumerate(directories):
            pid = f"proj-{i}"
            sid = f"sess-{i}"
            conn.execute(
                "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                (pid, directory, project_name, "2026-06-01T10:00:00", "2026-06-01T12:00:00"),
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, pid, None, "slug", directory, "Title", "1.0",
                 '{"id":"model"}', "default", 0.0, 0, 0, 0, 0, 0,
                 "2026-06-01T10:00:00", "2026-06-01T12:00:00"),
            )

    conn.commit()
    conn.close()


class TestDiscoverOpencode:
    def test_no_db(self, tmp_path):
        discovery = ProjectDiscovery(opencode_base=tmp_path / "nonexistent.db")
        result = discovery.discover_opencode()
        assert result == []

    def test_empty_db(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db)
        discovery = ProjectDiscovery(opencode_base=db)
        result = discovery.discover_opencode()
        assert result == []

    def test_with_sessions(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, directories=[
            ("/Users/test/myapp", "myapp"),
            ("/Users/test/webapp", "webapp"),
        ])
        discovery = ProjectDiscovery(opencode_base=db)
        result = discovery.discover_opencode()
        assert len(result) == 2
        names = {p.name for p in result}
        assert "myapp" in names
        assert "webapp" in names
        for p in result:
            assert p.provider == Provider.OPENCODE
            assert len(p.session_files) == 1
            assert p.session_files[0] == db

    def test_project_uses_db_name(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, directories=[
            ("/Users/test/myapp", "myapp"),
        ])
        discovery = ProjectDiscovery(opencode_base=db)
        result = discovery.discover_opencode()
        assert result[0].name == "myapp"
        assert result[0].path == "/Users/test/myapp"

    def test_fallback_to_extract_name(self, tmp_path):
        """When project name is NULL, extract from directory."""
        db = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT, time_created DATETIME, time_updated DATETIME);
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, title TEXT, version TEXT, model TEXT, agent TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER, time_created DATETIME, time_updated DATETIME);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created DATETIME, time_updated DATETIME, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created DATETIME, time_updated DATETIME, data TEXT);
        """)
        conn.execute(
            "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
            ("p1", "/Users/test/myapp", None, "2026-06-01", "2026-06-01"),
        )
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "p1", None, "s", "/Users/test/myapp", "T", "1",
             '{}', "default", 0, 0, 0, 0, 0, 0, "2026-06-01", "2026-06-01"),
        )
        conn.commit()
        conn.close()
        discovery = ProjectDiscovery(opencode_base=db)
        result = discovery.discover_opencode()
        assert len(result) == 1
        assert result[0].name == "myapp"


class TestDiscoverAllIncludesOpencode:
    def test_includes_opencode(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, directories=[
            ("/Users/test/myapp", "myapp"),
        ])
        # Claude dir with a project
        claude_dir = tmp_path / "claude" / "-Users-test-cl"
        claude_dir.mkdir(parents=True)
        (claude_dir / "s1.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(
            claude_base=tmp_path / "claude",
            codex_base=tmp_path / "codex",
            qwen_base=tmp_path / "qwen",
            opencode_base=db,
        )
        result = discovery.discover_all()
        providers = {p.provider for p in result}
        assert Provider.OPENCODE in providers
        assert Provider.CLAUDE in providers
        assert len(result) == 2
