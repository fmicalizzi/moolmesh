"""file_registry keyed by (fingerprint, path) — issue #50.

Two files that start with the same 1 KB used to share ONE offset row, so the
second one harvested resumed from the first one's offset and skipped its own
content. These tests pin the composite key, rename vs. collision, the
additive migration of old registries, the backfill recovery and the stable key
of the SQLite-backed providers.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from hub.backfill import run_backfill
from hub.cache.event_store import (
    EventStore,
    _mig_4_registry_path_key,
    file_fingerprint,
    registry_path,
)
from hub.watchers.claude_watcher import ClaudeWatcher
from hub.watchers.opencode_watcher import OpenCodeWatcher

DAY = 86400

_OLD_REGISTRY = """
    CREATE TABLE file_registry (
        fingerprint TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        file_path TEXT NOT NULL,
        last_offset INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
"""


def _line(sid: str, i: int, text: str) -> str:
    return json.dumps({
        "type": "user", "sessionId": sid, "cwd": "/Users/test/alpha",
        "uuid": f"{sid}-{i}", "timestamp": f"2026-03-15T10:{i:02d}:00.000Z",
        "message": {"role": "user", "content": text},
    })


def _twin_files(base: Path, n_a: int = 2, n_b: int = 6, age: float | None = None):
    """Two Claude files whose first line (> 1 KB) is identical — a fork."""
    proj = base / "-Users-test-alpha"
    proj.mkdir(parents=True, exist_ok=True)
    head = _line("shared", 0, "x" * 1500)
    a = proj / "a.jsonl"
    b = proj / "b.jsonl"
    a.write_text("\n".join([head] + [_line("sa", i, f"a {i}") for i in range(1, n_a + 1)]) + "\n")
    b.write_text("\n".join([head] + [_line("sb", i, f"b {i}") for i in range(1, n_b + 1)]) + "\n")
    if age is not None:
        t = time.time() - age
        for f in (a, b):
            os.utime(f, (t, t))
    assert file_fingerprint(a) == file_fingerprint(b)
    return a, b


def _event_fps(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT fingerprint FROM events")}
    finally:
        conn.close()


def _parse_fps(watcher, *files: Path) -> set[str]:
    from hub.cache.event_store import _compute_fingerprint
    out: set[str] = set()
    for f in files:
        events, _ = watcher._parse_and_adapt(f, 0)
        out |= {_compute_fingerprint(e) for e in events}
    return out


def _make_old_registry(db: Path, rows: list[tuple]) -> None:
    """Turn an EventStore DB back into the pre-#50 registry shape."""
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE file_registry")
    conn.execute(_OLD_REGISTRY)
    conn.executemany("INSERT INTO file_registry VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute("DELETE FROM schema_migrations WHERE version = 4")
    conn.commit()
    conn.close()


class _OffsetSpy:
    """Wraps a watcher's ``_parse_and_adapt`` to record the offsets it gets."""

    def __init__(self, watcher):
        self.offsets: list[int] = []
        real = watcher._parse_and_adapt

        def spy(path, offset):
            self.offsets.append(offset)
            return real(path, offset)

        watcher._parse_and_adapt = spy


class TestCompositeKey:
    def test_twin_files_get_independent_offsets_and_full_ingest(self, tmp_path):
        a, b = _twin_files(tmp_path / "claude")
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        w = ClaudeWatcher(store, claude_base=tmp_path / "claude")
        fp = file_fingerprint(a)
        w._harvest_file(a, fp)
        w._harvest_file(b, fp)

        assert store.get_offset(fp, a) == a.stat().st_size
        assert store.get_offset(fp, b) == b.stat().st_size
        assert _event_fps(db) == _parse_fps(w, a, b)
        store.close()

    def test_second_harvest_resumes_with_unnormalized_path(self, tmp_path):
        a, _b = _twin_files(tmp_path / "claude")
        odd = Path(str(a.parent / "sub" / ".." / a.name))  # same file, other spelling
        (a.parent / "sub").mkdir()
        store = EventStore(db_path=tmp_path / "events.db")
        w = ClaudeWatcher(store, claude_base=tmp_path / "claude")
        spy = _OffsetSpy(w)
        fp = file_fingerprint(a)
        w._harvest_file(odd, fp)
        w._harvest_file(odd, fp)
        w._harvest_file(a, fp)
        assert spy.offsets[0] == 0
        assert spy.offsets[1:] == [a.stat().st_size] * 2
        rows = store._conn.execute("SELECT file_path FROM file_registry").fetchall()
        assert rows == [(registry_path(a),)]
        store.close()

    def test_rename_keeps_offset(self, tmp_path):
        a, _b = _twin_files(tmp_path / "claude")
        store = EventStore(db_path=tmp_path / "events.db")
        w = ClaudeWatcher(store, claude_base=tmp_path / "claude")
        fp = file_fingerprint(a)
        w._harvest_file(a, fp)
        size = a.stat().st_size

        moved = a.with_name("a-renamed.jsonl")
        os.rename(a, moved)
        spy = _OffsetSpy(w)
        w._harvest_file(moved, fp)
        assert spy.offsets == [size]  # no re-read
        rows = store._conn.execute(
            "SELECT file_path, last_offset FROM file_registry WHERE fingerprint = ?", (fp,)
        ).fetchall()
        assert rows == [(registry_path(moved), size)]  # adopted, not duplicated
        store.close()

    def test_collision_with_live_file_starts_from_zero(self, tmp_path):
        a, b = _twin_files(tmp_path / "claude")
        store = EventStore(db_path=tmp_path / "events.db")
        fp = file_fingerprint(a)
        store.save_offset(fp, "claude", str(a), a.stat().st_size)
        assert store.get_offset(fp, b) is None
        # the other file's row is untouched
        assert store.get_offset(fp, a) == a.stat().st_size
        store.close()


class TestMigration:
    def _old_db(self, path: Path, rows: list[tuple]) -> None:
        conn = sqlite3.connect(path)
        conn.execute(_OLD_REGISTRY)
        conn.executemany("INSERT INTO file_registry VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def _rows(self, n: int = 40) -> list[tuple]:
        rows = [(f"fp{i:03d}", "claude", f"/Users/x/.claude/p/{i}.jsonl", i * 100, 1000.0 + i)
                for i in range(n)]
        # OpenCode: one DB, a new content fingerprint per daemon start
        rows += [(f"oc{i}", "opencode", "/Users/x/.local/share/opencode/opencode.db",
                  10 * i, 2000.0 + i) for i in range(3)]
        return rows

    def test_rows_and_offsets_preserved(self, tmp_path):
        db = tmp_path / "events.db"
        rows = self._rows()
        self._old_db(db, rows)
        store = EventStore(db_path=db)
        got = store._conn.execute(
            "SELECT fingerprint, provider, file_path, last_offset, updated_at, legacy"
            " FROM file_registry ORDER BY fingerprint"
        ).fetchall()
        assert got == sorted((*r[:2], registry_path(r[2]), *r[3:], 1) for r in rows)
        pk = [r[1] for r in sorted(
            (r for r in store._conn.execute("PRAGMA table_info(file_registry)") if r[5]),
            key=lambda r: r[5])]
        assert pk == ["fingerprint", "file_path"]
        assert store._conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 4").fetchone()[0] == 1
        store.close()

        again = EventStore(db_path=db)  # reopen: runs nothing, loses nothing
        assert again._conn.execute("SELECT COUNT(*) FROM file_registry").fetchone()[0] == len(rows)
        again.close()

    def test_idempotent_and_survives_leftover_temp_table(self, tmp_path):
        db = tmp_path / "events.db"
        rows = self._rows(5)
        self._old_db(db, rows)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE file_registry_new (junk TEXT)")  # interrupted run
        conn.commit()
        _mig_4_registry_path_key(conn)
        _mig_4_registry_path_key(conn)  # already composite: no-op
        assert conn.execute("SELECT COUNT(*) FROM file_registry").fetchone()[0] == len(rows)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'file_registry_new'").fetchone() is None
        conn.close()

    def test_fresh_db_has_composite_key(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        store.save_offset("fp", "claude", "/a.jsonl", 10)
        store.save_offset("fp", "claude", "/b.jsonl", 20)
        assert store.get_offset("fp", "/a.jsonl") == 10
        assert store.get_offset("fp", "/b.jsonl") == 20
        assert store.is_legacy_offset("fp", "/a.jsonl") is False
        store.close()

    def test_live_upsert_keeps_legacy_flag(self, tmp_path):
        db = tmp_path / "events.db"
        self._old_db(db, [("fp", "claude", "/a.jsonl", 10, 1.0)])
        store = EventStore(db_path=db)
        assert store.is_legacy_offset("fp", "/a.jsonl") is True
        store.store_with_offset([], "fp", "claude", "/a.jsonl", 50)
        store.save_offset("fp", "claude", "/a.jsonl", 60)
        assert store.get_offset("fp", "/a.jsonl") == 60
        assert store.is_legacy_offset("fp", "/a.jsonl") is True
        store.clear_legacy_offset("fp", "/a.jsonl")
        assert store.is_legacy_offset("fp", "/a.jsonl") is False
        store.close()


class TestBackfillRecovery:
    def _collided_db(self, tmp_path):
        """events.db as the pre-#50 code left it after harvesting twin files."""
        base = tmp_path / "claude"
        a, b = _twin_files(base, age=3 * DAY)
        db = tmp_path / "moolmesh" / "events.db"
        store = EventStore(db_path=db)
        w = ClaudeWatcher(store, claude_base=base)
        fp = file_fingerprint(a)
        # Old behaviour: A read fully, then B resumed from A's offset.
        ev_a, off_a = w._parse_and_adapt(a, 0)
        store.store_with_offset(ev_a, "", "claude", str(a), 0)
        ev_b, off_b = w._parse_and_adapt(b, off_a)
        store.store_with_offset(ev_b, "", "claude", str(b), 0)
        expected_missing = len(_parse_fps(w, a, b) - _event_fps(db))
        store.close()
        _make_old_registry(db, [(fp, "claude", str(b), off_b, time.time())])
        return base, db, a, b, expected_missing

    def test_recovers_missing_events_once(self, tmp_path):
        base, db, a, b, expected = self._collided_db(tmp_path)
        assert expected > 0
        store = EventStore(db_path=db)  # migrates: the shared row becomes legacy
        rep = run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)
        r = rep.providers[0]
        assert r.fingerprint_collisions == 1
        assert r.collision_files == 2
        assert r.collision_recovered == expected
        w = ClaudeWatcher(store, claude_base=base)
        assert _event_fps(db) == _parse_fps(w, a, b)
        fp = file_fingerprint(a)
        assert store.get_offset(fp, a) == a.stat().st_size
        assert store.get_offset(fp, b) == b.stat().st_size

        second = run_backfill(store, providers=["claude"], bases={"claude": base}, db_path=db)
        r2 = second.providers[0]
        assert (r2.collision_files, r2.collision_recovered, r2.processed) == (0, 0, 0)
        assert r2.up_to_date == 2
        store.close()

    def test_dry_run_on_unmigrated_db_reports_and_writes_nothing(self, tmp_path):
        base, db, _a, _b, _expected = self._collided_db(tmp_path)
        before = db.read_bytes()
        rep = run_backfill(None, providers=["claude"], bases={"claude": base},
                           db_path=db, dry_run=True)
        r = rep.providers[0]
        assert r.collision_files == 2 and r.processed == 2
        assert db.read_bytes() == before
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 4").fetchone()[0] == 0
        conn.close()


def _opencode_db(path: Path, parts: int, start: int = 0) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, project_id TEXT,
            directory TEXT, title TEXT, model TEXT, cost REAL, time_updated TEXT);
        CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT,
            session_id TEXT, time_created TEXT, data TEXT);
        INSERT OR IGNORE INTO project VALUES ('p1', '/Users/test/app', 'app');
        INSERT OR IGNORE INTO session VALUES ('s1', 'p1', '/Users/test/app', 'T', '', 0, '');
        INSERT OR IGNORE INTO message VALUES ('m1', 's1', '2026-06-01T10:00:00',
            '{"role": "user"}');
    """)
    for i in range(start, start + parts):
        conn.execute(
            "INSERT INTO part VALUES (?, 'm1', 's1', ?, ?)",
            (f"pt{i}", f"2026-06-01T10:{i:02d}:00",
             json.dumps({"type": "text", "content": f"hello {i}"})),
        )
    conn.commit()
    conn.close()


class TestSqliteSourceStableKey:
    def test_restart_after_write_does_not_reread_from_zero(self, tmp_path):
        oc = tmp_path / "opencode.db"
        _opencode_db(oc, 3)
        db = tmp_path / "events.db"
        store = EventStore(db_path=db)
        w1 = OpenCodeWatcher(store, opencode_db=oc)
        w1._rescan()
        for path, key in list(w1._watched_files.items()):
            w1._harvest_file(path, key)
        fp_before = file_fingerprint(oc)
        store.close()

        _opencode_db(oc, 2, start=3)  # OpenCode writes; its SQLite header changes
        assert file_fingerprint(oc) != fp_before

        store2 = EventStore(db_path=db)
        w2 = OpenCodeWatcher(store2, opencode_db=oc)
        spy = _OffsetSpy(w2)
        w2._rescan()
        for path, key in list(w2._watched_files.items()):
            w2._harvest_file(path, key)
        assert spy.offsets == [3]  # resumed at the last rowid, not 0
        assert store2._conn.execute(
            "SELECT COUNT(*) FROM events WHERE provider = 'opencode'").fetchone()[0] == 5
        store2.close()

    def test_first_start_after_upgrade_seeds_from_fingerprint_rows(self, tmp_path):
        oc = tmp_path / "opencode.db"
        _opencode_db(oc, 5)
        db = tmp_path / "events.db"
        EventStore(db_path=db).close()
        # Pre-#50 registry: one content-fingerprint row per daemon start.
        _make_old_registry(db, [
            ("old-a", "opencode", str(oc), 2, 1000.0),
            ("old-b", "opencode", str(oc), 4, 2000.0),  # the latest start
        ])
        store = EventStore(db_path=db)
        w = OpenCodeWatcher(store, opencode_db=oc)
        spy = _OffsetSpy(w)
        w._rescan()
        (path, key), = w._watched_files.items()
        assert key == f"opencode:{registry_path(oc)}"
        w._harvest_file(path, key)
        assert spy.offsets == [4]
        assert store.get_offset(key, oc) == 5  # the stable row now exists
        store.close()
