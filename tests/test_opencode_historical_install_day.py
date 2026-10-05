"""OpenCode live rows ingested before #45 date sessions to the install day (#61).

OpenCode sessions first ingested live (``historical = 0``) before the #45 flag
existed carry their activity clock on the provider's first-ingest day even
though their events are older. The one-off events.db migration 5 marks
``historical = 1`` on rows ingested on that first day whose parsed event time is
strictly earlier, so ``read_session_activity`` (#53) dates them by the event.
Bounded: only OpenCode, only the first-day window, never a row with an
unreadable timestamp.
"""

import sqlite3
from datetime import datetime

from hub.cache.event_store import EventStore
from hub.cache.workspace_store import read_session_activity

INSTALL = datetime(2026, 6, 22, 10, 0, 0).timestamp()      # local install day
NEXT_DAY = datetime(2026, 6, 23, 9, 0, 0).timestamp()


def _fresh_db(path) -> None:
    store = EventStore(path)
    store.close()
    c = sqlite3.connect(str(path))
    c.execute("DELETE FROM schema_migrations WHERE version = 5")
    c.commit()
    c.close()


def _insert(path, provider, timestamp, created_at, historical=0, session="s",
            summary="x") -> int:
    c = sqlite3.connect(str(path))
    cur = c.execute(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, created_at, historical) VALUES (?,?,?,?,?,?,?,?)",
        (provider, "p", "user", timestamp, summary, session, created_at,
         historical),
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _flags(path) -> dict[int, int]:
    c = sqlite3.connect(str(path))
    out = dict(c.execute("SELECT id, historical FROM events").fetchall())
    c.close()
    return out


def test_marks_only_the_bounded_first_ingest_day_rows(tmp_path):
    db = tmp_path / "events.db"
    _fresh_db(db)
    old1 = _insert(db, "opencode", "2026-06-20T10:00:00", INSTALL, summary="a")
    old2 = _insert(db, "opencode", "2026-06-01T10:00:00Z", INSTALL, summary="b")
    same_day = _insert(db, "opencode", "2026-06-22T15:00:00", INSTALL)
    later_ingest = _insert(db, "opencode", "2026-06-20T10:00:00", NEXT_DAY)
    unreadable = _insert(db, "opencode", "not-a-date", INSTALL)
    other_provider = _insert(db, "claude", "2026-06-20T10:00:00", INSTALL)
    already = _insert(db, "opencode", "2026-06-01T10:00:00", INSTALL,
                      historical=1)
    later_day_event = _insert(db, "opencode", "2026-06-25T10:00:00", INSTALL)

    store = EventStore(db)  # second open runs migration 5
    try:
        flags = _flags(db)
        with store._lock:
            applied = store._conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
            ).fetchone()[0]
    finally:
        store.close()

    assert flags[old1] == 1 and flags[old2] == 1          # pre-install events
    assert flags[same_day] == 0                            # same day, later time
    assert flags[later_ingest] == 0                        # ingested later
    assert flags[unreadable] == 0                          # never guess
    assert flags[other_provider] == 0                      # bounded to OpenCode
    assert flags[already] == 1                             # untouched
    assert flags[later_day_event] == 0                     # event after day start
    assert applied == 1


def test_second_open_is_a_no_op(tmp_path):
    db = tmp_path / "events.db"
    _fresh_db(db)
    row = _insert(db, "opencode", "2026-06-20T10:00:00", INSTALL)
    EventStore(db).close()

    # A row inserted after the migration stays live; re-opening must not touch
    # it nor re-run the migration.
    live = _insert(db, "opencode", "2026-06-20T10:00:00", INSTALL)
    store = EventStore(db)
    try:
        flags = _flags(db)
        with store._lock:
            applied = store._conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
            ).fetchone()[0]
    finally:
        store.close()
    assert flags[row] == 1
    assert flags[live] == 0     # inserted post-migration: never touched
    assert applied == 1         # recorded once


def test_migrated_session_is_dated_by_its_event_time(tmp_path):
    """The user-visible effect: a session whose only rows were first-day live is
    now dated by its events, not the install day."""
    db = tmp_path / "events.db"
    _fresh_db(db)
    _insert(db, "opencode", "2026-06-01T10:00:00", INSTALL, session="old")
    _insert(db, "opencode", "2026-06-05T10:00:00", INSTALL, session="old")
    _insert(db, "opencode", "2026-06-20T10:00:00", NEXT_DAY, session="fresh")

    store = EventStore(db)
    try:
        activity = read_session_activity(store._conn)
    finally:
        store.close()
    # Naive fixture timestamps are local; compare as epochs, not as UTC walls.
    assert activity[("old", "opencode")].timestamp() == datetime(
        2026, 6, 5, 10, 0).timestamp()
    assert activity[("fresh", "opencode")].timestamp() == NEXT_DAY
