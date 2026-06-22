"""Tests for Claude JSONL parser."""

import json
import tempfile
from pathlib import Path

from hub.parsers.claude_parser import ClaudeParser

from .conftest import FIXTURES_DIR


class TestClaudeParserFullFile:
    def setup_method(self):
        self.parser = ClaudeParser()
        self.sample = FIXTURES_DIR / "claude_sample.jsonl"

    def test_parse_file_returns_entries(self):
        entries = self.parser.parse_file(self.sample)
        # 9 lines total, 1 is file-history-snapshot (skipped) = 8
        assert len(entries) == 8

    def test_skips_file_history_snapshot(self):
        entries = self.parser.parse_file(self.sample)
        types = [e.type for e in entries]
        assert "file-history-snapshot" not in types

    def test_user_entry_parsed(self):
        entries = self.parser.parse_file(self.sample)
        user = entries[0]
        assert user.type == "user"
        assert user.uuid == "u001"
        assert user.session_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert user.content_text == "Show me the main config file"
        assert user.content_blocks == []  # string content -> no blocks

    def test_assistant_with_tool_use(self):
        entries = self.parser.parse_file(self.sample)
        assistant = entries[1]  # second entry: assistant with text + tool_use
        assert assistant.type == "assistant"
        assert assistant.model == "claude-sonnet-4-20250514"
        assert len(assistant.content_blocks) == 2
        assert assistant.content_blocks[0].type == "text"
        assert assistant.content_blocks[1].type == "tool_use"
        assert assistant.content_blocks[1].tool_name == "Read"
        assert assistant.usage is not None
        assert assistant.usage.input_tokens == 1200
        assert assistant.usage.cache_creation_input_tokens == 8000

    def test_thinking_block_parsed(self):
        entries = self.parser.parse_file(self.sample)
        # Entry 5 (index 4 after skip) has thinking + tool_use
        thinking_entry = entries[5]
        thinking_blocks = [b for b in thinking_entry.content_blocks if b.type == "thinking"]
        assert len(thinking_blocks) == 1
        assert "pytest" in thinking_blocks[0].thinking

    def test_system_entry_with_subtype(self):
        entries = self.parser.parse_file(self.sample)
        system = entries[6]
        assert system.type == "system"
        assert system.subtype == "at_mention"

    def test_summary_entry(self):
        entries = self.parser.parse_file(self.sample)
        summary = entries[7]
        assert summary.type == "summary"
        assert summary.is_sidechain is True
        assert "config.yaml" in summary.content_text


class TestClaudeParserIncremental:
    def setup_method(self):
        self.parser = ClaudeParser()

    def test_incremental_reads_only_new_lines(self):
        sample = FIXTURES_DIR / "claude_sample.jsonl"

        # First parse: get all entries and final offset
        all_entries = self.parser.parse_file(sample)
        with open(sample, "rb") as f:
            f.seek(0, 2)
            total_size = f.tell()

        # Read first line to get offset after it
        with open(sample, "rb") as f:
            first_line = f.readline()
            offset_after_first = len(first_line)

        # Incremental from after first line
        entries, new_offset = self.parser.parse_incremental(sample, offset_after_first)
        assert len(entries) == len(all_entries) - 1
        assert new_offset == total_size

    def test_incremental_no_new_data(self):
        sample = FIXTURES_DIR / "claude_sample.jsonl"
        with open(sample, "rb") as f:
            f.seek(0, 2)
            end = f.tell()

        entries, offset = self.parser.parse_incremental(sample, end)
        assert entries == []
        assert offset == end

    def test_incremental_with_append(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            line1 = json.dumps({
                "type": "user", "uuid": "x1", "sessionId": "s1",
                "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp",
                "message": {"role": "user", "content": "hello"},
            })
            f.write(line1 + "\n")

        entries1, offset1 = self.parser.parse_incremental(path, 0)
        assert len(entries1) == 1

        # Append a new line
        with open(path, "a") as f:
            line2 = json.dumps({
                "type": "user", "uuid": "x2", "sessionId": "s1",
                "timestamp": "2026-01-01T00:01:00Z", "cwd": "/tmp",
                "message": {"role": "user", "content": "world"},
            })
            f.write(line2 + "\n")

        entries2, offset2 = self.parser.parse_incremental(path, offset1)
        assert len(entries2) == 1
        assert entries2[0].content_text == "world"
        assert offset2 > offset1

        path.unlink()


class TestClaudeParserCanParse:
    def test_detects_claude_format(self):
        sample = FIXTURES_DIR / "claude_sample.jsonl"
        assert ClaudeParser.can_parse(sample) is True

    def test_rejects_non_jsonl(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = Path(f.name)
            f.write(b"not jsonl\n")
        assert ClaudeParser.can_parse(path) is False
        path.unlink()

    def test_rejects_other_jsonl(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
            f.write(json.dumps({"type": "event", "data": "other"}) + "\n")
        assert ClaudeParser.can_parse(path) is False
        path.unlink()
