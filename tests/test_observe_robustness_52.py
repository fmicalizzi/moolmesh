"""Observe robustness (issue #52): locked DB, streaming reads, full-text cap,
symlinked ~/.codex, live cloud placeholders, backup retention."""

from __future__ import annotations

import collections
import json
import os
import sqlite3
import time
import tracemalloc
from pathlib import Path

import pytest

from hub.backups import BACKUP_KEEP, prune_backups
from hub.cache.event_store import EventStore, cap_full_text
from hub.discovery import ProjectDiscovery
from hub.parsers.base import iter_complete_lines
from hub.parsers.claude_parser import ClaudeParser
from hub.watchers.base import BaseHarvester


def _event(n: int = 0, **extra) -> dict:
    ev = {
        "provider": "fake", "project": "p", "event_type": "user",
        "timestamp": f"2026-01-01T00:00:{n:02d}Z",
        "summary": f"event {n}", "session_id": "s1",
    }
    ev.update(extra)
    return ev


class _OneFileHarvester(BaseHarvester):
    POLL_INTERVAL = 0.01
    RESCAN_INTERVAL = 0.05

    def __init__(self, store, files):
        super().__init__(store, collections.deque())
        self._files = list(files)
        self.opened: list[Path] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    def discover_files(self, since=None, skip_dir=None) -> list[Path]:
        return list(self._files)

    def _parse_and_adapt(self, path: Path, offset: int):
        self.opened.append(path)
        if offset == 0:
            return [_event()], 10
        return [], offset


# ── (1) database is locked ─────────────────────────────────────────────


class TestDatabaseLocked:
    def test_locked_store_is_retried_next_cycle(self, tmp_path, monkeypatch):
        """A 'database is locked' on the store never kills the watcher."""
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            f = tmp_path / "a.jsonl"
            f.write_text("x\n")
            w = _OneFileHarvester(store, [f])
            real = store.store_with_offset
            calls = {"n": 0}

            def locked_twice(*a, **kw):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise sqlite3.OperationalError("database is locked")
                return real(*a, **kw)

            monkeypatch.setattr(store, "store_with_offset", locked_twice)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and store.count() == 0:
                time.sleep(0.02)
            alive = w.alive
            w.stop()
            assert alive
            assert calls["n"] >= 3  # retried on later cycles
            assert store.count() == 1
            assert w.last_error is None  # cleared once the file recovered
        finally:
            store.close()

    def test_locked_offset_read_is_recorded_not_raised(self, tmp_path, monkeypatch):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            f = tmp_path / "a.jsonl"
            f.write_text("x\n")
            w = _OneFileHarvester(store, [f])

            def locked(*a, **kw):
                raise sqlite3.OperationalError("database is locked")

            monkeypatch.setattr(store, "get_offset", locked)
            w._harvest_file(f, w.registry_key(f))  # must not raise
            assert w.last_error == {"type": "OperationalError", "file": str(f)}
        finally:
            store.close()


# ── (2) streaming parse_incremental ────────────────────────────────────


class TestStreamingRead:
    def test_partial_tail_is_left_for_next_call(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_bytes(b'{"a": 1}\n\n{"b": 2}\n{"c": ')
        got = list(iter_complete_lines(p, 0))
        assert [line for line, _ in got] == ['{"a": 1}', "", '{"b": 2}']
        assert got[-1][1] == len(b'{"a": 1}\n\n{"b": 2}\n')

    def test_offset_past_end_restarts_at_zero(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_bytes(b"partial")
        assert list(iter_complete_lines(p, 500)) == [("", 0)]
        _entries, new = ClaudeParser().parse_incremental(p, 500)
        assert new == 0

    def test_line_separator_inside_json_string_is_not_a_line_break(self, tmp_path):
        """Only \\n ends a line (old splitlines() also split on U+2028)."""
        p = tmp_path / "s.jsonl"
        line = json.dumps({"text": "a b"}, ensure_ascii=False)
        p.write_text(line + "\n", encoding="utf-8")
        got = list(iter_complete_lines(p, 0))
        assert len(got) == 1 and json.loads(got[0][0])["text"] == "a b"

    def test_memory_is_bounded_by_the_line_not_the_file(self, tmp_path):
        """A 16 MB file of skippable lines parses in far less than 16 MB."""
        p = tmp_path / "big.jsonl"
        junk = b"x" * 1023 + b"\n"  # not JSON: parsed and dropped
        with open(p, "wb") as f:
            for _ in range(16 * 1024):
                f.write(junk)
        size = p.stat().st_size
        tracemalloc.start()
        try:
            entries, new_offset = ClaudeParser().parse_incremental(p, 0)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert entries == [] and new_offset == size
        assert peak < size // 8, f"peak {peak} for a {size} byte file"

    def test_resumes_from_offset(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_bytes(b"one\ntwo\n")
        got = list(iter_complete_lines(p, 4))
        assert got == [("two", 8)]


# ── (3) full-text cap ──────────────────────────────────────────────────


class TestFullTextCap:
    def test_short_text_untouched(self):
        assert cap_full_text("hola", 100) == "hola"
        assert cap_full_text("x" * 500, 0) == "x" * 500  # 0 = no cap

    def test_keeps_head_and_tail(self):
        text = "HEAD" + "m" * 10_000 + "TAIL"
        capped = cap_full_text(text, 1000)
        assert len(capped) <= 1000
        assert capped.startswith("HEAD") and capped.endswith("TAIL")
        assert "caracteres omitidos" in capped
        omitted = len(text) - (len(capped) - len(capped[capped.index("\n\n["):
                                                   capped.index("]\n\n") + 3]))
        assert f"{omitted} caracteres" in capped

    def test_deterministic(self):
        text = "a" * 5000 + "b" * 5000
        assert cap_full_text(text, 700) == cap_full_text(text, 700)

    def test_store_caps_every_insert_path(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db", full_text_max_chars=200)
        try:
            big = "H" * 50 + "z" * 5000 + "T" * 50
            store.store(_event(1, full_text=big))
            store.store_batch([_event(2, full_text=big)])
            store.store_with_offset([_event(3, full_text=big)], "fp", "fake", "/x", 1)
            store.replace_session_events("fake", ["s2"], [_event(4, session_id="s2",
                                                                 full_text=big)], [])
            rows = store._conn.execute("SELECT full_text FROM event_content").fetchall()
            assert len(rows) == 4
            for (text,) in rows:
                assert len(text) <= 200
                assert text.startswith("H") and text.endswith("T")
        finally:
            store.close()

    def test_cap_from_config(self, tmp_path, monkeypatch):
        import hub.config as config
        cfg = tmp_path / "config.toml"
        cfg.write_text("[daemon]\nfull_text_max_chars = 1234\n")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg)
        assert config.load_config().full_text_max_chars == 1234
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            assert store.full_text_max_chars == 1234
        finally:
            store.close()
        out = config._serialize_toml(config.load_config())
        assert "full_text_max_chars = 1234" in out

    def test_default_and_invalid_config(self, tmp_path, monkeypatch):
        import hub.config as config
        cfg = tmp_path / "config.toml"
        cfg.write_text('[daemon]\nfull_text_max_chars = "mucho"\n')
        monkeypatch.setattr(config, "CONFIG_PATH", cfg)
        assert config.load_config().full_text_max_chars == config.DEFAULT_FULL_TEXT_MAX_CHARS
        assert "full_text_max_chars" not in config._serialize_toml(config.HubConfig())


# ── (4) symlinked ~/.codex ─────────────────────────────────────────────


def _codex_tree(real: Path, rows_base: Path) -> None:
    """Rollouts under ``real``; state_5.sqlite paths spelled under ``rows_base``."""
    day = real / "sessions" / "2026" / "04" / "01"
    day.mkdir(parents=True)
    (day / "rollout-work.jsonl").write_text("{}\n")
    (day / "rollout-noise.jsonl").write_text("{}\n")
    rows_day = rows_base / "sessions" / "2026" / "04" / "01"
    conn = sqlite3.connect(str(real / "state_5.sqlite"))
    conn.execute("CREATE TABLE threads (rollout_path TEXT, cwd TEXT,"
                 " tokens_used INTEGER, source TEXT)")
    conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                 (str(rows_day / "rollout-work.jsonl"), "/Users/u/work/widget-services",
                  900, "cli"))
    conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                 (str(rows_day / "rollout-noise.jsonl"), "/", 0, "exec"))
    conn.commit()
    conn.close()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
class TestSymlinkedCodex:
    @pytest.mark.parametrize("walk_link,rows_link", [(True, False), (False, True)])
    def test_filter_survives_a_symlink_on_either_side(self, tmp_path, walk_link, rows_link):
        real = tmp_path / "real-codex"
        link = tmp_path / "link-codex"
        real.mkdir()
        link.symlink_to(real, target_is_directory=True)
        _codex_tree(real, link if rows_link else real)
        result = ProjectDiscovery(codex_base=link if walk_link else real).discover_codex()
        files = [f.name for proj in result for f in proj.session_files]
        assert files == ["rollout-work.jsonl"]  # the exec noise stays out
        assert [p.name for p in result] == ["widget-services"]


# ── (5) cloud placeholders in the live watcher ─────────────────────────


class TestLivePlaceholders:
    def test_placeholder_file_is_never_registered_or_read(self, tmp_path):
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            normal = tmp_path / "a.jsonl"
            normal.write_text("x\n")
            stub = tmp_path / ".b.jsonl.icloud"
            stub.write_text("")
            w = _OneFileHarvester(store, [normal, stub])
            w._rescan()
            assert set(w._watched_files) == {normal}
        finally:
            store.close()

    def test_file_turning_placeholder_is_dropped_without_a_last_read(
        self, tmp_path, monkeypatch
    ):
        import hub.cloudfiles as cloudfiles
        store = EventStore(db_path=tmp_path / "events.db")
        try:
            f = tmp_path / "a.jsonl"
            f.write_text("x\n")
            w = _OneFileHarvester(store, [f])
            w._rescan()
            assert f in w._watched_files
            monkeypatch.setattr(cloudfiles, "is_cloud_placeholder", lambda p: Path(p) == f)
            w.opened.clear()
            w._rescan()
            assert f not in w._watched_files
            assert w.opened == []  # evicted to the cloud: never opened
        finally:
            store.close()


# ── (6) backup retention ───────────────────────────────────────────────


class TestBackupPruning:
    def _make(self, d: Path, name: str, age: float) -> Path:
        p = d / name
        p.write_bytes(b"db")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_keeps_newest_n_per_kind(self, tmp_path):
        events = [self._make(tmp_path, f"events-20260101-0000{i:02d}.db", 100 - i)
                  for i in range(8)]
        ws = [self._make(tmp_path, f"workspace-20260101-0000{i:02d}.db", 100 - i)
              for i in range(3)]
        mine = self._make(tmp_path, "events-mine.db", 1000)
        removed = prune_backups(tmp_path, "events", keep=5)
        assert sorted(removed) == sorted(events[:3])
        assert all(p.exists() for p in events[3:])
        assert all(p.exists() for p in ws)
        assert mine.exists()  # not a MoolMesh backup name: never touched

    def test_never_removes_the_fresh_backup(self, tmp_path):
        fresh = self._make(tmp_path, "events-20260101-000000-1.db", 10_000)  # odd mtime
        others = [self._make(tmp_path, f"events-20260102-0000{i:02d}.db", 10 - i)
                  for i in range(4)]
        prune_backups(tmp_path, "events", keep=2, protect=fresh)
        assert fresh.exists()
        assert [p.exists() for p in others] == [False, False, False, True]

    def test_backup_events_db_prunes(self, tmp_path):
        from hub.backfill import backup_events_db
        db = tmp_path / "events.db"
        EventStore(db_path=db).close()
        bdir = tmp_path / "backups"
        bdir.mkdir()
        for i in range(BACKUP_KEEP + 3):
            self._make(bdir, f"events-20200101-0000{i:02d}.db", 1000 - i)
        dest = backup_events_db(db, bdir)
        left = sorted(p.name for p in bdir.glob("events-*.db"))
        assert dest.exists() and dest.name in left
        assert len(left) == BACKUP_KEEP

    def test_backup_workspace_db_prunes(self, tmp_path):
        from hub.cache.workspace_store import backup_workspace_db
        bdir = tmp_path / "backups"
        bdir.mkdir()
        for i in range(BACKUP_KEEP + 2):
            self._make(bdir, f"workspace-20200101-0000{i:02d}.db", 1000 - i)
        conn = sqlite3.connect(":memory:")
        dest = backup_workspace_db(conn, tmp_path / "workspace.db", bdir)
        conn.close()
        assert dest.exists()
        assert len(list(bdir.glob("workspace-*.db"))) == BACKUP_KEEP
