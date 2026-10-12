"""Antigravity is registered across the shared surfaces: server, backfill, CLI, reporter."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pytest

from hub.dashboard.server import DashboardServer
from hub.models.base import SHELL_TOOLS, Provider
from hub.watchers.antigravity_watcher import AntigravityWatcher
from tests.test_antigravity_parser import (
    CONV,
    fixture_lines,
    make_summaries_db,
    summary_row,
    write_transcript,
)


def test_provider_enum_value():
    assert Provider.ANTIGRAVITY.value == "antigravity"
    assert Provider("antigravity") is Provider.ANTIGRAVITY


def test_run_command_is_a_shell_tool():
    assert "run_command" in SHELL_TOOLS


def test_watcher_registered_by_default_and_labelled():
    server = DashboardServer(host="localhost", port=0)
    labels = {label for label, _ in server.watchers}
    assert "Antigravity" in labels  # what /health reports (#65)
    assert any(isinstance(w, AntigravityWatcher) for _, w in server.watchers)


def test_excluded_when_not_in_providers():
    server = DashboardServer(host="localhost", port=0, providers=["claude"])
    assert not any(isinstance(w, AntigravityWatcher) for _, w in server.watchers)


def test_selectable_by_name():
    server = DashboardServer(host="localhost", port=0, providers=["antigravity"])
    assert [label for label, _ in server.watchers] == ["Antigravity"]


def test_is_a_file_backfill_provider():
    from hub.backfill import FILE_PROVIDERS, backfill, gap_fill, make_watcher
    assert "antigravity" in FILE_PROVIDERS
    watcher = make_watcher("antigravity", None, {})
    assert isinstance(watcher, AntigravityWatcher)
    assert watcher.provider_name == "antigravity"
    assert backfill()["antigravity"] == 0 and gap_fill()["antigravity"] == 0


def test_batch_reporter_maps_antigravity():
    from hub.adapters.antigravity_adapter import AntigravityAdapter
    from hub.batch_reporter import _ADAPTERS, _PARSERS
    from hub.parsers.antigravity_parser import AntigravityParser
    assert isinstance(_PARSERS[Provider.ANTIGRAVITY], AntigravityParser)
    assert isinstance(_ADAPTERS[Provider.ANTIGRAVITY], AntigravityAdapter)


def test_batch_reporter_loads_messages(tmp_path):
    from hub.batch_reporter import load_project_messages
    from hub.discovery import ProjectDiscovery

    base = tmp_path / ".gemini" / "antigravity-cli"
    write_transcript(base, fixture_lines())
    (project,) = ProjectDiscovery(antigravity_base=base).discover_antigravity()
    messages = load_project_messages(project)
    assert messages and {m.provider for m in messages} == {Provider.ANTIGRAVITY}
    assert f"{CONV}:3" in {m.id for m in messages}


def test_cli_choices_accept_antigravity(monkeypatch, capsys, tmp_path):
    from hub.cli import main

    base = tmp_path / "agy"
    write_transcript(base, fixture_lines())
    monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(base))
    monkeypatch.setattr(sys, "argv", ["mool", "discover", "--provider", "antigravity", "--json"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert [(p["provider"], p["session_files"]) for p in out] == [("antigravity", 1)]

    for argv in (["mool", "report", "--provider", "antigravity", "--help"],
                 ["mool", "sessions", "--provider", "antigravity", "--help"],
                 ["mool", "backfill", "--provider", "antigravity", "--help"],
                 ["mool", "bogus-provider-check"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == (0 if "--help" in argv else 2)
    capsys.readouterr()


def test_cli_backfill_dry_run_lists_the_provider(capsys, tmp_path, monkeypatch):
    from hub.cli import cmd_backfill

    monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(tmp_path / "agy"))
    args = argparse.Namespace(provider="antigravity", since=None, dry_run=True, limit=None,
                              verbose=False)
    cmd_backfill(args)
    assert "ANTIGRAVITY" in capsys.readouterr().out


def test_backfill_ingests_a_conversation_end_to_end(tmp_path):
    from hub.backfill import run_backfill
    from hub.cache.event_store import EventStore

    base = tmp_path / ".gemini" / "antigravity-cli"
    f = write_transcript(base, fixture_lines())
    make_summaries_db(base, [summary_row(CONV, title="Backfilled")]).close()
    old = time.time() - 48 * 3600  # older than the daemon's live window
    os.utime(f, (old, old))

    store = EventStore(db_path=tmp_path / "events.db")
    report = run_backfill(
        store, providers=["antigravity"], bases={"antigravity": base},
        db_path=tmp_path / "events.db",
    )
    rep = report.providers[0]
    assert rep.provider == "antigravity"
    assert (rep.events_parsed, rep.events_inserted, rep.new_sessions) == (14, 14, 1)

    detail = store.get_session_detail(CONV)
    assert detail["title"] == "Backfilled" and detail["cwd"] == "/Users/u/work/proj"
    assert detail["event_count"] == 14
    events = store.get_session_events(CONV)
    assert {e["file_path"] for e in events if e["file_path"]} == {
        "/Users/u/work/proj/src/app.py", "/Users/u/work/proj/src/greeting.py"}

    # idempotent: a second run reads nothing new
    again = run_backfill(
        store, providers=["antigravity"], bases={"antigravity": base},
        db_path=tmp_path / "events.db",
    )
    assert again.providers[0].events_inserted == 0 and again.providers[0].up_to_date == 1
    assert store.count() == 14
    store.close()


def test_backfill_resumes_after_an_append(tmp_path):
    from hub.backfill import run_backfill
    from hub.cache.event_store import EventStore
    from tests.test_antigravity_parser import append, step

    base = tmp_path / ".gemini" / "antigravity-cli"
    lines = fixture_lines()
    f = write_transcript(base, lines)
    store = EventStore(db_path=tmp_path / "events.db")
    kw = dict(providers=["antigravity"], bases={"antigravity": base}, db_path=tmp_path / "events.db")
    old = time.time() - 48 * 3600
    os.utime(f, (old, old))
    run_backfill(store, **kw)
    # the CLI resumed the conversation: the writer appended a repeated copy and one new step
    append(f, lines)
    append(f, [step(30, "USER_INPUT", sec=45, content="resumed later")])
    os.utime(f, (old, old))
    report = run_backfill(store, **kw)
    assert report.providers[0].events_inserted == 1
    assert store.count() == 15
    store.close()
