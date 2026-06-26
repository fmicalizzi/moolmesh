"""Tests for session metadata — Phase 1, 3, and 6."""

import json
import time
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore
from hub.models.base import Provider, SessionMeta
from hub.adapters.claude_adapter import ClaudeAdapter
from hub.adapters.codex_adapter import CodexAdapter
from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.adapters.qwen_adapter import QwenAdapter
from hub.models.claude import ClaudeEntry
from hub.models.codex import CodexEntry
from hub.models.opencode import OpenCodeEntry
from hub.models.qwen import QwenEntry


@pytest.fixture
def store(tmp_path) -> EventStore:
    db = tmp_path / "events.db"
    s = EventStore(db)
    yield s
    s.close()


class TestSessionMeta:
    def test_session_meta_to_dict(self):
        meta = SessionMeta(
            id="sess-1", provider=Provider.CLAUDE, project="myproj",
            git_branch="main", model="opus-4", cli_version="1.0.0",
        )
        d = meta.to_dict()
        assert d["id"] == "sess-1"
        assert d["provider"] == "claude"
        assert d["git_branch"] == "main"
        assert d["model"] == "opus-4"
        assert "cost" not in d  # cost=0 should be omitted

    def test_session_meta_empty_fields_omitted(self):
        meta = SessionMeta(id="s", provider=Provider.QWEN, project="p")
        d = meta.to_dict()
        assert "git_branch" not in d
        assert "title" not in d
        assert "model" not in d


class TestAdapterSessionMeta:
    def test_claude_adapter_to_session_meta(self):
        adapter = ClaudeAdapter()
        entry = ClaudeEntry(
            type="user", session_id="cs-1", cwd="/code",
            git_branch="feat/x", model="opus-4", version="1.2.0",
            is_sidechain=True,
        )
        meta = adapter.to_session_meta(entry, "myproject")
        assert meta is not None
        assert meta.id == "cs-1"
        assert meta.provider == Provider.CLAUDE
        assert meta.git_branch == "feat/x"
        assert meta.cli_version == "1.2.0"
        assert meta.is_sidechain is True

    def test_claude_adapter_skips_file_history(self):
        adapter = ClaudeAdapter()
        entry = ClaudeEntry(type="file-history-snapshot", session_id="cs-1")
        assert adapter.to_session_meta(entry, "p") is None

    def test_codex_adapter_to_session_meta(self):
        adapter = CodexAdapter()
        entry = CodexEntry(
            event_type="session_meta", session_id="cx-1",
            cwd="/work", cli_version="0.5", model_provider="gpt-5",
            source="terminal",
        )
        meta = adapter.to_session_meta(entry, "codex-proj")
        assert meta is not None
        assert meta.provider == Provider.CODEX
        assert meta.cli_version == "0.5"
        assert meta.source == "terminal"

    def test_opencode_adapter_to_session_meta(self):
        adapter = OpenCodeAdapter()
        entry = OpenCodeEntry(
            session_id="oc-1", model_id="deepseek-v3",
            cwd="/app", cost=0.42, session_title="Fix login bug",
        )
        meta = adapter.to_session_meta(entry, "oc-proj")
        assert meta is not None
        assert meta.provider == Provider.OPENCODE
        assert meta.title == "Fix login bug"
        assert meta.cost == 0.42

    def test_qwen_adapter_to_session_meta(self):
        adapter = QwenAdapter()
        entry = QwenEntry(
            type="user", session_id="qw-1", model="qwen3-coder",
            cwd="/project",
        )
        meta = adapter.to_session_meta(entry, "qwen-proj")
        assert meta is not None
        assert meta.provider == Provider.QWEN
        assert meta.model == "qwen3-coder"


class TestEventStoreSessionsTable:
    def test_sessions_table_created(self, store):
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchall()
        assert len(rows) == 1

    def test_upsert_session_insert(self, store):
        meta = {
            "id": "s1", "provider": "claude", "project": "proj",
            "git_branch": "main", "model": "opus-4",
        }
        store.upsert_session(meta, "2026-06-25T10:00:00")
        sessions = store.get_sessions(hours=48)
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"
        assert sessions[0]["git_branch"] == "main"

    def test_upsert_session_update_preserves_fields(self, store):
        meta1 = {
            "id": "s1", "provider": "claude", "project": "proj",
            "git_branch": "feat/x", "model": "opus-4",
        }
        store.upsert_session(meta1, "2026-06-25T10:00:00")

        meta2 = {
            "id": "s1", "provider": "claude", "project": "proj",
            "git_branch": "", "model": "sonnet-4",
        }
        store.upsert_session(meta2, "2026-06-25T10:05:00")

        sessions = store.get_sessions(hours=48)
        assert len(sessions) == 1
        assert sessions[0]["git_branch"] == "feat/x"  # preserved from first upsert
        assert sessions[0]["model"] == "sonnet-4"  # updated

    def test_upsert_session_empty_id_ignored(self, store):
        store.upsert_session({"id": "", "provider": "claude", "project": "p"}, "2026-01-01T00:00:00")
        assert store.get_sessions(hours=99999) == []

    def test_get_session_detail(self, store):
        meta = {
            "id": "detail-1", "provider": "claude", "project": "proj",
            "git_branch": "main", "model": "opus-4", "title": "Testing",
        }
        store.upsert_session(meta, "2026-06-25T10:00:00")
        detail = store.get_session_detail("detail-1")
        assert detail is not None
        assert detail["title"] == "Testing"
        assert detail["git_branch"] == "main"

    def test_get_session_detail_not_found(self, store):
        assert store.get_session_detail("nonexistent") is None

    def test_backfill_from_events(self, tmp_path):
        db = tmp_path / "backfill.db"
        s1 = EventStore(db)
        s1.store_batch([
            {"provider": "claude", "project": "p1", "event_type": "user",
             "timestamp": "2026-06-25T10:00:00", "summary": "hello",
             "session_id": "bf-1"},
            {"provider": "claude", "project": "p1", "event_type": "assistant",
             "timestamp": "2026-06-25T10:00:05", "summary": "hi",
             "session_id": "bf-1"},
        ])
        s1.close()

        # Drop the sessions table to force re-backfill
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE IF EXISTS sessions")
        conn.commit()
        conn.close()

        s2 = EventStore(db)
        sessions = s2.get_sessions(hours=99999)
        assert len(sessions) >= 1
        bf = [s for s in sessions if s["id"] == "bf-1"]
        assert len(bf) == 1
        assert bf[0]["event_count"] == 2
        s2.close()


class TestSessionsFilter:
    def test_filter_by_branch(self, store):
        store.upsert_session(
            {"id": "s1", "provider": "claude", "project": "p", "git_branch": "main"},
            "2026-06-25T10:00:00",
        )
        store.upsert_session(
            {"id": "s2", "provider": "claude", "project": "p", "git_branch": "feat/x"},
            "2026-06-25T10:00:00",
        )
        main_sessions = store.get_sessions(hours=48, branch="main")
        assert len(main_sessions) == 1
        assert main_sessions[0]["id"] == "s1"

        feat_sessions = store.get_sessions(hours=48, branch="feat/x")
        assert len(feat_sessions) == 1
        assert feat_sessions[0]["id"] == "s2"

    def test_filter_by_provider(self, store):
        store.upsert_session(
            {"id": "s1", "provider": "claude", "project": "p"},
            "2026-06-25T10:00:00",
        )
        store.upsert_session(
            {"id": "s2", "provider": "codex", "project": "p"},
            "2026-06-25T10:00:00",
        )
        claude_only = store.get_sessions(hours=48, provider="claude")
        assert len(claude_only) == 1
        assert claude_only[0]["provider"] == "claude"


class TestMcpSessionFunctions:
    def test_get_sessions_pure_function(self, tmp_path):
        from hub.mcp_server import _get_sessions, _get_session_detail, _get_branch_sessions

        db = tmp_path / "mcp.db"
        s = EventStore(db)
        s.upsert_session(
            {"id": "mcp-1", "provider": "claude", "project": "p",
             "git_branch": "main", "model": "opus-4"},
            "2026-06-25T10:00:00",
        )
        s.close()

        sessions = _get_sessions(str(db), hours=48)
        assert len(sessions) >= 1

        detail = _get_session_detail(str(db), "mcp-1")
        assert detail is not None
        assert detail["model"] == "opus-4"

        branch = _get_branch_sessions(str(db), "main", hours=48)
        assert len(branch) >= 1
        assert branch[0]["git_branch"] == "main"
