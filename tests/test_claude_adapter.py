"""Tests for Claude adapter."""

from hub.adapters.claude_adapter import ClaudeAdapter
from hub.models.base import MessageRole, Provider
from hub.models.claude import ClaudeContentBlock, ClaudeEntry, ClaudeUsage
from hub.parsers.claude_parser import ClaudeParser

from .conftest import FIXTURES_DIR


class TestClaudeAdapterToUnified:
    def setup_method(self):
        self.adapter = ClaudeAdapter()
        self.parser = ClaudeParser()

    def test_user_message(self):
        entry = ClaudeEntry(
            type="user", uuid="u1", session_id="s1",
            timestamp="2026-03-15T10:00:00.000Z", cwd="/tmp/proj",
            content_text="hello world", role="user",
        )
        msg = self.adapter.to_unified(entry, "test-project")
        assert msg is not None
        assert msg.provider == Provider.CLAUDE
        assert msg.role == MessageRole.USER
        assert msg.text == "hello world"
        assert msg.project == "test-project"
        assert msg.id == "u1"

    def test_assistant_with_tool_use(self):
        blocks = [
            ClaudeContentBlock(type="text", text="Let me check."),
            ClaudeContentBlock(
                type="tool_use", tool_name="Read", tool_id="tu1",
                tool_input={"file_path": "/tmp/config.yaml"},
            ),
        ]
        entry = ClaudeEntry(
            type="assistant", uuid="u2", session_id="s1",
            timestamp="2026-03-15T10:00:02Z", cwd="/tmp/proj",
            content_blocks=blocks, content_text="Let me check.",
            model="claude-sonnet-4-20250514", role="assistant",
            usage=ClaudeUsage(input_tokens=100, output_tokens=20, cache_creation_input_tokens=500),
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.ASSISTANT
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "Read"
        assert msg.tool_calls[0].file_path == "/tmp/config.yaml"
        assert msg.tool_calls[0].operation_type == "read"
        assert msg.tokens is not None
        assert msg.tokens.input_tokens == 100
        assert msg.tokens.cache_creation == 500

    def test_tool_use_only_maps_to_tool_use_role(self):
        blocks = [
            ClaudeContentBlock(
                type="tool_use", tool_name="Bash", tool_id="tu1",
                tool_input={"command": "ls"},
            ),
        ]
        entry = ClaudeEntry(
            type="assistant", uuid="u3", session_id="s1",
            timestamp="2026-01-01T00:00:00Z", cwd="/tmp",
            content_blocks=blocks, content_text="", role="assistant",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg.role == MessageRole.TOOL_USE

    def test_skips_file_history_snapshot(self):
        entry = ClaudeEntry(type="file-history-snapshot", uuid="fhs1")
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is None

    def test_summary_entry(self):
        entry = ClaudeEntry(
            type="summary", uuid="s1", session_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            content_text="Summary of the session", is_sidechain=True,
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg.role == MessageRole.SUMMARY
        assert msg.is_sidechain is True

    def test_from_real_fixture(self):
        entries = self.parser.parse_file(FIXTURES_DIR / "claude_sample.jsonl")
        messages = [self.adapter.to_unified(e, "test") for e in entries]
        messages = [m for m in messages if m is not None]
        assert len(messages) == 8
        assert messages[0].role == MessageRole.USER
        assert messages[1].role == MessageRole.ASSISTANT


class TestClaudeAdapterToEvent:
    def setup_method(self):
        self.adapter = ClaudeAdapter()

    def test_user_event_summary(self):
        entry = ClaudeEntry(
            type="user", uuid="u1", session_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            content_text="Show me the config file",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "user"
        assert evt.summary == "Show me the config file"
        assert evt.provider == Provider.CLAUDE

    def test_tool_use_event(self):
        blocks = [
            ClaudeContentBlock(
                type="tool_use", tool_name="Bash", tool_id="tu1",
                tool_input={"command": "git status"},
            ),
        ]
        entry = ClaudeEntry(
            type="assistant", uuid="u2", session_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            content_blocks=blocks, content_text="",
            usage=ClaudeUsage(input_tokens=50, output_tokens=10),
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt.tool_name == "Bash"
        assert evt.tokens == {"input": 50, "output": 10}
        assert "git status" in evt.summary

    def test_skips_file_history_snapshot(self):
        entry = ClaudeEntry(type="file-history-snapshot", uuid="fhs1")
        evt = self.adapter.to_event(entry, "proj")
        assert evt is None

    def test_event_to_dict_roundtrip(self):
        entry = ClaudeEntry(
            type="user", uuid="u1", session_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            content_text="hello",
        )
        evt = self.adapter.to_event(entry, "proj")
        d = evt.to_dict()
        assert d["provider"] == "claude"
        assert d["event_type"] == "user"
        assert d["summary"] == "hello"
