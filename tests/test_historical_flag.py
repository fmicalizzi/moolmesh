"""History ingested by backfill/catch-up/re-parse never poses as recent (#45)."""

from __future__ import annotations

import collections
import json
import os
import sqlite3
import time
from pathlib import Path

from hub.backfill import run_backfill, run_reparse_codex
from hub.cache.event_store import EventStore
from hub.mcp_server import _get_recent_events
from hub.watchers.claude_watcher import ClaudeWatcher

DAY = 86400


def _claude_file(base: Path, sid: str, age: float, n: int = 3) -> Path:
    proj = base / "-Users-test-alpha"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps({
        "type": "user", "sessionId": sid, "cwd": "/Users/test/alpha",
        "uuid": f"{sid}-{i}", "timestamp": f"2026-03-15T10:0{i}:00.000Z",
        "message": {"role": "user", "content": f"msg {i} {sid}"},
    }) for i in range(n)) + "\n")
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def _live_harvest(store, base):
    w = ClaudeWatcher(store, collections.deque(), claude_base=base)
    w._rescan()
    for path, fp in list(w._watched_files.items()):
        w._harvest_file(path, fp)
    return w


def _flags(store) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for sid, h in store._conn.execute("SELECT session_id, historical FROM events"):
        out.setdefault(sid, set()).add(h)
    return out


def test_live_rows_are_0_and_backfilled_rows_are_1(tmp_path):
    base = tmp_path / "claude"
    store = EventStore(db_path=tmp_path / "events.db")
    _claude_file(base, "live", age=600)
    _live_harvest(store, base)
    _claude_file(base, "old", age=3 * DAY)
    run_backfill(store, providers=["claude"], bases={"claude": base},
                 db_path=tmp_path / "events.db")
    assert _flags(store) == {"live": {0}, "old": {1}}
    store.close()


def test_catchup_rows_are_historical(tmp_path):
    base = tmp_path / "claude"
    store = EventStore(db_path=tmp_path / "events.db")
    store.set_watcher_cycle("claude", time.time() - 3 * DAY)
    _claude_file(base, "outage", age=2 * DAY)
    w = ClaudeWatcher(store, collections.deque(), claude_base=base)
    w._plan_catchup()
    w._running = True
    w._drain_catchup(10)
    assert _flags(store) == {"outage": {1}}
    store.close()


def test_reparsed_rows_are_historical(tmp_path):
    codex = tmp_path / ".codex"
    d = codex / "sessions" / "2026" / "03" / "25"
    d.mkdir(parents=True)
    f = d / "rollout-2026-03-25T10-00-00-a.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "session_meta", "timestamp": "2026-03-25T10:00:00Z",
         "payload": {"id": "sess-a", "cwd": "/Users/test/cx"}},
        {"type": "event_msg", "timestamp": "2026-03-25T10:00:05Z",
         "payload": {"type": "user_message", "message": "hi"}},
    ]) + "\n")
    t = time.time() - 3 * DAY
    os.utime(f, (t, t))
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    run_backfill(store, providers=["codex"], bases={"codex": codex}, db_path=db)
    # Pretend the rows were live-ingested with an old shape, then re-parse.
    store._conn.execute("UPDATE events SET historical = 0, file_path = 'x'")
    store._conn.commit()
    rep = run_reparse_codex(store, dry_run=False, yes=True, db_path=db,
                            bases={"codex": codex}, backup_dir=tmp_path / "bk")
    assert rep.groups == 1
    assert _flags(store) == {"sess-a": {1}}
    store.close()


def test_recent_readers_skip_history_even_with_higher_ids(tmp_path):
    base = tmp_path / "claude"
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    _claude_file(base, "live", age=600)
    _live_harvest(store, base)
    live_max = store.get_max_id()
    _claude_file(base, "old", age=3 * DAY)
    run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)
    assert store.get_max_id() > live_max  # history got the newest ids

    assert {e["session_id"] for e in store.load_recent(500)} == {"live"}
    assert store.load_since_id(0) and all(
        e["session_id"] == "live" for e in store.load_since_id(0)
    )
    assert store.load_since_id(live_max) == []  # jumps over historical ids
    assert {e["session_id"] for e in _get_recent_events(str(db), 50)} == {"live"}

    # A live event after the backfill replays normally past the historical ids.
    f = base / "-Users-test-alpha" / "live.jsonl"
    with open(f, "a") as fh:
        fh.write(json.dumps({
            "type": "user", "sessionId": "live", "cwd": "/Users/test/alpha",
            "uuid": "live-new", "timestamp": "2026-09-26T10:00:00.000Z",
            "message": {"role": "user", "content": "new live message"},
        }) + "\n")
    _live_harvest(store, base)
    replay = store.load_since_id(live_max)
    assert [e["summary"] for e in replay] and replay[0]["id"] > live_max
    assert all(e["session_id"] == "live" for e in replay)
    store.close()


def test_rehaversting_a_historical_file_adds_no_duplicates(tmp_path):
    base = tmp_path / "claude"
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    _claude_file(base, "old", age=3 * DAY)
    run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)
    n = store.count()
    store._conn.execute("DELETE FROM file_registry")  # force a re-read from 0
    store._conn.commit()
    rep = run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)
    assert rep.providers[0].events_inserted == 0
    assert store.count() == n
    store.close()


def test_migration_on_old_db_leaves_existing_rows_live(tmp_path):
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    store.store({"provider": "claude", "project": "p", "event_type": "user",
                 "timestamp": "2026-01-01T00:00:00Z", "summary": "pre-existing",
                 "session_id": "s"})
    store.close()
    # Rewind to a pre-#45b schema: drop the column/index and the migration row.
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX idx_events_live")
    conn.execute("ALTER TABLE events DROP COLUMN historical")
    conn.execute("DELETE FROM schema_migrations WHERE version = 3")
    conn.commit()
    conn.close()

    store = EventStore(db_path=db)
    rows = store._conn.execute("SELECT summary, historical FROM events").fetchall()
    assert rows == [("pre-existing", 0)]
    assert [e["summary"] for e in store.load_recent()] == ["pre-existing"]
    store.close()


def test_load_recent_uses_the_live_index(tmp_path):
    store = EventStore(db_path=tmp_path / "events.db")
    plan = " ".join(r[-1] for r in store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM events WHERE historical = 0 "
        "ORDER BY id DESC LIMIT 500"
    ))
    assert "idx_events_live" in plan
    store.close()


def test_api_recent_after_backfill_returns_only_live(tmp_path):
    import http.server
    import threading
    import urllib.request

    from hub.dashboard.server import DashboardServer

    base = tmp_path / "claude"
    db = tmp_path / "events.db"
    store = EventStore(db_path=db)
    _claude_file(base, "live", age=600)
    _live_harvest(store, base)
    _claude_file(base, "old", age=3 * DAY)
    run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)

    srv = DashboardServer(host="127.0.0.1", port=0)
    srv.event_store = store
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/recent"
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
    finally:
        httpd.shutdown()
    assert {e["session_id"] for e in payload["events"]} == {"live"}
    # max_id is the global max: the client's next replay starts past history.
    assert payload["max_id"] == store.get_max_id()
    assert store.load_since_id(payload["max_id"]) == []
    store.close()
