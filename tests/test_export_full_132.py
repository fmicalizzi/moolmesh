"""``mool export`` writes JSON to --output and never cuts a long session (#132).

Synthetic fixtures only (invented ``acme`` session), no network.
"""
from __future__ import annotations

import argparse
import json

import pytest

import hub.cli as cli_mod
from hub.cache.event_store import EventStore

SID = "acme-long"
N = 620  # over the 500-row default of get_session_events


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = EventStore(db_path=tmp_path / "events.db")
    store.upsert_session({"id": SID, "provider": "claude", "project": "acme",
                          "title": "Acme long session"}, "2026-09-17T10:00:00Z")
    events = [{
        "provider": "claude", "project": "acme", "session_id": SID,
        "event_type": "assistant" if i % 2 else "user",
        "timestamp": f"2026-09-17T10:{i // 60:02d}:{i % 60:02d}Z",
        "summary": f"acme step {i}", "full_text": f"acme step {i} text",
    } for i in range(N)]
    store.store_with_offset(events, "fp-acme", "claude", "/tmp/acme.jsonl", 1)
    path = store.db_path
    store.close()
    original = EventStore.__init__
    monkeypatch.setattr(EventStore, "__init__",
                        lambda self, db_path=None: original(self, db_path=path))
    return path


def _export(fmt, output=None):
    cli_mod.cmd_export(argparse.Namespace(session_id=SID, format=fmt, output=output))


def test_json_writes_output_file(db, tmp_path, capsys):
    out = tmp_path / "acme.json"
    _export("json", str(out))
    printed = capsys.readouterr().out
    assert "Exported to" in printed and '"events"' not in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["events"]) == N


def test_json_stdout_without_output(db, capsys):
    _export("json")
    assert len(json.loads(capsys.readouterr().out)["events"]) == N


def test_markdown_exports_every_event(db, tmp_path, capsys):
    out = tmp_path / "acme.md"
    _export("markdown", str(out))
    text = out.read_text(encoding="utf-8")
    assert f"acme step {N - 1} text" in text
    assert text.count("acme step ") == N


def test_read_surface_keeps_its_default_page(db):
    store = EventStore(db_path=db)
    try:
        assert len(store.get_session_events(SID)) == 500
        assert len(store.get_session_events(SID, active_branch=True)) == 500
    finally:
        store.close()
