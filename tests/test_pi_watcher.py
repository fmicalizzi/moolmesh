"""Tests for the Pi watcher (discovery window, ingest, idempotence)."""

import json
import os
import time
from pathlib import Path

from hub.cache.event_store import EventStore, file_fingerprint
from hub.watchers.pi_watcher import PiWatcher

HEADER = {
    "type": "session", "version": 3, "id": "sess-w1",
    "timestamp": "2026-05-01T10:00:00.000Z",
    "cwd": "/home/dev/acme-web",
}
MSG = {
    "type": "message", "id": "u001", "parentId": None,
    "timestamp": "2026-05-01T10:00:05.000Z",
    "message": {"role": "user", "content": [{"type": "text", "text": "hello pi"}]},
}


def _session_file(base: Path, folder: str, name: str, *lines: dict,
                  age: float = 0.0) -> Path:
    d = base / "sessions" / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(f, (t, t))
    return f


def _store(tmp_path: Path) -> EventStore:
    return EventStore(db_path=tmp_path / "events.db")


class TestDiscovery:
    def test_provider_name_and_catchup(self, tmp_path):
        w = PiWatcher(_store(tmp_path), None, pi_base=tmp_path / ".pi" / "agent")
        assert w.provider_name == "pi"
        assert w.CATCHUP is True

    def test_discovers_fresh_session_files(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG)
        w = PiWatcher(_store(tmp_path), None, pi_base=base)
        files = w.discover_files()
        assert files == [f]
        assert w._file_projects[f].endswith("acme-web")

    def test_old_files_excluded_from_live_window(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        _session_file(base, "--home-dev-acme-web--", "old.jsonl", HEADER, MSG,
                      age=48 * 3600)
        w = PiWatcher(_store(tmp_path), None, pi_base=base)
        assert w.discover_files() == []
        # ...but a since= window sees it (history path).
        assert len(w.discover_files(since=0.0)) == 1

    def test_project_filter(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        _session_file(base, "--a--", "a.jsonl", HEADER, MSG)
        other = dict(HEADER, id="sess-w2", cwd="/home/dev/other")
        _session_file(base, "--b--", "b.jsonl", other, dict(MSG, id="u002"))
        w = PiWatcher(_store(tmp_path), None, project_filter="acme", pi_base=base)
        assert len(w.discover_files()) == 1


class TestParseAndAdapt:
    def test_events_and_session_meta_stored(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG)
        store = _store(tmp_path)
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files()
        events, new_offset = w._parse_and_adapt(f, 0)
        assert new_offset == f.stat().st_size
        types = [e["event_type"] for e in events]
        assert types == ["system", "user"]
        for event in events:
            assert event["provider"] == "pi"
            assert event["session_id"] == "sess-w1"

        fp = file_fingerprint(f)
        store.store_with_offset(events, fp, "pi", str(f), new_offset)
        detail = store.get_session_detail("sess-w1")
        assert detail["provider"] == "pi"
        assert detail["cwd"] == "/home/dev/acme-web"
        assert detail["project"].endswith("acme-web")
        store.close()

    def test_reread_is_idempotent(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG)
        store = _store(tmp_path)
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        fp = file_fingerprint(f)
        store.store_with_offset(events, fp, "pi", str(f), offset)
        first_count = store.count()

        # Re-read from 0 (e.g. collision recovery): parser dedupes by id.
        again, _ = w._parse_and_adapt(f, 0)
        assert again == []
        store.store_with_offset(again, fp, "pi", str(f), offset)
        assert store.count() == first_count
        store.close()

    def test_append_is_incremental(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG)
        store = _store(tmp_path)
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files()
        events, offset = w._parse_and_adapt(f, 0)
        assert len(events) == 2

        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "message", "id": "a001",
                                 "parentId": "u001",
                                 "timestamp": "2026-05-01T10:00:09.000Z",
                                 "message": {"role": "assistant",
                                             "content": [{"type": "text",
                                                          "text": "hi"}]}}) + "\n")
        more, offset2 = w._parse_and_adapt(f, offset)
        assert [e["event_type"] for e in more] == ["assistant"]
        assert offset2 > offset
        store.close()

    def test_history_harvest_ingests_and_refreshes(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        f = _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG,
                          age=48 * 3600)
        store = _store(tmp_path)
        w = PiWatcher(store, None, pi_base=base)
        w.discover_files(since=0.0)
        parsed, inserted = w.harvest_history_file(f, file_fingerprint(f))
        assert parsed == 2 and inserted == 2
        detail = store.get_session_detail("sess-w1")
        assert detail["event_count"] == 2
        store.close()


class TestWatcherThroughBaseHarvester:
    def test_live_cycle_stores_events(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        _session_file(base, "--home-dev-acme-web--", "s1.jsonl", HEADER, MSG)
        store = _store(tmp_path)
        w = PiWatcher(store, None, pi_base=base)
        w.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and store.count() == 0:
                time.sleep(0.1)
        finally:
            w.stop()
        assert store.count() == 2
        assert w.alive is False
        store.close()
