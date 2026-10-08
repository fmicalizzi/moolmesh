"""Pi is registered across the shared surfaces: server, backfill, reporter."""

import json
import os
import time

from hub.dashboard.server import DashboardServer
from hub.models.base import Provider
from hub.watchers.pi_watcher import PiWatcher


def test_pi_watcher_registered_by_default():
    server = DashboardServer(host="localhost", port=0)
    labels = {label for label, _ in server.watchers}
    assert "Pi" in labels
    assert any(isinstance(w, PiWatcher) for _, w in server.watchers)


def test_pi_excluded_when_not_in_providers():
    server = DashboardServer(host="localhost", port=0, providers=["claude"])
    assert not any(isinstance(w, PiWatcher) for _, w in server.watchers)


def test_pi_is_a_file_backfill_provider():
    from hub.backfill import FILE_PROVIDERS, make_watcher
    assert "pi" in FILE_PROVIDERS
    watcher = make_watcher("pi", None, {})
    assert isinstance(watcher, PiWatcher)
    assert watcher.provider_name == "pi"


def test_batch_reporter_maps_pi():
    from hub.batch_reporter import _ADAPTERS, _PARSERS
    from hub.adapters.pi_adapter import PiAdapter
    from hub.parsers.pi_parser import PiParser
    assert isinstance(_PARSERS[Provider.PI], PiParser)
    assert isinstance(_ADAPTERS[Provider.PI], PiAdapter)


def test_backfill_ingests_pi_session_end_to_end(tmp_path):
    from hub.backfill import run_backfill
    from hub.cache.event_store import EventStore

    base = tmp_path / ".pi" / "agent"
    d = base / "sessions" / "--home-dev-acme-web--"
    d.mkdir(parents=True)
    lines = [
        {"type": "session", "version": 3, "id": "sess-pi-e2e",
         "timestamp": "2026-05-01T10:00:00.000Z", "cwd": "/home/dev/acme-web"},
        {"type": "message", "id": "u001", "parentId": None,
         "timestamp": "2026-05-01T10:00:05.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "hello pi e2e"}]}},
        {"type": "message", "id": "a001", "parentId": "u001",
         "timestamp": "2026-05-01T10:00:09.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "toolCall", "id": "c1", "name": "read",
                                  "arguments": {"path": "src/app.py"}}],
                     "model": "gpt-5.5", "provider": "openai-codex",
                     "usage": {"input": 10, "output": 5, "cacheRead": 0,
                               "cacheWrite": 0, "reasoning": 0,
                               "totalTokens": 15, "cost": {"total": 0.001}}}},
    ]
    f = d / "s1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    old = time.time() - 48 * 3600  # older than the daemon's live window
    os.utime(f, (old, old))

    store = EventStore(db_path=tmp_path / "events.db")
    report = run_backfill(
        store, providers=["pi"], bases={"pi": base},
        db_path=tmp_path / "events.db",
    )
    rep = report.providers[0]
    assert rep.provider == "pi"
    assert rep.events_inserted == 3
    assert rep.new_sessions == 1
    detail = store.get_session_detail("sess-pi-e2e")
    assert detail is not None
    events = store.get_session_events("sess-pi-e2e")
    tool_use = [e for e in events if e["event_type"] == "tool_use"]
    assert tool_use and tool_use[0]["file_path"] == "/home/dev/acme-web/src/app.py"
    store.close()
