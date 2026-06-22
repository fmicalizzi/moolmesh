"""Tests for OpenCode SQLite parser."""

import json
import sqlite3
from pathlib import Path

import pytest

from hub.parsers.opencode_parser import OpenCodeParser


def _create_opencode_db(db_path: Path, with_data: bool = False) -> None:
    """Create an OpenCode-style SQLite database."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            worktree TEXT,
            name TEXT,
            time_created DATETIME,
            time_updated DATETIME
        );
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            slug TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            model TEXT,
            agent TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            time_created DATETIME,
            time_updated DATETIME
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created DATETIME,
            time_updated DATETIME,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created DATETIME,
            time_updated DATETIME,
            data TEXT
        );
    """)

    if with_data:
        conn.execute(
            "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
            ("proj-1", "/Users/test/myapp", "myapp", "2026-06-01T10:00:00", "2026-06-01T12:00:00"),
        )
        model_json = json.dumps({"id": "mimo-v2.5", "providerID": "opencode-go", "variant": "high"})
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-1", "proj-1", None, "test-session", "/Users/test/myapp",
             "Test Session", "1.0", model_json, "default", 0.05,
             50000, 10000, 2000, 40000, 5000,
             "2026-06-01T10:00:00", "2026-06-01T12:00:00"),
        )
        msg_data_user = json.dumps({"role": "user", "time": "2026-06-01T10:00:01", "path": {"cwd": "/Users/test/myapp"}})
        msg_data_asst = json.dumps({"role": "assistant", "time": "2026-06-01T10:00:02", "path": {"cwd": "/Users/test/myapp"}})
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg-1", "sess-1", "2026-06-01T10:00:01", "2026-06-01T10:00:01", msg_data_user),
        )
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg-2", "sess-1", "2026-06-01T10:00:02", "2026-06-01T10:00:10", msg_data_asst),
        )
        # text part (user)
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-1", "msg-1", "sess-1", "2026-06-01T10:00:01", "2026-06-01T10:00:01",
             json.dumps({"type": "text", "content": "Fix the login bug"})),
        )
        # text part (assistant)
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-2", "msg-2", "sess-1", "2026-06-01T10:00:02", "2026-06-01T10:00:02",
             json.dumps({"type": "text", "content": "I'll fix the login bug for you."})),
        )
        # reasoning part
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-3", "msg-2", "sess-1", "2026-06-01T10:00:03", "2026-06-01T10:00:03",
             json.dumps({"type": "reasoning", "content": "Let me think about this..."})),
        )
        # tool part
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-4", "msg-2", "sess-1", "2026-06-01T10:00:04", "2026-06-01T10:00:04",
             json.dumps({"type": "tool", "tool": "read", "state": {"input": {"path": "/src/auth.py"}, "output": "file content"}})),
        )
        # patch part
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-5", "msg-2", "sess-1", "2026-06-01T10:00:05", "2026-06-01T10:00:05",
             json.dumps({"type": "patch", "files": [{"path": "/src/auth.py"}, {"path": "/src/login.py"}]})),
        )
        # step-start part (should be skipped)
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-6", "msg-2", "sess-1", "2026-06-01T10:00:06", "2026-06-01T10:00:06",
             json.dumps({"type": "step-start", "metadata": {}})),
        )
        # step-finish part (with tokens)
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-7", "msg-2", "sess-1", "2026-06-01T10:00:07", "2026-06-01T10:00:07",
             json.dumps({"type": "step-finish", "tokens": {"input": 1500, "output": 300, "reasoning": 100, "cache": {"read": 1000, "write": 200}}})),
        )
        # file part
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-8", "msg-2", "sess-1", "2026-06-01T10:00:08", "2026-06-01T10:00:08",
             json.dumps({"type": "file", "path": "/src/utils.py"})),
        )
        # compaction part
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part-9", "msg-2", "sess-1", "2026-06-01T10:00:09", "2026-06-01T10:00:09",
             json.dumps({"type": "compaction", "summary": "Session compacted."})),
        )

    conn.commit()
    conn.close()


class TestCanParse:
    def test_valid_db(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db)
        assert OpenCodeParser.can_parse(db) is True

    def test_wrong_file(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text("{}\n")
        assert OpenCodeParser.can_parse(f) is False

    def test_wrong_name(self, tmp_path):
        db = tmp_path / "other.db"
        _create_opencode_db(db)
        assert OpenCodeParser.can_parse(db) is False

    def test_nonexistent(self, tmp_path):
        assert OpenCodeParser.can_parse(tmp_path / "opencode.db") is False

    def test_missing_tables(self, tmp_path):
        db = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE session (id TEXT)")
        conn.close()
        assert OpenCodeParser.can_parse(db) is False


class TestParseFile:
    def test_empty_db(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        assert entries == []

    def test_nonexistent_file(self, tmp_path):
        parser = OpenCodeParser()
        entries = parser.parse_file(tmp_path / "missing.db")
        assert entries == []

    def test_with_data(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        # 9 parts minus step-start = 8
        assert len(entries) == 8

    def test_text_part_user(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        user_entries = [e for e in entries if e.part_type == "text" and e.role == "user"]
        assert len(user_entries) == 1
        assert user_entries[0].text == "Fix the login bug"
        assert user_entries[0].session_id == "sess-1"

    def test_tool_part(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        tool_entries = [e for e in entries if e.part_type == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0].tool_call is not None
        assert tool_entries[0].tool_call.name == "read"
        assert tool_entries[0].tool_call.input_data["path"] == "/src/auth.py"

    def test_reasoning_part(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        reasoning = [e for e in entries if e.part_type == "reasoning"]
        assert len(reasoning) == 1
        assert reasoning[0].text == "Let me think about this..."

    def test_patch_part(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        patches = [e for e in entries if e.part_type == "patch"]
        assert len(patches) == 1
        assert patches[0].files_affected == ["/src/auth.py", "/src/login.py"]
        assert patches[0].tool_call is not None
        assert patches[0].tool_call.name == "file_edit"

    def test_step_finish_tokens(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        sf = [e for e in entries if e.part_type == "step-finish"]
        assert len(sf) == 1
        assert sf[0].token_input == 1500
        assert sf[0].token_output == 300
        assert sf[0].token_reasoning == 100
        assert sf[0].token_cache_read == 1000
        assert sf[0].token_cache_write == 200

    def test_skip_step_start(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        step_starts = [e for e in entries if e.part_type == "step-start"]
        assert len(step_starts) == 0

    def test_model_extraction(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        assert entries[0].model_id == "mimo-v2.5"
        assert entries[0].model_provider == "opencode-go"

    def test_project_dir(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        for e in entries:
            assert e.project_dir == "/Users/test/myapp"
            assert e.project_name == "myapp"

    def test_file_part(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        file_entries = [e for e in entries if e.part_type == "file"]
        assert len(file_entries) == 1
        assert file_entries[0].text == "/src/utils.py"
        assert file_entries[0].tool_call is not None
        assert file_entries[0].tool_call.name == "file_read"

    def test_compaction_part(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_file(db)
        compactions = [e for e in entries if e.part_type == "compaction"]
        assert len(compactions) == 1
        assert compactions[0].text == "Session compacted."


class TestParseSession:
    def test_single_session(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_session(db, "sess-1")
        assert len(entries) == 8

    def test_nonexistent_session(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db, with_data=True)
        parser = OpenCodeParser()
        entries = parser.parse_session(db, "nonexistent")
        assert entries == []


class TestParseIncremental:
    def test_returns_empty(self, tmp_path):
        db = tmp_path / "opencode.db"
        _create_opencode_db(db)
        parser = OpenCodeParser()
        entries, offset = parser.parse_incremental(db, 0)
        assert entries == []
        assert offset == 0
