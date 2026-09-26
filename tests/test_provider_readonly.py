"""Provider databases are opened read-only — issue #51.

MoolMesh observes Codex ``state_5.sqlite``, ``opencode.db`` and Cursor's
``state.vscdb``; it must never write them, take write locks on them, or fall
back to a read-write connection when a read-only open fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hub.discovery import ProjectDiscovery
from hub.parsers.cursor_parser import CursorParser
from hub.parsers.opencode_parser import OpenCodeParser
from hub.sqlite_ro import connect_ro, ro_uri


def _opencode_db(path: Path, wal: bool = False) -> sqlite3.Connection:
    """Minimal OpenCode schema + one part; returns the (open) owner connection."""
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")  # keep frames in -wal
    conn.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT,
            title TEXT, model TEXT, cost REAL, time_updated TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created TEXT, data TEXT);
        INSERT INTO project VALUES ('p1', '/Users/test/app', 'app');
        INSERT INTO session VALUES ('s1', 'p1', '/Users/test/app', 'T', '', 0, 't');
        INSERT INTO message VALUES ('m1', 's1', '2026-06-01T10:00:00', '{"role": "user"}');
    """)
    conn.execute(
        "INSERT INTO part VALUES ('pt1', 'm1', 's1', '2026-06-01T10:00:01', ?)",
        (json.dumps({"type": "text", "content": "hello"}),),
    )
    conn.commit()
    return conn


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestConnectRo:
    def test_uri_is_read_only_and_percent_encoded(self, tmp_path):
        uri = ro_uri(tmp_path / "odd name?#.db")
        assert uri.startswith("file:") and uri.endswith("?mode=ro")
        assert "%3F" in uri and "%23" in uri and " " not in uri

    def test_rollback_db_rejects_writes_and_creates_no_files(self, tmp_path):
        db = tmp_path / "opencode.db"
        _opencode_db(db).close()
        before = sorted(os.listdir(tmp_path))
        digest = _sha(db)

        conn = connect_ro(db)
        assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO part (id) VALUES ('x')")
        conn.close()

        entries, offset = OpenCodeParser().parse_incremental(db, 0)
        assert len(entries) == 1 and offset == 1
        assert OpenCodeParser.can_parse(db)
        assert [p.path for p in ProjectDiscovery(opencode_base=db).discover_opencode()] == [
            "/Users/test/app"
        ]
        assert sorted(os.listdir(tmp_path)) == before  # no -wal / -shm / -journal
        assert _sha(db) == digest

    def test_wal_db_read_never_checkpoints_into_main_file(self, tmp_path):
        """ro reads see WAL frames but never write the provider's main DB.

        Whether SQLite creates ``-wal``/``-shm`` for a reader depends on its
        version and on the owner (see ``hub/sqlite_ro``), so this asserts
        the invariant that matters: the main file's bytes don't change and a
        write through the connection is rejected.
        """
        db = tmp_path / "opencode.db"
        owner = _opencode_db(db, wal=True)
        owner.execute(
            "INSERT INTO part VALUES ('pt2', 'm1', 's1', '2026-06-01T10:00:02', ?)",
            (json.dumps({"type": "text", "content": "pending in wal"}),),
        )
        owner.commit()
        assert (tmp_path / "opencode.db-wal").stat().st_size > 0
        digest = _sha(db)

        entries, offset = OpenCodeParser().parse_incremental(db, 0)
        assert [e.text for e in entries] == ["hello", "pending in wal"]
        conn = connect_ro(db)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM part")
        conn.close()
        assert _sha(db) == digest
        owner.close()


class _RecordingConnect:
    """Stands in for ``sqlite3.connect``: records every open, then fails it."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, database, *args, **kwargs):
        self.calls.append((str(database), kwargs))
        raise sqlite3.OperationalError("unable to open database file")


class TestNeverFallsBackToReadWrite:
    def test_failed_ro_open_is_skipped_not_retried(self, tmp_path, monkeypatch, caplog):
        oc = tmp_path / "opencode.db"
        _opencode_db(oc).close()
        codex = tmp_path / ".codex"
        (codex / "sessions").mkdir(parents=True)
        _opencode_db(codex / "state_5.sqlite").close()
        cursor = tmp_path / "cursor"
        (cursor / "globalStorage").mkdir(parents=True)
        vscdb = cursor / "globalStorage" / "state.vscdb"
        c = sqlite3.connect(vscdb)
        c.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        c.commit()
        c.close()

        caplog.set_level("WARNING", logger="hub.OpenCodeParser")  # conftest mutes hub.*
        rec = _RecordingConnect()
        monkeypatch.setattr(sqlite3, "connect", rec)

        parser = OpenCodeParser()
        assert parser.parse_incremental(oc, 7) == ([], 7)
        assert parser.parse_incremental(oc, 7) == ([], 7)
        assert parser.parse_file(oc) == []
        assert parser.parse_session(oc, "s1") == []
        assert OpenCodeParser.can_parse(oc) is False
        assert ProjectDiscovery(opencode_base=oc).discover_opencode() == []
        ProjectDiscovery(codex_base=codex).discover_codex()
        assert CursorParser(cursor_base=cursor).parse_incremental(vscdb, 3) == ([], 3)
        assert CursorParser.can_parse(vscdb) is False

        assert len(rec.calls) == 9  # one open per read: no read-write retry
        for database, kwargs in rec.calls:
            assert database.startswith("file:") and database.endswith("?mode=ro")
            assert kwargs.get("uri") is True
        # the polling read logs once when it starts failing, not every cycle
        assert sum("se saltea hasta" in r.getMessage() for r in caplog.records) == 1

    def test_parser_logs_recovery_once(self, tmp_path, monkeypatch, caplog):
        oc = tmp_path / "opencode.db"
        _opencode_db(oc).close()
        parser = OpenCodeParser()
        real = sqlite3.connect
        monkeypatch.setattr(sqlite3, "connect", _RecordingConnect())
        parser.parse_incremental(oc, 0)
        monkeypatch.setattr(sqlite3, "connect", real)
        caplog.set_level("INFO", logger="hub.OpenCodeParser")
        entries, _ = parser.parse_incremental(oc, 0)
        assert len(entries) == 1
        assert sum("vuelve a leerse" in r.getMessage() for r in caplog.records) == 1
