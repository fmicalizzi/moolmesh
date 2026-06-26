"""Tests for cross-session linking (Phase 1: explicit + Phase 2: temporal)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hub.cache.event_store import EventStore


@pytest.fixture
def store(tmp_path) -> EventStore:
    db = tmp_path / "events.db"
    s = EventStore(db)
    yield s
    s.close()


def _seed_session(store: EventStore, sid: str, provider: str, project: str = "proj",
                  model: str = "opus-4", title: str = "") -> None:
    store.upsert_session({
        "id": sid, "provider": provider, "project": project,
        "model": model, "title": title,
    }, "2026-06-25T10:00:00")


def _seed_event(store: EventStore, sid: str, provider: str, project: str = "proj",
                file_path: str | None = None, timestamp: str = "2026-06-25T10:00:00") -> None:
    store.store({
        "provider": provider, "project": project, "event_type": "tool_use",
        "timestamp": timestamp, "summary": "edit file", "session_id": sid,
        "file_path": file_path,
    })


class TestSessionLinksTable:
    def test_table_exists(self, store):
        tables = [r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "session_links" in tables

    def test_indexes_exist(self, store):
        indexes = [r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()]
        assert "idx_links_source" in indexes
        assert "idx_links_target" in indexes


class TestLinkSessions:
    def test_creates_link(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        created = store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        assert created is True

        row = store._conn.execute(
            "SELECT * FROM session_links WHERE source_session = 's1'"
        ).fetchone()
        assert row is not None

    def test_dedup_returns_false(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        second = store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        assert second is False

    def test_different_link_type_allowed(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        created = store.link_sessions("s1", "claude", "s2", "opencode", "references")
        assert created is True

    def test_stores_metadata(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        store.link_sessions("s1", "claude", "s2", "opencode",
                            metadata={"reason": "manual"})
        row = store._conn.execute(
            "SELECT metadata_json FROM session_links WHERE source_session = 's1'"
        ).fetchone()
        assert json.loads(row[0]) == {"reason": "manual"}

    def test_stores_confidence(self, store):
        store.link_sessions("s1", "claude", "s2", "opencode", confidence=0.75)
        row = store._conn.execute(
            "SELECT confidence FROM session_links WHERE source_session = 's1'"
        ).fetchone()
        assert row[0] == 0.75


class TestGetSessionChain:
    def test_successors(self, store):
        _seed_session(store, "s1", "claude", title="First session")
        _seed_session(store, "s2", "opencode", title="Second session")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")

        chain = store.get_session_chain("s1")
        assert len(chain) == 1
        assert chain[0]["session_id"] == "s2"
        assert chain[0]["direction"] == "successor"
        assert chain[0]["link_type"] == "continues"

    def test_predecessors(self, store):
        _seed_session(store, "s1", "claude", title="First session")
        _seed_session(store, "s2", "opencode", title="Second session")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")

        chain = store.get_session_chain("s2")
        assert len(chain) == 1
        assert chain[0]["session_id"] == "s1"
        assert chain[0]["direction"] == "predecessor"

    def test_bidirectional(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        _seed_session(store, "s3", "codex")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        store.link_sessions("s3", "codex", "s1", "claude", "references")

        chain = store.get_session_chain("s1")
        assert len(chain) == 2
        directions = {c["direction"] for c in chain}
        assert directions == {"successor", "predecessor"}

    def test_empty(self, store):
        chain = store.get_session_chain("nonexistent")
        assert chain == []

    def test_includes_session_metadata(self, store):
        _seed_session(store, "s1", "claude", title="First", model="opus-4")
        _seed_session(store, "s2", "opencode", title="Second", model="gpt-5")
        _seed_event(store, "s2", "opencode")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")

        chain = store.get_session_chain("s1")
        assert chain[0]["title"] == "Second"
        assert chain[0]["model"] == "gpt-5"
        assert chain[0]["event_count"] >= 1


class TestDetectTemporalLinks:
    def test_finds_shared_files(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        _seed_event(store, "s1", "claude", file_path="/src/main.py",
                    timestamp="2026-06-25T10:00:00")
        _seed_event(store, "s2", "opencode", file_path="/src/main.py",
                    timestamp="2026-06-25T11:00:00")

        candidates = store.detect_temporal_links("s1", hours=4.0)
        assert len(candidates) == 1
        assert candidates[0]["session_id"] == "s2"
        assert candidates[0]["shared_files"] >= 1

    def test_respects_time_window(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        _seed_event(store, "s1", "claude", file_path="/src/main.py",
                    timestamp="2026-06-25T10:00:00")
        _seed_event(store, "s2", "opencode", file_path="/src/main.py",
                    timestamp="2026-06-28T10:00:00")

        candidates = store.detect_temporal_links("s1", hours=4.0)
        assert len(candidates) == 0

    def test_confidence_scaling(self, store):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        files = ["/src/a.py", "/src/b.py", "/src/c.py"]
        for i, f in enumerate(files):
            store.store({
                "provider": "claude", "project": "proj", "event_type": "tool_use",
                "timestamp": f"2026-06-25T10:0{i}:00", "summary": f"edit {f}",
                "session_id": "s1", "file_path": f,
            })
            store.store({
                "provider": "opencode", "project": "proj", "event_type": "tool_use",
                "timestamp": f"2026-06-25T11:0{i}:00", "summary": f"edit {f}",
                "session_id": "s2", "file_path": f,
            })

        candidates = store.detect_temporal_links("s1", hours=4.0)
        assert len(candidates) == 1
        assert candidates[0]["confidence"] == min(0.5 + (3 * 0.1), 0.9)

    def test_no_self_links(self, store):
        _seed_session(store, "s1", "claude")
        _seed_event(store, "s1", "claude", file_path="/src/main.py",
                    timestamp="2026-06-25T10:00:00")
        _seed_event(store, "s1", "claude", file_path="/src/main.py",
                    timestamp="2026-06-25T10:30:00")

        candidates = store.detect_temporal_links("s1", hours=4.0)
        assert len(candidates) == 0

    def test_empty_result(self, store):
        candidates = store.detect_temporal_links("nonexistent")
        assert candidates == []


class TestMCPSessionChain:
    def test_pure_function(self, tmp_path):
        db_path = tmp_path / "events.db"
        store = EventStore(db_path)
        _seed_session(store, "s1", "claude", title="One")
        _seed_session(store, "s2", "opencode", title="Two")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        store.close()

        from hub.mcp_server import _get_session_chain
        chain = _get_session_chain(str(db_path), "s1")
        assert len(chain) == 1
        assert chain[0]["session_id"] == "s2"
        assert chain[0]["direction"] == "successor"

    def test_pure_function_empty(self, tmp_path):
        db_path = tmp_path / "events.db"
        store = EventStore(db_path)
        store.close()

        from hub.mcp_server import _get_session_chain
        chain = _get_session_chain(str(db_path), "nonexistent")
        assert chain == []

    def test_session_detail_includes_links(self, tmp_path):
        db_path = tmp_path / "events.db"
        store = EventStore(db_path)
        _seed_session(store, "s1", "claude", title="One")
        _seed_session(store, "s2", "opencode", title="Two")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        store.close()

        from hub.mcp_server import _get_session_detail
        detail = _get_session_detail(str(db_path), "s1")
        assert detail is not None
        assert "linked_sessions" in detail
        assert len(detail["linked_sessions"]) == 1
        assert detail["linked_sessions"][0]["session_id"] == "s2"

    def test_session_detail_no_links(self, tmp_path):
        db_path = tmp_path / "events.db"
        store = EventStore(db_path)
        _seed_session(store, "s1", "claude", title="One")
        store.close()

        from hub.mcp_server import _get_session_detail
        detail = _get_session_detail(str(db_path), "s1")
        assert detail is not None
        assert "linked_sessions" not in detail


class TestCLILink:
    def test_cmd_link(self, store, capsys, monkeypatch):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        db_path = store.db_path

        import hub.cache.event_store as es_mod
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", db_path)

        from hub.cli import cmd_link
        import argparse
        args = argparse.Namespace(source="s1", target="s2", type="continues")
        cmd_link(args)

        out = capsys.readouterr().out
        assert "Linked" in out

    def test_cmd_link_not_found(self, store, capsys, monkeypatch):
        db_path = store.db_path

        import hub.cache.event_store as es_mod
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", db_path)

        from hub.cli import cmd_link
        import argparse
        args = argparse.Namespace(source="nope", target="s2", type="continues")
        cmd_link(args)

        out = capsys.readouterr().out
        assert "not found" in out


class TestCLIChain:
    def test_cmd_chain(self, store, capsys, monkeypatch):
        _seed_session(store, "s1", "claude", title="First")
        _seed_session(store, "s2", "opencode", title="Second")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        db_path = store.db_path

        import hub.cache.event_store as es_mod
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", db_path)

        from hub.cli import cmd_chain
        import argparse
        args = argparse.Namespace(session_id="s1", json_output=False)
        cmd_chain(args)

        out = capsys.readouterr().out
        assert "Session chain" in out
        assert "continues" in out

    def test_cmd_chain_json(self, store, capsys, monkeypatch):
        _seed_session(store, "s1", "claude")
        _seed_session(store, "s2", "opencode")
        store.link_sessions("s1", "claude", "s2", "opencode", "continues")
        db_path = store.db_path

        import hub.cache.event_store as es_mod
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", db_path)

        from hub.cli import cmd_chain
        import argparse
        args = argparse.Namespace(session_id="s1", json_output=True)
        cmd_chain(args)

        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_cmd_chain_empty(self, store, capsys, monkeypatch):
        db_path = store.db_path

        import hub.cache.event_store as es_mod
        monkeypatch.setattr(es_mod, "DEFAULT_DB_PATH", db_path)

        from hub.cli import cmd_chain
        import argparse
        args = argparse.Namespace(session_id="nonexistent", json_output=False)
        cmd_chain(args)

        out = capsys.readouterr().out
        assert "No linked sessions" in out
