"""Antigravity surfaces (#77): the CLI's data dir and the desktop app / IDE one.

Both keep ``brain/<uuid>/.system_generated/logs/transcript.jsonl`` plus a
``conversation_summaries.db`` whose ``app_data_dir`` names the surface. Every
base that exists is read; a conversation found in two is ingested once.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from hub.cache.event_store import EventStore
from hub.discovery import ProjectDiscovery
from hub.parsers.antigravity_parser import (
    default_antigravity_base,
    default_antigravity_bases,
    default_desktop_base,
)
from hub.watchers.antigravity_watcher import AntigravityWatcher
from tests.test_antigravity_parser import (
    CONV,
    CONV2,
    make_summaries_db,
    step,
    summary_row,
    write_transcript,
)


def _cli(tmp_path: Path) -> Path:
    return tmp_path / ".gemini" / "antigravity-cli"


def _desktop(tmp_path: Path) -> Path:
    return tmp_path / ".gemini" / "antigravity"


def _lines(text: str) -> list[dict]:
    return [step(0, "USER_INPUT", content=text), step(1, content="ok")]


def _ingest(tmp_path: Path, bases: list[Path]) -> EventStore:
    """One full pass of the watcher over ``bases`` (discover + harvest every file)."""
    store = EventStore(db_path=tmp_path / "events.db")
    w = AntigravityWatcher(store, None, antigravity_bases=bases)
    w._rescan()
    for path, fp in list(w._watched_files.items()):
        w._harvest_file(path, fp)
    return store


def _sessions(store: EventStore) -> dict[str, dict]:
    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute(
            "SELECT id, provider, cwd, title, metadata_json FROM sessions").fetchall()
    finally:
        conn.close()
    return {
        sid: {"provider": prov, "cwd": cwd, "title": title,
              "metadata": json.loads(meta) if meta else {}}
        for sid, prov, cwd, title, meta in rows
    }


class TestDefaultBases:
    def test_cli_first_then_desktop(self, monkeypatch):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        home = Path.home() / ".gemini"
        assert default_desktop_base() == home / "antigravity"
        assert default_antigravity_bases() == [home / "antigravity-cli", home / "antigravity"]

    def test_env_replaces_only_the_cli_base(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(tmp_path / "agy"))
        assert default_antigravity_base() == tmp_path / "agy"
        assert default_antigravity_bases() == [tmp_path / "agy", default_desktop_base()]

    def test_env_pointing_at_the_desktop_dir_is_not_read_twice(self, monkeypatch):
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(default_desktop_base()))
        assert default_antigravity_bases() == [default_desktop_base()]

    def test_watcher_and_discovery_default_to_every_surface(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(_cli(tmp_path)))
        a = write_transcript(_cli(tmp_path), _lines("cli"), conv=CONV)
        b = write_transcript(default_desktop_base(), _lines("desktop"), conv=CONV2)
        try:
            w = AntigravityWatcher(EventStore(db_path=tmp_path / "e.db"), None)
            assert set(w.discover_files()) == {a, b}
            files = {f for p in ProjectDiscovery().discover_antigravity() for f in p.session_files}
            assert files == {a, b}
        finally:
            shutil.rmtree(default_desktop_base())


class TestSurfaces:
    def test_cli_only(self, tmp_path):
        cli = _cli(tmp_path)
        write_transcript(cli, _lines("cli"), conv=CONV)
        make_summaries_db(cli, [summary_row(CONV, title="Acme task",
                                            workspace="file:///Users/u/work/acme/web")]).close()
        store = _ingest(tmp_path, [cli, _desktop(tmp_path)])
        s = _sessions(store)
        assert set(s) == {CONV}
        assert s[CONV]["provider"] == "antigravity"
        assert s[CONV]["cwd"] == "/Users/u/work/acme/web"
        assert s[CONV]["title"] == "Acme task"
        assert s[CONV]["metadata"]["surface"] == "antigravity-cli"
        assert len(store.get_session_events(CONV)) == 2
        store.close()

    def test_desktop_only(self, tmp_path):
        desk = _desktop(tmp_path)
        write_transcript(desk, _lines("desktop"), conv=CONV2)
        make_summaries_db(desk, [
            summary_row(CONV2, title="Globex task", workspace="file:///Users/u/work/globex/api",
                        app_dir="antigravity"),
        ]).close()
        store = _ingest(tmp_path, [_cli(tmp_path), desk])
        s = _sessions(store)
        assert set(s) == {CONV2}
        assert s[CONV2]["cwd"] == "/Users/u/work/globex/api"
        assert s[CONV2]["title"] == "Globex task"
        assert s[CONV2]["metadata"]["surface"] == "antigravity"
        store.close()

    def test_both_with_different_conversations(self, tmp_path):
        cli, desk = _cli(tmp_path), _desktop(tmp_path)
        write_transcript(cli, _lines("cli"), conv=CONV)
        write_transcript(desk, _lines("desktop"), conv=CONV2)
        # The index is shared: each base keeps only its own surface's rows.
        rows = [
            summary_row(CONV, title="Acme task", workspace="file:///Users/u/work/acme/web"),
            summary_row(CONV2, title="Globex task", workspace="file:///Users/u/work/globex/api",
                        app_dir="antigravity"),
        ]
        make_summaries_db(cli, rows).close()
        make_summaries_db(desk, rows).close()
        store = _ingest(tmp_path, [cli, desk])
        s = _sessions(store)
        assert set(s) == {CONV, CONV2}
        assert (s[CONV]["title"], s[CONV]["metadata"]["surface"]) == ("Acme task", "antigravity-cli")
        assert (s[CONV2]["title"], s[CONV2]["metadata"]["surface"]) == ("Globex task", "antigravity")
        names = {p.name for p in ProjectDiscovery(antigravity_bases=[cli, desk]).discover_antigravity()}
        assert names == {"acme/web", "globex/api"}
        store.close()

    def test_same_conversation_in_both_is_ingested_once(self, tmp_path):
        cli, desk = _cli(tmp_path), _desktop(tmp_path)
        a = write_transcript(cli, _lines("same"), conv=CONV)
        b = write_transcript(desk, _lines("same"), conv=CONV)
        make_summaries_db(cli, [summary_row(CONV, title="From CLI")]).close()
        make_summaries_db(desk, [summary_row(CONV, title="From desktop", app_dir="antigravity")]).close()

        files = [f for p in ProjectDiscovery(antigravity_bases=[cli, desk]).discover_antigravity()
                 for f in p.session_files]
        assert files == [a]  # the first base wins
        w = AntigravityWatcher(EventStore(db_path=tmp_path / "x.db"), None,
                               antigravity_bases=[cli, desk])
        assert w.discover_files() == [a]
        assert b not in w.discover_files()

        store = _ingest(tmp_path, [cli, desk])
        s = _sessions(store)
        assert set(s) == {CONV}
        assert s[CONV]["title"] == "From CLI"
        assert s[CONV]["metadata"]["surface"] == "antigravity-cli"
        assert len(store.get_session_events(CONV)) == 2  # no duplicated events
        store.close()

    def test_missing_bases_are_ignored(self, tmp_path, caplog):
        cli = _cli(tmp_path)
        write_transcript(cli, _lines("cli"), conv=CONV)
        nope = tmp_path / "nope" / "antigravity"
        with caplog.at_level("DEBUG", logger="hub"):
            store = _ingest(tmp_path, [cli, nope])
            assert ProjectDiscovery(antigravity_bases=[nope]).discover_antigravity() == []
            w = AntigravityWatcher(store, None, antigravity_bases=[nope, tmp_path / "x"])
            assert w.discover_files() == []
        assert set(_sessions(store)) == {CONV}
        assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
        store.close()

    def test_an_unreadable_index_in_one_base_keeps_its_last_good_rows(self, tmp_path):
        cli, desk = _cli(tmp_path), _desktop(tmp_path)
        write_transcript(cli, _lines("cli"), conv=CONV)
        write_transcript(desk, _lines("desktop"), conv=CONV2)
        make_summaries_db(cli, [summary_row(CONV, title="Acme task")]).close()
        make_summaries_db(desk, [summary_row(CONV2, title="Globex task", app_dir="antigravity")]).close()
        w = AntigravityWatcher(EventStore(db_path=tmp_path / "x.db"), None,
                               antigravity_bases=[cli, desk])
        w.discover_files()
        (desk / "conversation_summaries.db").write_bytes(b"not a database")
        w.discover_files()
        assert w._summaries[CONV].title == "Acme task"
        assert w._summaries[CONV2].title == "Globex task"
