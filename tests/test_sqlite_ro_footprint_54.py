"""Zero footprint when reading a provider's WAL database (issue #54).

After a read, the provider's directory holds no new ``-wal`` / ``-shm``;
with the owner running, its uncheckpointed WAL frames are still visible.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hub.discovery import ProjectDiscovery
from hub.parsers.opencode_parser import OpenCodeParser
from hub.sqlite_ro import connect_ro, is_closed_wal_db, zero_footprint_uri


def _wal_db(path: Path, rows: int = 3) -> sqlite3.Connection:
    """A WAL-mode DB with ``rows`` rows; returned OPEN (close = owner exits)."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def _count(path: Path) -> int:
    conn = connect_ro(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


class TestZeroFootprint:
    def test_closed_wal_db_read_leaves_nothing(self, tmp_path):
        db = tmp_path / "provider.db"
        _wal_db(db).close()
        before = sorted(os.listdir(tmp_path))
        assert before == ["provider.db"]
        assert is_closed_wal_db(db)
        assert zero_footprint_uri(db).endswith("?mode=ro&immutable=1")
        assert _count(db) == 3
        assert sorted(os.listdir(tmp_path)) == before  # no -wal, no -shm

    def test_owner_running_sees_wal_frames(self, tmp_path):
        db = tmp_path / "provider.db"
        owner = _wal_db(db)
        try:
            owner.execute("PRAGMA wal_autocheckpoint=0")
            owner.execute("INSERT INTO t VALUES (99)")
            owner.commit()
            assert (tmp_path / "provider.db-wal").stat().st_size > 0
            assert not is_closed_wal_db(db)
            assert zero_footprint_uri(db).endswith("?mode=ro")
            assert _count(db) == 4  # the uncheckpointed row is visible
        finally:
            owner.close()

    def test_leftover_shm_alone_disables_immutable(self, tmp_path):
        db = tmp_path / "provider.db"
        _wal_db(db).close()
        (tmp_path / "provider.db-shm").write_bytes(b"")
        assert not is_closed_wal_db(db)

    def test_rollback_journal_db_is_never_immutable(self, tmp_path):
        db = tmp_path / "plain.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        assert not is_closed_wal_db(db)
        assert _count(db) == 1
        assert sorted(os.listdir(tmp_path)) == ["plain.db"]

    def test_not_a_database(self, tmp_path):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"hello")
        assert not is_closed_wal_db(junk)
        assert not is_closed_wal_db(tmp_path / "missing.db")

    @pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                        reason="POSIX permissions, non-root")
    def test_read_only_directory_is_now_readable(self, tmp_path):
        d = tmp_path / "ro"
        d.mkdir()
        db = d / "provider.db"
        _wal_db(db).close()
        d.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            assert _count(db) == 3
        finally:
            d.chmod(stat.S_IRWXU)


class TestProvidersLeaveNoFootprint:
    def test_codex_state_db(self, tmp_path):
        day = tmp_path / "sessions" / "2026" / "04" / "01"
        day.mkdir(parents=True)
        rollout = day / "rollout-a.jsonl"
        rollout.write_text("{}\n")
        conn = sqlite3.connect(str(tmp_path / "state_5.sqlite"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE threads (rollout_path TEXT, cwd TEXT,"
                     " tokens_used INTEGER, source TEXT)")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                     (str(rollout), "/Users/u/work/widget-services", 10, "cli"))
        conn.commit()
        conn.close()
        before = sorted(os.listdir(tmp_path))
        result = ProjectDiscovery(codex_base=tmp_path).discover_codex()
        assert [p.name for p in result] == ["widget-services"]
        assert sorted(os.listdir(tmp_path)) == before

    def test_opencode_db(self, tmp_path):
        db = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                     " time_created INTEGER, data TEXT)")
        conn.commit()
        conn.close()
        before = sorted(os.listdir(tmp_path))
        OpenCodeParser().parse_incremental(db, 0)
        assert sorted(os.listdir(tmp_path)) == before
