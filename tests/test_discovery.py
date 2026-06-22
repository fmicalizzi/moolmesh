"""Tests for project discovery."""

import json
import sqlite3
from pathlib import Path

from hub.discovery import ProjectDiscovery
from hub.models.base import Provider


class TestExtractProjectName:
    """Test extract_project_name() with known paths."""

    def test_simple_project(self):
        result = ProjectDiscovery.extract_project_name("/Users/franco/Downloads/Claude/acuernavaca")
        assert result == "acuernavaca"

    def test_nested_project(self):
        result = ProjectDiscovery.extract_project_name("/Users/franco/Downloads/Claude/ddtyi/YAAHub")
        assert result == "ddtyi/YAAHub"

    def test_deep_path(self):
        result = ProjectDiscovery.extract_project_name("/Users/franco/Downloads/Claude/eventsmx/front/stack")
        assert result == "eventsmx/front/stack"

    def test_root_path(self):
        result = ProjectDiscovery.extract_project_name("/")
        assert result == "/"

    def test_empty_path(self):
        result = ProjectDiscovery.extract_project_name("")
        assert result == "unknown"

    def test_strips_users_prefix(self):
        result = ProjectDiscovery.extract_project_name("/Users/testuser/Downloads/Claude/myproject")
        assert result == "myproject"

    def test_strips_claude_prefix(self):
        result = ProjectDiscovery.extract_project_name("/Users/testuser/Claude/tools/live-monitor")
        assert result == "tools/live-monitor"

    def test_very_deep_path(self):
        result = ProjectDiscovery.extract_project_name("/Users/testuser/Downloads/Claude/a/b/c/d/e")
        assert result == "c/d/e"  # keeps last 3

    def test_volumes_path(self):
        result = ProjectDiscovery.extract_project_name("/Volumes/SSD/Projects/myapp")
        # "Projects" is in strip_dirs, so only "myapp" remains
        assert result == "myapp"

    def test_strips_temporal_prefix(self):
        result = ProjectDiscovery.extract_project_name(
            "/Users/franco/Downloads/Claude/Temporal/LACNIC"
        )
        assert result == "LACNIC"

    def test_strips_temporal_nested(self):
        result = ProjectDiscovery.extract_project_name(
            "/Users/franco/Downloads/Claude/Temporal/retail/qr/tracker"
        )
        assert result == "retail/qr/tracker"


class TestDecodeProjectPath:
    """Test the lossy decode_project_path()."""

    def test_basic_decode(self):
        result = ProjectDiscovery.decode_project_path("-Users-foo-bar")
        assert result == "/Users/foo/bar"

    def test_empty_string(self):
        result = ProjectDiscovery.decode_project_path("")
        assert result == ""

    def test_single_component(self):
        result = ProjectDiscovery.decode_project_path("-foo")
        assert result == "/foo"


class TestEncodeProjectPath:
    def test_basic_encode(self):
        result = ProjectDiscovery.encode_project_path("/Users/foo/bar")
        assert result == "-Users-foo-bar"

    def test_roundtrip_simple(self):
        original = "/Users/foo/bar"
        encoded = ProjectDiscovery.encode_project_path(original)
        decoded = ProjectDiscovery.decode_project_path(encoded)
        # Note: this is lossy but works for simple paths without hyphens
        assert decoded == original


class TestShortCwd:
    def test_shorten_path(self):
        result = ProjectDiscovery.short_cwd("/Users/franco/Downloads/Claude/tools/live-monitor")
        # short_cwd strips at "Downloads", so remaining is "Claude/tools/live-monitor"
        # depth=3 keeps last 3: "Claude/tools/live-monitor"
        assert result == "Claude/tools/live-monitor"

    def test_empty(self):
        assert ProjectDiscovery.short_cwd("") == ""

    def test_short_path(self):
        result = ProjectDiscovery.short_cwd("/tmp/foo")
        # No known prefix found, returns full path
        assert result == "/tmp/foo"


class TestDiscoverClaude:
    """Test Claude discovery with mock directories."""

    def test_no_base_dir(self, tmp_path):
        discovery = ProjectDiscovery(claude_base=tmp_path / "nonexistent")
        result = discovery.discover_claude()
        assert result == []

    def test_discovers_single_project(self, tmp_path):
        # Create encoded project dir
        encoded = "-Users-test-project-alpha"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()
        # Add a JSONL file
        (proj_dir / "session-001.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(claude_base=tmp_path)
        result = discovery.discover_claude()
        assert len(result) == 1
        proj = result[0]
        assert proj.provider == Provider.CLAUDE
        assert len(proj.session_files) == 1
        assert proj.encoded_name == encoded

    def test_skips_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "test.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(claude_base=tmp_path)
        result = discovery.discover_claude()
        assert result == []

    def test_discovers_session_subdirs(self, tmp_path):
        encoded = "-Users-test-proj"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()
        # Session directory with JSONL
        session_dir = proj_dir / "sess-001"
        session_dir.mkdir()
        (session_dir / "rollout.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(claude_base=tmp_path)
        result = discovery.discover_claude()
        assert len(result) == 1
        assert len(result[0].session_files) == 1


class TestDiscoverCodex:
    """Test Codex discovery with mock directories and SQLite."""

    def test_no_base_dir(self, tmp_path):
        discovery = ProjectDiscovery(codex_base=tmp_path / "nonexistent")
        result = discovery.discover_codex()
        assert result == []

    def test_no_sqlite_fallback(self, tmp_path):
        """Without SQLite, rollouts go into codex-sessions bucket."""
        sessions_dir = tmp_path / "sessions" / "2026" / "04" / "01"
        sessions_dir.mkdir(parents=True)
        rollout = sessions_dir / "rollout-abc.jsonl"
        rollout.write_text("{}\n")

        discovery = ProjectDiscovery(codex_base=tmp_path)
        result = discovery.discover_codex()
        assert len(result) == 1
        assert result[0].name == "codex-sessions"

    def test_sqlite_groups_by_project(self, tmp_path):
        """SQLite data groups rollouts by project name."""
        sessions_dir = tmp_path / "sessions" / "2026" / "04" / "01"
        sessions_dir.mkdir(parents=True)

        # Create rollout files
        r1 = sessions_dir / "rollout-aaa.jsonl"
        r1.write_text("{}\n")
        r2 = sessions_dir / "rollout-bbb.jsonl"
        r2.write_text("{}\n")

        # Create SQLite with CWD mapping
        db = tmp_path / "state_5.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE threads (
            rollout_path TEXT, cwd TEXT, tokens_used INTEGER, source TEXT
        )""")
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            (str(r1), "/Users/test/MyApp", 5000, "cli")
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            (str(r2), "/Users/test/MyApp", 3000, "cli")
        )
        conn.commit()
        conn.close()

        discovery = ProjectDiscovery(codex_base=tmp_path)
        result = discovery.discover_codex()
        assert len(result) == 1
        # Both rollouts should be grouped under "MyApp"
        assert result[0].name == "MyApp"
        assert len(result[0].session_files) == 2
        assert result[0].provider == Provider.CODEX

    def test_sqlite_exception_logged(self, tmp_path):
        """Corrupt SQLite should log warning, not crash."""
        sessions_dir = tmp_path / "sessions" / "2026"
        sessions_dir.mkdir(parents=True)

        # Create a corrupt SQLite file
        db = tmp_path / "state_5.sqlite"
        db.write_text("not a sqlite database")

        # Create a rollout file
        r1 = sessions_dir / "rollout-xyz.jsonl"
        r1.write_text("{}\n")

        discovery = ProjectDiscovery(codex_base=tmp_path)
        result = discovery.discover_codex()
        # Should still return the rollout (fallback to codex-sessions)
        assert len(result) == 1

    def test_sqlite_filters_noise_sessions(self, tmp_path):
        """Exec sessions with 0 tokens and cwd='/' should be filtered out."""
        sessions_dir = tmp_path / "sessions" / "2026" / "04" / "01"
        sessions_dir.mkdir(parents=True)

        # Rollout from a real project (should be included)
        r_real = sessions_dir / "rollout-real.jsonl"
        r_real.write_text("{}\n")

        # Rollout from noise session (exec, 0 tokens, cwd="/")
        r_noise = sessions_dir / "rollout-noise.jsonl"
        r_noise.write_text("{}\n")

        # Create SQLite with both threads
        db = tmp_path / "state_5.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE threads (
            rollout_path TEXT, cwd TEXT, tokens_used INTEGER, source TEXT
        )""")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                     (str(r_real), "/Users/test/MyApp", 5000, "vscode"))
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                     (str(r_noise), "/", 0, "exec"))
        conn.commit()
        conn.close()

        discovery = ProjectDiscovery(codex_base=tmp_path)
        result = discovery.discover_codex()

        # Solo el rollout real debe estar, el de ruido se filtra
        assert len(result) == 1
        assert result[0].name == "MyApp"
        assert len(result[0].session_files) == 1
        assert result[0].session_files[0].name == "rollout-real.jsonl"


class TestDiscoverQwen:
    """Test Qwen discovery with mock directories."""

    def test_no_base_dir(self, tmp_path):
        discovery = ProjectDiscovery(qwen_base=tmp_path / "nonexistent")
        result = discovery.discover_qwen()
        assert result == []

    def test_discovers_project(self, tmp_path):
        encoded = "-Users-test-qwen-proj"
        proj_dir = tmp_path / encoded
        chats_dir = proj_dir / "chats"
        chats_dir.mkdir(parents=True)
        (chats_dir / "chat-001.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(qwen_base=tmp_path)
        result = discovery.discover_qwen()
        assert len(result) == 1
        assert result[0].provider == Provider.QWEN
        assert len(result[0].session_files) == 1

    def test_skips_empty_project(self, tmp_path):
        encoded = "-Users-test-empty"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir()
        # No chats directory

        discovery = ProjectDiscovery(qwen_base=tmp_path)
        result = discovery.discover_qwen()
        assert result == []


class TestDiscoverAll:
    def test_returns_all_providers(self, tmp_path):
        # Claude
        claude_dir = tmp_path / "claude" / "-Users-test-cl"
        claude_dir.mkdir(parents=True)
        (claude_dir / "s1.jsonl").write_text("{}\n")

        # Qwen
        qwen_dir = tmp_path / "qwen" / "-Users-test-qw" / "chats"
        qwen_dir.mkdir(parents=True)
        (qwen_dir / "c1.jsonl").write_text("{}\n")

        discovery = ProjectDiscovery(
            claude_base=tmp_path / "claude",
            qwen_base=tmp_path / "qwen",
            codex_base=tmp_path / "codex",
            opencode_base=tmp_path / "nonexistent.db",
        )
        result = discovery.discover_all()
        assert len(result) == 2
        providers = {p.provider for p in result}
        assert Provider.CLAUDE in providers
        assert Provider.QWEN in providers
