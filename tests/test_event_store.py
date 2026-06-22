"""Tests for EventStore (SQLite persistence)."""

import json
import time
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore


@pytest.fixture
def store(tmp_path) -> EventStore:
    """Create a temporary EventStore."""
    db = tmp_path / "events.db"
    s = EventStore(db)
    yield s
    s.close()


class TestEventStoreStore:
    def test_store_single_event(self, store):
        store.store({
            "provider": "claude", "project": "test", "event_type": "user",
            "timestamp": "2026-04-08T10:00:00", "summary": "hello",
        })
        assert store.count() == 1

    def test_store_with_tokens(self, store):
        store.store({
            "provider": "claude", "project": "test", "event_type": "user",
            "timestamp": "2026-04-08T10:00:00", "summary": "hello",
            "tokens": {"input": 100, "output": 50},
            "tool_name": "Bash", "file_path": "/tmp/test",
            "model": "claude-sonnet-4", "cwd": "/tmp",
            "session_id": "s1",
        })
        events = store.load_recent()
        assert len(events) == 1
        e = events[0]
        assert e["tokens"] == {"input": 100, "output": 50}
        assert e["tool_name"] == "Bash"
        assert e["model"] == "claude-sonnet-4"
        assert e["cwd"] == "/tmp"
        assert e["session_id"] == "s1"


class TestEventStoreBatch:
    def test_store_batch(self, store):
        events = [
            {"provider": "claude", "project": "p1", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "msg1"},
            {"provider": "codex", "project": "p1", "event_type": "assistant",
             "timestamp": "2026-04-08T10:00:05", "summary": "msg2"},
            {"provider": "qwen", "project": "p2", "event_type": "tool_use",
             "timestamp": "2026-04-08T10:00:10", "summary": "msg3"},
        ]
        store.store_batch(events)
        assert store.count() == 3

    def test_store_batch_empty(self, store):
        store.store_batch([])
        assert store.count() == 0


class TestEventStoreLoadRecent:
    def test_load_recent_default_limit(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": f"2026-04-08T10:00:{i:02d}", "summary": f"msg{i}"}
            for i in range(10)
        ]
        store.store_batch(events)
        result = store.load_recent(limit=5)
        assert len(result) == 5
        # load_recent orders by id DESC then reverses, so chronological within the limit
        # Last 5 stored are msg5-msg9, reversed gives msg5 first
        assert result[0]["summary"] == "msg5"

    def test_load_recent_chronological_order(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": f"2026-04-08T10:00:{i:02d}", "summary": f"msg{i}"}
            for i in range(3)
        ]
        store.store_batch(events)
        result = store.load_recent()
        # Should be chronological (oldest first after reversal)
        assert result[0]["summary"] == "msg0"
        assert result[1]["summary"] == "msg1"
        assert result[2]["summary"] == "msg2"


class TestEventStoreQuery:
    def test_query_by_provider(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "c1"},
            {"provider": "codex", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "x1"},
        ]
        store.store_batch(events)

        result = store.query(provider="claude")
        assert len(result) == 1
        assert result[0]["summary"] == "c1"

    def test_query_by_project(self, store):
        events = [
            {"provider": "claude", "project": "YAAHub", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "y1"},
            {"provider": "claude", "project": "other", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "o1"},
        ]
        store.store_batch(events)

        result = store.query(project="YAA")
        assert len(result) == 1

    def test_query_by_since(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-01-01T00:00:00", "summary": "old"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "new"},
        ]
        store.store_batch(events)

        result = store.query(since="2026-04-01")
        assert len(result) == 1
        assert result[0]["summary"] == "new"

    def test_query_no_filters_returns_all(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": f"msg{i}"}
            for i in range(5)
        ]
        store.store_batch(events)
        result = store.query()
        assert len(result) == 5


class TestEventStoreAnalytics:
    def test_analytics_basic(self, store):
        events = [
            {"provider": "claude", "project": "p1", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "msg1",
             "tokens": {"input": 100, "output": 50}},
            {"provider": "codex", "project": "p1", "event_type": "assistant",
             "timestamp": "2026-04-08T10:00:05", "summary": "msg2",
             "tokens": {"input": 200, "output": 100}},
            {"provider": "claude", "project": "p2", "event_type": "tool_use",
             "timestamp": "2026-04-08T11:00:00", "summary": "msg3",
             "tool_name": "Bash"},
        ]
        store.store_batch(events)

        result = store.analytics()
        assert result["total_events"] == 3
        assert result["total_projects"] == 2
        assert len(result["by_provider"]) == 2

    def test_analytics_with_since(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-01-01T00:00:00", "summary": "old",
             "tokens": {"input": 999, "output": 999}},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "new",
             "tokens": {"input": 100, "output": 50}},
        ]
        store.store_batch(events)

        result = store.analytics(since="2026-04-01")
        assert result["total_events"] == 1
        assert result["tokens"]["input"] == 100

    def test_analytics_no_since_returns_all(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2020-01-01T00:00:00", "summary": "old"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "new"},
        ]
        store.store_batch(events)

        result = store.analytics(since=None)
        assert result["total_events"] == 2

    def test_analytics_sql_injection_safe(self, store):
        """Parameterized query prevents SQL injection via since."""
        store.store({
            "provider": "claude", "project": "p", "event_type": "user",
            "timestamp": "2020-01-01T00:00:00", "summary": "msg",
        })
        # Intento de inyeccion: si NO estuviera parametrizado, esto borraria la tabla
        result = store.analytics(since="2026-04-08' OR '1'='1")
        # Con parametrizacion el string se trata como dato literal — no ejecuta SQL
        # La tabla sigue intacta (prueba clave: DROP TABLE no se ejecuto)
        assert store.count() == 1
        # El timestamp '2020-01-01' < '2026-04-08...' inyeccion, asi que no matchea
        assert result["total_events"] == 0

    def test_analytics_injection_union_safe(self, store):
        """UNION injection attempt should be treated as literal string."""
        store.store({
            "provider": "claude", "project": "p", "event_type": "user",
            "timestamp": "2020-01-01T00:00:00", "summary": "msg",
        })
        result = store.analytics(since="' UNION SELECT * FROM events WHERE '1'='1")
        # Parametrizado: se compara como string literal, no ejecuta el UNION
        # La tabla sigue intacta — si el UNION se ejecutara habria error de schema
        assert store.count() == 1
        # No crash = la query se ejecuto correctamente con ? placeholder
        # (el resultado exacto depende de comparacion de strings, no de SQL injection)
        assert "total_events" in result

    def test_analytics_by_provider(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "c1",
             "tokens": {"input": 100, "output": 50}},
            {"provider": "codex", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "x1",
             "tokens": {"input": 200, "output": 100}},
        ]
        store.store_batch(events)

        result = store.analytics()
        providers = {p["provider"]: p["events"] for p in result["by_provider"]}
        assert providers["claude"] == 1
        assert providers["codex"] == 1

    def test_analytics_by_type(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "m1"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "m2"},
            {"provider": "claude", "project": "p", "event_type": "assistant",
             "timestamp": "2026-04-08T10:00:02", "summary": "m3"},
        ]
        store.store_batch(events)

        result = store.analytics()
        types = {t["type"]: t["count"] for t in result["by_type"]}
        assert types["user"] == 2
        assert types["assistant"] == 1

    def test_analytics_by_tool(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "tool_use",
             "timestamp": "2026-04-08T10:00:00", "summary": "t1",
             "tool_name": "Bash"},
            {"provider": "claude", "project": "p", "event_type": "tool_use",
             "timestamp": "2026-04-08T10:00:01", "summary": "t2",
             "tool_name": "Bash"},
            {"provider": "claude", "project": "p", "event_type": "tool_use",
             "timestamp": "2026-04-08T10:00:02", "summary": "t3",
             "tool_name": "Read"},
        ]
        store.store_batch(events)

        result = store.analytics()
        tools = {t["name"]: t["count"] for t in result["by_tool"]}
        assert tools["Bash"] == 2
        assert tools["Read"] == 1

    def test_analytics_total_tokens(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "m1",
             "tokens": {"input": 1000, "output": 500, "cached_input": 200}},
        ]
        store.store_batch(events)

        result = store.analytics()
        assert result["tokens"]["input"] == 1000
        assert result["tokens"]["output"] == 500
        assert result["tokens"]["cached"] == 200

    def test_analytics_sessions_count(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "m1",
             "session_id": "sess-a"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "m2",
             "session_id": "sess-a"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:02", "summary": "m3",
             "session_id": "sess-b"},
        ]
        store.store_batch(events)

        result = store.analytics()
        assert result["total_sessions"] == 2

    def test_analytics_hourly(self, store):
        events = [
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "m1"},
            {"provider": "claude", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T10:30:00", "summary": "m2"},
            {"provider": "codex", "project": "p", "event_type": "user",
             "timestamp": "2026-04-08T11:00:00", "summary": "m3"},
        ]
        store.store_batch(events)

        result = store.analytics()
        assert len(result["hourly"]) >= 1
        # Hourly should group by the hour prefix
        hours = {h["hour"]: h["total"] for h in result["hourly"]}
        assert "2026-04-08T10" in hours
        assert hours["2026-04-08T10"] == 2


class TestEventStoreStatsSummary:
    def test_stats_summary(self, store):
        events = [
            {"provider": "claude", "project": "p1", "event_type": "user",
             "timestamp": "2026-04-08T10:00:00", "summary": "m1"},
            {"provider": "codex", "project": "p2", "event_type": "user",
             "timestamp": "2026-04-08T10:00:01", "summary": "m2"},
        ]
        store.store_batch(events)

        stats = store.stats_summary()
        assert stats["total_events"] == 2
        assert stats["by_provider"]["claude"] == 1
        assert stats["by_provider"]["codex"] == 1
        assert len(stats["top_projects"]) == 2


import threading


class TestEventStoreConcurrency:
    def test_concurrent_writes(self, store):
        """Multiple threads writing simultaneously should not crash."""
        errors = []
        def write_events(thread_id):
            try:
                for i in range(20):
                    store.store({
                        "provider": "claude", "project": f"p{thread_id}",
                        "event_type": "user",
                        "timestamp": f"2026-04-08T10:00:{i:02d}",
                        "summary": f"thread-{thread_id}-msg-{i}",
                    })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_events, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent writes failed: {errors}"
        assert store.count() == 100  # 5 threads × 20 events

    def test_concurrent_read_write(self, store):
        """Reading while writing should not crash or deadlock."""
        store.store({
            "provider": "claude", "project": "p", "event_type": "user",
            "timestamp": "2026-04-08T10:00:00", "summary": "seed",
        })
        errors = []
        def reader():
            try:
                for _ in range(20):
                    store.analytics()
                    store.stats_summary()
                    store.load_recent()
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(20):
                    store.store({
                        "provider": "claude", "project": "p",
                        "event_type": "user",
                        "timestamp": f"2026-04-08T10:01:{i:02d}",
                        "summary": f"msg-{i}",
                    })
            except Exception as e:
                errors.append(e)

        t_read = threading.Thread(target=reader)
        t_write = threading.Thread(target=writer)
        t_read.start()
        t_write.start()
        t_read.join(timeout=10)
        t_write.join(timeout=10)

        assert errors == [], f"Concurrent read/write failed: {errors}"
        assert not t_read.is_alive(), "Reader thread deadlocked"
        assert not t_write.is_alive(), "Writer thread deadlocked"

    def test_no_fd_leak_after_many_calls(self, store):
        """Repeated calls from different threads should not leak fds."""
        import os

        def count_open_fds():
            try:
                return len(os.listdir("/dev/fd"))
            except FileNotFoundError:
                return len(os.listdir("/proc/self/fd"))

        # Get baseline fd count
        baseline_fds = count_open_fds()

        def call_analytics():
            store.analytics()

        # Simulate 50 HTTP request threads (like analytics page auto-refresh)
        threads = []
        for _ in range(50):
            t = threading.Thread(target=call_analytics)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # fd count should not have grown significantly
        final_fds = count_open_fds()
        leaked = final_fds - baseline_fds
        assert leaked < 5, f"Leaked {leaked} fds after 50 concurrent analytics calls"
