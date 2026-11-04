"""Codex per-turn cwd and recognized-but-ignored line types (issue #47.1/#47.3).

Synthetic rollouts only.
"""

import json
from pathlib import Path

from hub.adapters.codex_adapter import CodexAdapter
from hub.parsers.codex_parser import CodexParser


def _write(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    return path


META = {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
        "payload": {"id": "sess-tc", "cwd": "/w/start", "cli_version": "1",
                    "model_provider": "openai", "source": "cli"}}


def _turn(cwd):
    return {"type": "turn_context", "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"cwd": cwd, "model": "m"}}


def _msg(text):
    return {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z",
            "payload": {"type": "user_message", "message": text}}


def _patch_call(rel):
    patch = f"*** Begin Patch\n*** Update File: {rel}\n@@\n-a\n+b\n*** End Patch\n"
    return {"type": "response_item", "timestamp": "2026-01-01T00:00:03Z",
            "payload": {"type": "custom_tool_call", "name": "apply_patch",
                        "input": patch, "call_id": "c1"}}


class TestTurnContextCwd:
    def test_turn_cwd_overrides_session_cwd(self, tmp_path):
        f = _write(tmp_path / "rollout-a.jsonl",
                   [META, _msg("one"), _turn("/w/other"), _msg("two")])
        entries = CodexParser().parse_file(f)
        msgs = [e for e in entries if e.event_type == "event_msg"]
        assert [m.cwd for m in msgs] == ["/w/start", "/w/other"]
        # session_meta keeps the session's starting cwd
        assert entries[0].cwd == "/w/start"

    def test_relative_patch_resolves_against_turn_cwd(self, tmp_path):
        f = _write(tmp_path / "rollout-b.jsonl",
                   [META, _turn("/w/other"), _patch_call("src/a.py")])
        entries = CodexParser().parse_file(f)
        call = [e for e in entries if e.event_type == "response_item"][0]
        events = CodexAdapter().to_events(call, "proj")
        assert any(ev.file_path == "/w/other/src/a.py" for ev in events)
        assert all(ev.cwd == "/w/other" for ev in events)

    def test_relative_or_bad_turn_cwd_is_ignored(self, tmp_path):
        f = _write(tmp_path / "rollout-c.jsonl",
                   [META, _turn("relative/dir"), _msg("x"), _turn(None), _msg("y")])
        msgs = [e for e in CodexParser().parse_file(f) if e.event_type == "event_msg"]
        assert [m.cwd for m in msgs] == ["/w/start", "/w/start"]

    def test_windows_turn_cwd_kept(self, tmp_path):
        f = _write(tmp_path / "rollout-d.jsonl",
                   [META, _turn("C:\\work\\app"), _msg("x")])
        msgs = [e for e in CodexParser().parse_file(f) if e.event_type == "event_msg"]
        assert msgs[0].cwd == "C:\\work\\app"

    def test_incremental_tail_follows_turn_cwd(self, tmp_path):
        f = _write(tmp_path / "rollout-e.jsonl", [META, _msg("one")])
        p = CodexParser()
        _, off = p.parse_incremental(f, 0)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_turn("/w/next")) + "\n" + json.dumps(_msg("two")) + "\n")
        entries, _ = p.parse_incremental(f, off)
        assert [e.cwd for e in entries] == ["/w/next"]


class TestIgnoredLineTypes:
    def test_world_state_and_token_usage_record_are_ignored(self, tmp_path):
        f = _write(tmp_path / "rollout-f.jsonl", [
            META,
            {"type": "world_state", "timestamp": "t", "payload": {"x": 1}},
            {"type": "token_usage_record", "timestamp": "t",
             "payload": {"input_tokens": 5}},
            {"type": "world_state", "timestamp": "t", "payload": "not-a-dict"},
            _msg("hi"),
        ])
        entries = CodexParser().parse_file(f)
        assert [e.event_type for e in entries] == ["session_meta", "event_msg"]
