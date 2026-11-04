"""#52: MCP search_events returns the most recent events first, even after a backfill."""

from __future__ import annotations

import sqlite3

from hub.mcp_server import _search_events


def _db(tmp_path, with_historical=True):
    path = tmp_path / "events.db"
    conn = sqlite3.connect(path)
    hist = ", historical INTEGER NOT NULL DEFAULT 0" if with_historical else ""
    conn.execute(f"""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
        project TEXT NOT NULL, event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
        summary TEXT NOT NULL, session_id TEXT, tokens_json TEXT, tool_name TEXT,
        file_path TEXT, model TEXT, cwd TEXT, fingerprint TEXT,
        created_at REAL NOT NULL{hist})""")
    return conn, str(path)


def _ins(conn, ts, summary, historical=0, with_historical=True):
    if with_historical:
        conn.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " created_at, historical) VALUES ('claude','demo','user',?,?,0,?)",
            (ts, summary, historical),
        )
    else:
        conn.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " created_at) VALUES ('claude','demo','user',?,?,0)",
            (ts, summary),
        )


def test_backfill_does_not_push_recent_down(tmp_path):
    conn, path = _db(tmp_path)
    # Live events first (low ids), then a backfill inserts old history (high ids).
    _ins(conn, "2026-11-03T10:00:00Z", "needle live-a")
    _ins(conn, "2026-11-04T09:00:00.500Z", "needle live-b")
    _ins(conn, "2025-01-01T00:00:00Z", "needle old-1", historical=1)
    _ins(conn, "2025-02-01T00:00:00Z", "needle old-2", historical=1)
    conn.commit()
    got = [r["summary"] for r in _search_events(path, "needle")]
    assert got == ["needle live-b", "needle live-a", "needle old-2", "needle old-1"]


def test_mixed_offsets_are_parsed(tmp_path):
    conn, path = _db(tmp_path)
    # 08:00-03:00 == 11:00Z → newer than 10:30Z although it sorts lower as text.
    _ins(conn, "2026-11-04T10:30:00Z", "needle utc")
    _ins(conn, "2026-11-04T08:00:00-03:00", "needle offset")
    conn.commit()
    got = [r["summary"] for r in _search_events(path, "needle")]
    assert got == ["needle offset", "needle utc"]


def test_tie_and_unparseable(tmp_path):
    conn, path = _db(tmp_path)
    _ins(conn, "not-a-time", "needle bad")
    _ins(conn, "2026-11-04T10:00:00Z", "needle hist", historical=1)
    _ins(conn, "2026-11-04T10:00:00Z", "needle live")
    conn.commit()
    got = [r["summary"] for r in _search_events(path, "needle")]
    # Same instant → live before historical; unparseable time sinks to the end.
    assert got == ["needle live", "needle hist", "needle bad"]


def test_pagination_follows_time_order(tmp_path):
    conn, path = _db(tmp_path)
    for i in range(5):
        _ins(conn, f"2026-11-0{5 - i}T00:00:00Z", f"needle {i}")
    conn.commit()
    p1 = _search_events(path, "needle", limit=2)
    p2 = _search_events(path, "needle", limit=2, offset=2)
    assert [r["summary"] for r in p1 + p2] == [f"needle {i}" for i in range(4)]


def test_db_without_historical_column(tmp_path):
    conn, path = _db(tmp_path, with_historical=False)
    _ins(conn, "2026-11-04T10:00:00Z", "needle new", with_historical=False)
    _ins(conn, "2025-11-04T10:00:00Z", "needle old", with_historical=False)
    conn.commit()
    assert [r["summary"] for r in _search_events(path, "needle")] == ["needle new", "needle old"]


def test_epoch_timestamps_are_parsed(tmp_path):
    conn, path = _db(tmp_path)
    # 1730000000000 ms = 2024-10-27; as a raw julianday it would sort first.
    _ins(conn, "1730000000000", "needle epoch-ms")
    _ins(conn, "1740000000", "needle epoch-s")          # 2025-02-19
    _ins(conn, "2026-11-04T10:00:00Z", "needle iso")
    conn.execute(  # a real INTEGER value, not text
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " created_at) VALUES ('claude','demo','user',?,?,0)",
        (1600000000000, "needle int-ms"))                # 2020-09-13
    conn.commit()
    got = [r["summary"] for r in _search_events(path, "needle")]
    assert got == ["needle iso", "needle epoch-s", "needle epoch-ms", "needle int-ms"]
