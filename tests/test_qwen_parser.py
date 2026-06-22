"""Tests for Qwen parser."""

import json
import tempfile
from pathlib import Path

from hub.parsers.qwen_parser import QwenParser
from hub.models.qwen import QwenEntry

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestQwenParserParseFile:
    def setup_method(self):
        self.parser = QwenParser()

    def test_parse_file_returns_entries(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        assert len(entries) == 6

    def test_user_message_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        user = entries[0]
        assert user.type == "user"
        assert user.uuid == "q1"
        assert user.session_id == "qwen-s1"
        assert user.cwd == "/Users/test/qwenproj"
        assert user.text == "explain the code"
        assert user.usage is not None
        assert user.usage.prompt_tokens == 100
        assert user.usage.candidates_tokens == 50

    def test_assistant_text_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        assistant = entries[1]
        assert assistant.type == "assistant"
        assert assistant.uuid == "q2"
        assert "explanation" in assistant.text
        assert not assistant.has_thought

    def test_assistant_with_function_call(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        fc = entries[2]
        assert fc.type == "assistant"
        assert len(fc.function_calls) == 1
        assert fc.function_calls[0].name == "run_shell_command"
        assert fc.function_calls[0].call_id == "fc1"
        assert fc.function_calls[0].args["command"] == "ls -la"

    def test_tool_result_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        tr = entries[3]
        assert tr.type == "tool_result"
        assert len(tr.function_responses) == 1
        assert tr.function_responses[0].name == "run_shell_command"
        assert "total 42" in tr.function_responses[0].output

    def test_thought_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        thought = entries[4]
        assert thought.type == "assistant"
        assert thought.has_thought is True
        assert "thinking" in thought.text

    def test_system_event_parsed(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "qwen_sample.jsonl")
        system = entries[5]
        assert system.type == "system"
        assert system.subtype == "instructions"
        assert "helpful coding" in system.text


class TestQwenParserCanParse:
    def test_detects_qwen_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.jsonl"
            fpath.write_text(json.dumps({
                "uuid": "x", "type": "user",
                "message": {"parts": []}
            }) + "\n")
            assert QwenParser.can_parse(fpath) is True

    def test_rejects_non_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.txt"
            fpath.write_text("hello\n")
            assert QwenParser.can_parse(fpath) is False

    def test_rejects_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.jsonl"
            fpath.write_text("")
            assert QwenParser.can_parse(fpath) is False


class TestQwenParserEdgeCases:
    def test_empty_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.jsonl"
            fpath.write_text(json.dumps({
                "uuid": "x", "type": "user",
                "message": {"parts": [{"text": "hello"}]}
            }) + "\n\n\n")
            parser = QwenParser()
            entries = parser.parse_file(fpath)
            assert len(entries) == 1

    def test_invalid_json_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.jsonl"
            fpath.write_text(json.dumps({
                "uuid": "x", "type": "user",
                "message": {"parts": [{"text": "hello"}]}
            }) + "\nnot json\n" + json.dumps({
                "uuid": "y", "type": "user",
                "message": {"parts": [{"text": "bye"}]}
            }) + "\n")
            parser = QwenParser()
            entries = parser.parse_file(fpath)
            assert len(entries) == 2

    def test_truncates_large_function_output(self):
        """Function response output should be truncated to 500 chars."""
        with tempfile.TemporaryDirectory() as tmp:
            fpath = Path(tmp) / "test.jsonl"
            long_output = "x" * 1000
            fpath.write_text(json.dumps({
                "uuid": "x", "type": "tool_result",
                "message": {"role": "user", "parts": [{
                    "functionResponse": {
                        "id": "fc1", "name": "test",
                        "response": {"output": long_output}
                    }
                }]}
            }) + "\n")
            parser = QwenParser()
            entries = parser.parse_file(fpath)
            assert len(entries[0].function_responses) == 1
            assert len(entries[0].function_responses[0].output) == 500
