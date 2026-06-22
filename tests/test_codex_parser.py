"""Tests for Codex parser."""

import json
import tempfile
import threading
from pathlib import Path

import pytest

from hub.parsers.codex_parser import CodexParser
from hub.models.codex import CodexEntry
from hub.models.base import Provider
from hub.batch_reporter import _PARSERS

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestCodexParserParseFile:
    def setup_method(self):
        self.parser = CodexParser()

    def test_parse_file_returns_entries(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        assert len(entries) == 7

    def test_session_meta_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        meta = entries[0]
        assert meta.event_type == "session_meta"
        assert meta.session_id == "abc123-def456"
        assert meta.cwd == "/Users/test/project"
        assert meta.cli_version == "0.98.0"
        assert meta.model_provider == "openai"
        assert meta.source == "vscode"

    def test_event_msg_user_input_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        msg = entries[1]
        assert msg.event_type == "event_msg"
        assert msg.event_msg_text == "fix the bug"
        assert msg.role == "user"

    def test_response_item_message_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        msg = entries[2]
        assert msg.event_type == "response_item"
        assert msg.payload_type == "message"
        assert msg.role == "assistant"
        assert "I'll fix it" in msg.text

    def test_response_item_function_call_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        fc = entries[3]
        assert fc.event_type == "response_item"
        assert fc.payload_type == "function_call"
        assert fc.function_call is not None
        assert fc.function_call.name == "shell"
        assert "git diff" in fc.function_call.arguments

    def test_response_item_function_output_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        fo = entries[4]
        assert fo.event_type == "response_item"
        assert fo.payload_type == "function_call_output"
        assert fo.function_output is not None
        assert fo.function_output.call_id == "fc1"
        assert "diff --git" in fo.function_output.output

    def test_response_item_reasoning_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        reasoning = entries[5]
        assert reasoning.event_type == "response_item"
        assert reasoning.payload_type == "reasoning"
        assert "config parser" in reasoning.reasoning_text

    def test_token_count_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        tc = entries[6]
        assert tc.event_type == "token_count"
        assert tc.token_input == 1200
        assert tc.token_output == 350
        assert tc.token_cached_input == 800
        assert tc.token_reasoning == 120
        assert tc.token_total == 2470


class TestCodexParserSessionContext:
    """Verify that session_id/cwd/model_provider are propagated to all entries."""

    def test_context_propagated_to_event_msg(self):
        parser = CodexParser()
        entries = parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        msg = entries[1]  # event_msg
        assert msg.session_id == "abc123-def456"
        assert msg.cwd == "/Users/test/project"
        assert msg.cli_version == "0.98.0"
        assert msg.model_provider == "openai"

    def test_context_propagated_to_response_item(self):
        parser = CodexParser()
        entries = parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        msg = entries[2]  # response_item message
        assert msg.session_id == "abc123-def456"
        assert msg.cwd == "/Users/test/project"

    def test_context_propagated_to_function_call(self):
        parser = CodexParser()
        entries = parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        fc = entries[3]  # function_call
        assert fc.session_id == "abc123-def456"
        assert fc.cwd == "/Users/test/project"

    def test_context_propagated_to_token_count(self):
        parser = CodexParser()
        entries = parser.parse_file(FIXTURES_DIR / "codex_sample.jsonl")
        tc = entries[6]  # token_count
        assert tc.session_id == "abc123-def456"
        assert tc.cwd == "/Users/test/project"
        assert tc.model_provider == "openai"

    def test_context_reset_between_files(self):
        """Different files should have independent session contexts."""
        with tempfile.TemporaryDirectory() as tmp:
            # File A
            file_a = Path(tmp) / "a.jsonl"
            file_a.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-01-01",
                "payload": {"id": "session-a", "cwd": "/path/a",
                            "cli_version": "1.0", "model_provider": "openai", "source": "cli"}
            }) + "\n" + json.dumps({
                "type": "event_msg", "timestamp": "2026-01-01",
                "payload": {"content": "hello"}
            }) + "\n")

            # File B
            file_b = Path(tmp) / "b.jsonl"
            file_b.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-01-01",
                "payload": {"id": "session-b", "cwd": "/path/b",
                            "cli_version": "2.0", "model_provider": "anthropic", "source": "vscode"}
            }) + "\n" + json.dumps({
                "type": "event_msg", "timestamp": "2026-01-01",
                "payload": {"content": "world"}
            }) + "\n")

            parser = CodexParser()
            entries_a = parser.parse_file(file_a)
            entries_b = parser.parse_file(file_b)

            assert entries_a[1].session_id == "session-a"
            assert entries_a[1].cwd == "/path/a"
            assert entries_b[1].session_id == "session-b"
            assert entries_b[1].cwd == "/path/b"


class TestCodexParserIncremental:
    """Verify incremental parsing maintains context between calls."""

    def test_incremental_maintains_context(self):
        parser = CodexParser()
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "session.jsonl"
            # Write session_meta + event_msg
            fpath.write_text(
                json.dumps({
                    "type": "session_meta", "timestamp": "2026-01-01",
                    "payload": {"id": "inc-001", "cwd": "/tmp/inc",
                                "cli_version": "1.0", "model_provider": "openai", "source": "cli"}
                }) + "\n"
                + json.dumps({
                    "type": "event_msg", "timestamp": "2026-01-01",
                    "payload": {"content": "first message"}
                }) + "\n"
            )
            entries1, offset = parser.parse_incremental(fpath, 0)
            assert len(entries1) == 2
            assert entries1[1].session_id == "inc-001"
            assert entries1[1].cwd == "/tmp/inc"

            # Append more data
            with open(fpath, "a") as f:
                f.write(json.dumps({
                    "type": "event_msg", "timestamp": "2026-01-02",
                    "payload": {"content": "second message"}
                }) + "\n")

            entries2, _ = parser.parse_incremental(fpath, offset)
            assert len(entries2) == 1
            # Context should persist from previous call
            assert entries2[0].session_id == "inc-001"
            assert entries2[0].cwd == "/tmp/inc"


class TestCodexParserCanParse:
    def test_detects_codex_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "rollout-test.jsonl"
            fpath.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-01-01",
                "payload": {"id": "x"}
            }) + "\n")
            assert CodexParser.can_parse(fpath) is True

    def test_rejects_non_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.txt"
            fpath.write_text("hello\n")
            assert CodexParser.can_parse(fpath) is False

    def test_rejects_non_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "something.jsonl"
            fpath.write_text("{}\n")
            assert CodexParser.can_parse(fpath) is False

    def test_rejects_other_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "rollout-test.jsonl"
            fpath.write_text(json.dumps({"type": "other"}) + "\n")
            assert CodexParser.can_parse(fpath) is False


class TestCodexParserEdgeCases:
    def test_empty_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "rollout-test.jsonl"
            fpath.write_text(
                json.dumps({
                    "type": "session_meta", "timestamp": "2026-01-01",
                    "payload": {"id": "x", "cwd": "/t", "cli_version": "1", "model_provider": "o", "source": "c"}
                }) + "\n\n\n"
                + json.dumps({"type": "event_msg", "timestamp": "2026-01-01", "payload": {"content": "hi"}}) + "\n"
            )
            parser = CodexParser()
            entries = parser.parse_file(fpath)
            assert len(entries) == 2

    def test_invalid_json_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "rollout-test.jsonl"
            fpath.write_text(
                json.dumps({
                    "type": "session_meta", "timestamp": "2026-01-01",
                    "payload": {"id": "x", "cwd": "/t", "cli_version": "1", "model_provider": "o", "source": "c"}
                }) + "\n"
                + "not valid json\n"
                + json.dumps({"type": "event_msg", "timestamp": "2026-01-01", "payload": {"content": "hi"}}) + "\n"
            )
            parser = CodexParser()
            entries = parser.parse_file(fpath)
            assert len(entries) == 2

    def test_turn_context_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "rollout-test.jsonl"
            fpath.write_text(
                json.dumps({
                    "type": "session_meta", "timestamp": "2026-01-01",
                    "payload": {"id": "x", "cwd": "/t", "cli_version": "1", "model_provider": "o", "source": "c"}
                }) + "\n"
                + json.dumps({"type": "turn_context", "timestamp": "2026-01-01", "payload": {}}) + "\n"
                + json.dumps({"type": "event_msg", "timestamp": "2026-01-01", "payload": {"content": "hi"}}) + "\n"
            )
            parser = CodexParser()
            entries = parser.parse_file(fpath)
            assert len(entries) == 2  # session_meta + event_msg, turn_context skipped


class TestCodexParserConcurrency:
    """Verify parse_file() is thread-safe — each call uses local context."""

    def test_concurrent_parse_no_cross_contamination(self):
        """Two threads parsing different files should not contaminate each other's context."""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            # File A: session with id=session-a, cwd=/path/a
            file_a = Path(tmp) / "a.jsonl"
            file_a.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-01-01",
                "payload": {"id": "session-a", "cwd": "/path/a",
                            "cli_version": "1.0", "model_provider": "openai", "source": "cli"}
            }) + "\n" + json.dumps({
                "type": "event_msg", "timestamp": "2026-01-01",
                "payload": {"content": "hello from A"}
            }) + "\n")

            # File B: session with id=session-b, cwd=/path/b
            file_b = Path(tmp) / "b.jsonl"
            file_b.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-01-01",
                "payload": {"id": "session-b", "cwd": "/path/b",
                            "cli_version": "2.0", "model_provider": "anthropic", "source": "vscode"}
            }) + "\n" + json.dumps({
                "type": "event_msg", "timestamp": "2026-01-01",
                "payload": {"content": "hello from B"}
            }) + "\n")

            results: dict[str, list] = {"a": [], "b": [], "errors": []}

            def parse_file_a():
                try:
                    parser = CodexParser()
                    entries = parser.parse_file(file_a)
                    results["a"] = entries
                except Exception as e:
                    results["errors"].append(f"A: {e}")

            def parse_file_b():
                try:
                    parser = CodexParser()
                    entries = parser.parse_file(file_b)
                    results["b"] = entries
                except Exception as e:
                    results["errors"].append(f"B: {e}")

            # Run both concurrently with the SHARED global parser from batch_reporter
            shared_parser = CodexParser()
            _PARSERS[Provider.CODEX] = shared_parser

            t_a = threading.Thread(target=lambda: results.__setitem__("a", shared_parser.parse_file(file_a)))
            t_b = threading.Thread(target=lambda: results.__setitem__("b", shared_parser.parse_file(file_b)))

            t_a.start()
            t_b.start()
            t_a.join(timeout=5)
            t_b.join(timeout=5)

            assert not results.get("errors", []), f"Errors: {results.get('errors', [])}"
            # Verify each file's entries have the CORRECT context (not contaminated)
            entries_a = results.get("a", [])
            entries_b = results.get("b", [])
            # File A entries should have session-a context
            for e in entries_a:
                if e.event_type != "session_meta":
                    assert e.session_id == "session-a", f"File A entry has wrong session_id: {e.session_id}"
                    assert e.cwd == "/path/a", f"File A entry has wrong cwd: {e.cwd}"
            # File B entries should have session-b context
            for e in entries_b:
                if e.event_type != "session_meta":
                    assert e.session_id == "session-b", f"File B entry has wrong session_id: {e.session_id}"
                    assert e.cwd == "/path/b", f"File B entry has wrong cwd: {e.cwd}"
