"""Tests for the Antigravity CLI watcher and discovery (window, grouping, ingest, resilience)."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hub.cache.event_store import EventStore, registry_path
from hub.discovery import ProjectDiscovery
from hub.models.base import Provider
from hub.watchers.antigravity_watcher import AntigravityWatcher
from tests.test_antigravity_parser import (
    CONV,
    CONV2,
    CONV3,
    LOGGER,
    append,
    fixture_lines,
    make_summaries_db,
    step,
    summary_row,
    transcript_path,
    write_transcript,
)

OLD = 48 * 3600  # older than the daemon's live window


def _store(tmp_path: Path) -> EventStore:
    return EventStore(db_path=tmp_path / "events.db")


def _base(tmp_path: Path) -> Path:
    return tmp_path / ".gemini" / "antigravity-cli"


def _events_by_type(store: EventStore, session: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in store.get_session_events(session):
        out.setdefault(e["event_type"], []).append(e)
    return out


# ── watcher basics ───────────────────────────────────────────────


class TestIdentity:
    def test_provider_name_flags_and_registry_key(self, tmp_path):
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=_base(tmp_path))
        assert w.provider_name == "antigravity"
        assert w.CATCHUP is True and w.STABLE_KEY is True
        path = transcript_path(_base(tmp_path))
        assert w.registry_key(path) == f"antigravity:{registry_path(path)}"

    def test_base_follows_the_environment(self, tmp_path, monkeypatch):
        w = AntigravityWatcher(_store(tmp_path), None)
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(tmp_path / "elsewhere"))
        f = write_transcript(tmp_path / "elsewhere", fixture_lines())
        assert w.discover_files() == [f]


# ── discovery ────────────────────────────────────────────────────


class TestDiscovery:
    def test_window_and_since(self, tmp_path):
        base = _base(tmp_path)
        fresh = write_transcript(base, fixture_lines(), conv=CONV)
        write_transcript(base, fixture_lines(), conv=CONV2, age=OLD)
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert w.discover_files() == [fresh]
        assert len(w.discover_files(since=0.0)) == 2

    def test_project_label_from_the_summaries_workspace(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, [step(0, "USER_INPUT", content="hi")])  # no run_command Cwd
        make_summaries_db(base, [summary_row(CONV, workspace="file:///Users/u/work/globex/widget")]).close()
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert w.discover_files() == [f]
        assert w._file_projects[f] == "globex/widget"

    def test_project_label_falls_back_to_the_first_cwd_then_a_bucket(self, tmp_path):
        base = _base(tmp_path)
        with_cwd = write_transcript(base, fixture_lines(), conv=CONV)
        nothing = write_transcript(base, [step(0, "USER_INPUT", content="hi")], conv=CONV2)
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert set(w.discover_files()) == {with_cwd, nothing}
        assert w._file_projects[with_cwd] == "proj"
        assert w._file_projects[nothing] == ProjectDiscovery.short_cwd(str(base / "brain"))

    def test_project_filter(self, tmp_path):
        base = _base(tmp_path)
        write_transcript(base, [step(0, "USER_INPUT", content="a")], conv=CONV)
        write_transcript(base, [step(0, "USER_INPUT", content="b")], conv=CONV2)
        make_summaries_db(base, [
            summary_row(CONV, workspace="file:///Users/u/work/acme/web"),
            summary_row(CONV2, workspace="file:///Users/u/work/globex/api"),
        ]).close()
        w = AntigravityWatcher(_store(tmp_path), None, project_filter="acme", antigravity_base=base)
        assert [p.parents[2].name for p in w.discover_files()] == [CONV]

    def test_only_transcripts_are_listed(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        (f.parent / "transcript_full.jsonl").write_text(f.read_text())
        (base / "presence").mkdir(parents=True)
        (base / "presence" / f"{CONV}.lock").write_text("")
        (base / "conversations").mkdir()
        (base / "conversations" / f"{CONV}.db").write_bytes(b"SQLite format 3\0 not really")
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert w.discover_files() == [f]

    def test_other_surfaces_rows_do_not_make_sessions(self, tmp_path):
        base = _base(tmp_path)
        make_summaries_db(base, [
            summary_row(CONV2, workspace="file:///Users/u/work/desktop-only", app_dir="antigravity"),
        ]).close()
        assert AntigravityWatcher(_store(tmp_path), None, antigravity_base=base).discover_files() == []
        # and a transcript whose id only the desktop app knows takes no workspace from it
        f = write_transcript(base, fixture_lines(), conv=CONV2)
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert w.discover_files() == [f]
        assert w._file_projects[f] == "proj"  # the transcript's own Cwd, not the desktop workspace

    def test_subagents_are_ordinary_conversations(self, tmp_path):
        base = _base(tmp_path)
        a = write_transcript(base, fixture_lines(), conv=CONV)
        b = write_transcript(base, fixture_lines(), conv=CONV3)
        make_summaries_db(base, [summary_row(CONV), summary_row(CONV3, parent=CONV, depth=1)]).close()
        assert set(AntigravityWatcher(_store(tmp_path), None, antigravity_base=base).discover_files()) == {a, b}


class TestProjectDiscovery:
    def test_groups_by_workspace_and_keeps_the_rest_in_a_bucket(self, tmp_path):
        base = _base(tmp_path)
        a = write_transcript(base, [step(0, "USER_INPUT", content="a")], conv=CONV)
        b = write_transcript(base, [step(0, "USER_INPUT", content="b")], conv=CONV2)
        c = write_transcript(base, [step(0, "USER_INPUT", content="c")], conv=CONV3)
        make_summaries_db(base, [
            summary_row(CONV, workspace="file:///Users/u/work/acme/web"),
            summary_row(CONV2, workspace="file:///Users/u/work/acme/web"),
        ]).close()
        projects = ProjectDiscovery(antigravity_base=base).discover_antigravity()
        by_name = {p.name: p for p in projects}
        assert set(by_name) == {"acme/web", "antigravity-sessions"}
        assert by_name["acme/web"].session_files == sorted([a, b])
        assert by_name["acme/web"].path == "/Users/u/work/acme/web"
        assert by_name["antigravity-sessions"].session_files == [c]
        assert all(p.provider is Provider.ANTIGRAVITY for p in projects)
        assert any(p.provider is Provider.ANTIGRAVITY
                   for p in ProjectDiscovery(antigravity_base=base).discover_all())

    def test_skip_dir_is_asked_before_a_directory_is_listed(self, tmp_path):
        base = _base(tmp_path)
        keep = write_transcript(base, fixture_lines(), conv=CONV)
        write_transcript(base, fixture_lines(), conv=CONV2)
        asked: list[Path] = []
        cloud_only = base / "brain" / CONV2

        def skip(p: Path) -> bool:
            asked.append(p)
            return p == cloud_only

        projects = ProjectDiscovery(antigravity_base=base, skip_dir=skip).discover_antigravity()
        assert [f for p in projects for f in p.session_files] == [keep]
        assert base / "brain" in asked and cloud_only in asked
        # nothing under the skipped directory was asked about or touched
        assert not any(cloud_only in q.parents for q in asked)

        assert ProjectDiscovery(antigravity_base=base, skip_dir=lambda p: p == base / "brain"
                                ).discover_antigravity() == []

    def test_missing_dirs_are_not_an_error(self, tmp_path):
        assert ProjectDiscovery(antigravity_base=tmp_path / "nope").discover_antigravity() == []
        (tmp_path / "empty" / "brain").mkdir(parents=True)
        assert ProjectDiscovery(antigravity_base=tmp_path / "empty").discover_antigravity() == []

    def test_find_active_sessions_uses_the_conversation_id(self, tmp_path):
        base = _base(tmp_path)
        write_transcript(base, fixture_lines(), conv=CONV)
        sessions = ProjectDiscovery(antigravity_base=base).find_active_sessions(
            provider=Provider.ANTIGRAVITY)
        assert [s.session_id for s in sessions] == [CONV]  # not "transcript"


# ── ingest ───────────────────────────────────────────────────────


class TestParseAndAdapt:
    def test_fixture_becomes_the_expected_events_and_session(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        assert offset == f.stat().st_size
        assert len(events) == 14
        assert all(e["provider"] == "antigravity" and e["session_id"] == CONV for e in events)

        w._store.store_with_offset(events, w.registry_key(f), "antigravity", str(f), offset)
        by = _events_by_type(store, CONV)
        assert {k: len(v) for k, v in by.items()} == {
            "user": 2, "assistant": 2, "thinking": 1, "tool_use": 3, "tool_result": 4, "system": 2}
        paths = sorted(e["file_path"] for e in by["tool_use"] if e["file_path"])
        assert paths == ["/Users/u/work/proj/src/app.py", "/Users/u/work/proj/src/greeting.py"]
        run = [e for e in by["tool_use"] if e["tool_name"] == "run_command"]
        assert run and run[0]["file_path"] is None  # a shell command is never a path
        assert all(e["tool_name"] is None for e in by["tool_result"])
        assert not any(e.get("tokens") for e in store.get_session_events(CONV))

        detail = store.get_session_detail(CONV)
        assert detail["provider"] == "antigravity"
        assert detail["cwd"] == "/Users/u/work/proj"
        assert detail["project"] == "proj"
        assert detail["title"] == "Please add a greeting helper to the widget service."
        assert detail["is_sidechain"] is False
        store.close()

    def test_a_repeated_conversation_stores_nothing_extra(self, tmp_path):
        base = _base(tmp_path)
        lines = fixture_lines()
        f = write_transcript(base, lines + lines)
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        assert len(events) == 14
        store.store_with_offset(events, w.registry_key(f), "antigravity", str(f), offset)
        assert store.count() == 14
        store.close()

    def test_reread_is_idempotent(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        key = w.registry_key(f)
        store.store_with_offset(events, key, "antigravity", str(f), offset)
        # a failed store means a retry from 0: the same events come back and the store drops them
        again, _ = w._parse_and_adapt(f, 0)
        assert len(again) == len(events)
        store.store_with_offset(again, key, "antigravity", str(f), offset)
        assert store.count() == 14
        store.close()

    def test_append_is_incremental(self, tmp_path):
        base = _base(tmp_path)
        lines = fixture_lines()
        f = write_transcript(base, lines[:4])
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        first, offset = w._parse_and_adapt(f, 0)
        assert [e["event_type"] for e in first] == ["user", "thinking", "tool_use", "tool_result",
                                                    "tool_use", "tool_use"]
        append(f, lines[4:7])
        more, offset2 = w._parse_and_adapt(f, offset)
        assert [e["event_type"] for e in more] == ["tool_result", "tool_result", "assistant"]
        assert offset2 > offset
        store.close()

    def test_history_harvest_ingests_and_refreshes_the_session(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines(), age=OLD)
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files(since=0.0)
        parsed, inserted = w.harvest_history_file(f, w.registry_key(f))
        assert (parsed, inserted) == (14, 14)
        detail = store.get_session_detail(CONV)
        assert detail["event_count"] == 14
        assert detail["first_event_at"] == "2026-05-01T10:00:00Z"
        assert detail["last_event_at"] == "2026-05-01T10:00:20Z"
        # a second pass finds nothing new and does not duplicate
        assert w.harvest_history_file(f, w.registry_key(f)) == (0, 0)
        assert store.count() == 14
        store.close()

    def test_last_event_never_goes_back_when_steps_arrive_out_of_order(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, [step(0, "USER_INPUT", sec=1, content="start"),
                                    step(5, sec=50, content="late step written first"),
                                    step(2, sec=20, content="early step written last")])
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        store.store_with_offset(events, w.registry_key(f), "antigravity", str(f), offset)
        detail = store.get_session_detail(CONV)
        assert detail["first_event_at"] == "2026-05-01T10:00:01Z"
        assert detail["last_event_at"] == "2026-05-01T10:00:50Z"
        store.close()


    def test_an_older_step_arriving_in_a_later_poll_does_not_move_last_event_back(self, tmp_path):
        """upsert_session overwrites last_event_at: it must always be handed the conversation's latest."""
        base = _base(tmp_path)
        f = write_transcript(base, [step(0, "USER_INPUT", sec=1, content="start"),
                                    step(3, sec=50, content="late step")])
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        key = w.registry_key(f)
        events, offset = w._parse_and_adapt(f, 0)
        store.store_with_offset(events, key, "antigravity", str(f), offset)
        assert store.get_session_detail(CONV)["last_event_at"] == "2026-05-01T10:00:50Z"

        # poll 2: only an older step (written out of order) shows up
        append(f, [step(1, sec=20, content="written late, happened earlier")])
        events, offset2 = w._parse_and_adapt(f, offset)
        assert len(events) == 1
        store.store_with_offset(events, key, "antigravity", str(f), offset2)
        detail = store.get_session_detail(CONV)
        assert detail["last_event_at"] == "2026-05-01T10:00:50Z"
        assert detail["first_event_at"] == "2026-05-01T10:00:01Z"

        # the same after a restart: a fresh watcher rebuilds the range from the bytes before the offset
        append(f, [step(2, sec=30, content="another old one")])
        w2 = AntigravityWatcher(store, None, antigravity_base=base)
        w2.discover_files()
        events, offset3 = w2._parse_and_adapt(f, offset2)
        assert len(events) == 1
        store.store_with_offset(events, key, "antigravity", str(f), offset3)
        assert store.get_session_detail(CONV)["last_event_at"] == "2026-05-01T10:00:50Z"

        # and a genuinely newer step still advances it
        append(f, [step(9, sec=59, content="newest")])
        events, offset4 = w2._parse_and_adapt(f, offset3)
        store.store_with_offset(events, key, "antigravity", str(f), offset4)
        assert store.get_session_detail(CONV)["last_event_at"] == "2026-05-01T10:00:59Z"
        store.close()


# ── conversation_summaries.db in the cycle ───────────────────────


class TestSummariesInTheCycle:
    def test_title_workspace_and_subagent_link(self, tmp_path):
        base = _base(tmp_path)
        a = write_transcript(base, fixture_lines(), conv=CONV)
        b = write_transcript(base, [step(0, "USER_INPUT", content="sub task"),
                                    step(1, content="sub answer")], conv=CONV3)
        make_summaries_db(base, [
            summary_row(CONV, title="Greeting helper", workspace="file:///Users/u/work/acme/web"),
            summary_row(CONV3, parent=CONV, depth=1, workspace="file:///Users/u/work/acme/web"),
        ]).close()
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.discover_files()
        for f in (a, b):
            events, offset = w._parse_and_adapt(f, 0)
            store.store_with_offset(events, w.registry_key(f), "antigravity", str(f), offset)

        main, sub = store.get_session_detail(CONV), store.get_session_detail(CONV3)
        assert (main["title"], main["project"], main["cwd"]) == (
            "Greeting helper", "acme/web", "/Users/u/work/acme/web")
        assert main["is_sidechain"] is False
        assert sub["is_sidechain"] is True  # a separate session, linked to its parent
        assert sub["metadata"]["parent_session_id"] == CONV
        assert sub["metadata"]["nesting_depth"] == 1
        assert sub["title"] == "sub task"  # no index title: the first prompt
        store.close()

    def test_an_unreadable_index_never_stops_ingestion(self, tmp_path, caplog):
        caplog.set_level("WARNING", logger=LOGGER)
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        (base / "conversation_summaries.db").write_bytes(b"garbage" * 100)
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        for _ in range(3):  # three rescans
            assert w.discover_files() == [f]
        events, offset = w._parse_and_adapt(f, 0)
        assert len(events) == 14
        store.store_with_offset(events, w.registry_key(f), "antigravity", str(f), offset)
        # transcript-only: the project comes from the first run_command Cwd
        assert store.get_session_detail(CONV)["project"] == "proj"
        assert sum("No se pudo leer" in r.getMessage() for r in caplog.records) == 1
        store.close()

    def test_absent_index_is_fine(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        assert w.discover_files() == [f]
        assert len(w._parse_and_adapt(f, 0)[0]) == 14

    def test_the_last_good_index_is_kept_when_it_stops_reading(self, tmp_path):
        base = _base(tmp_path)
        write_transcript(base, fixture_lines())
        make_summaries_db(base, [summary_row(CONV, title="Kept title")]).close()
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        w.discover_files()
        assert w._summaries[CONV].title == "Kept title"
        (base / "conversation_summaries.db").write_bytes(b"garbage" * 100)
        w.discover_files()
        assert w._summaries[CONV].title == "Kept title"  # not forgotten, not an error

    def test_the_index_is_read_once_per_rescan_not_per_poll(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines()[:3])
        make_summaries_db(base, [summary_row(CONV)]).close()
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        reads = []
        real = w._parser.read_summaries
        w._parser.read_summaries = lambda b: (reads.append(b), real(b))[1]
        w.discover_files()
        assert len(reads) == 1
        offset = 0
        for lines in (fixture_lines()[3:6], fixture_lines()[6:9]):
            _, offset = w._parse_and_adapt(f, offset)
            append(f, lines)
        w._parse_and_adapt(f, offset)
        assert len(reads) == 1  # the polls reused the cached answer

    def test_only_the_summaries_database_is_ever_opened(self, tmp_path, monkeypatch):
        """The per-conversation .db files (WAL + protobuf) are never opened."""
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        make_summaries_db(base, [summary_row(CONV)]).close()
        (base / "conversations").mkdir()
        (base / "conversations" / f"{CONV}.db").write_bytes(b"SQLite format 3\0 pretend")
        opened: list[str] = []
        real_connect = sqlite3.connect

        def spy(database, *a, **kw):
            opened.append(str(database))
            return real_connect(database, *a, **kw)

        real_open = open

        def spy_open(file, *a, **kw):
            opened.append(str(file))
            return real_open(file, *a, **kw)

        monkeypatch.setattr(sqlite3, "connect", spy)
        monkeypatch.setattr("builtins.open", spy_open)
        w = AntigravityWatcher(_store(tmp_path), None, antigravity_base=base)
        w.discover_files()
        w._parse_and_adapt(f, 0)
        touched = [o for o in opened if "/conversations/" in o or "transcript_full" in o]
        assert touched == []
        assert any("conversation_summaries.db" in o for o in opened)


# ── through the harvester loop ───────────────────────────────────


class TestWatcherThroughBaseHarvester:
    def test_live_cycle_stores_events_and_resumes_from_its_offset(self, tmp_path):
        base = _base(tmp_path)
        f = write_transcript(base, fixture_lines())
        make_summaries_db(base, [summary_row(CONV, title="Live")]).close()
        store = _store(tmp_path)
        w = AntigravityWatcher(store, None, antigravity_base=base)
        w.POLL_INTERVAL = 0.05
        w.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and store.count() < 14:
                time.sleep(0.05)
            assert store.count() == 14
            append(f, [step(30, "USER_INPUT", sec=40, content="live append")])
            deadline = time.time() + 5
            while time.time() < deadline and store.count() < 15:
                time.sleep(0.05)
        finally:
            w.stop()
        assert store.count() == 15
        assert store.get_session_detail(CONV)["title"] == "Live"
        assert w.alive is False

        # a restarted watcher finds its offset and does not re-ingest
        w2 = AntigravityWatcher(store, None, antigravity_base=base)
        w2.POLL_INTERVAL = 0.05
        w2.start()
        try:
            time.sleep(0.5)
        finally:
            w2.stop()
        assert store.count() == 15
        store.close()
