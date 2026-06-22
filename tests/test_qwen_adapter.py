"""Tests for Qwen adapter."""

from pathlib import Path

from hub.adapters.qwen_adapter import QwenAdapter
from hub.models.base import MessageRole, Provider
from hub.models.qwen import QwenEntry, QwenFunctionCall, QwenFunctionResponse, QwenUsage


class TestQwenAdapterToUnified:
    def setup_method(self):
        self.adapter = QwenAdapter()

    def test_user_message(self):
        entry = QwenEntry(
            type="user", uuid="u1", session_id="s1",
            timestamp="2026-04-01T10:00:00Z", cwd="/tmp/proj",
            text="explain the code",
        )
        msg = self.adapter.to_unified(entry, "test-project")
        assert msg is not None
        assert msg.provider == Provider.QWEN
        assert msg.role == MessageRole.USER
        assert msg.text == "explain the code"
        assert msg.project == "test-project"
        assert msg.id == "u1"

    def test_assistant_with_text(self):
        entry = QwenEntry(
            type="assistant", uuid="a1", session_id="s1",
            timestamp="2026-04-01T10:00:05Z", cwd="/tmp/proj",
            text="Here is the answer.", model="qwen-coder-plus",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.ASSISTANT
        assert msg.model == "qwen-coder-plus"

    def test_assistant_with_thought(self):
        entry = QwenEntry(
            type="assistant", uuid="a2", session_id="s1",
            timestamp="2026-04-01T10:00:10Z", cwd="/tmp/proj",
            text="thinking", has_thought=True,
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.ASSISTANT

    def test_function_call_to_tool_use(self):
        entry = QwenEntry(
            type="assistant", uuid="a3", session_id="s1",
            timestamp="2026-04-01T10:00:15Z", cwd="/tmp/proj",
            text="",
            function_calls=[QwenFunctionCall(
                call_id="fc1", name="run_shell_command",
                args={"command": "git status"},
            )],
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "run_shell_command"
        assert msg.tool_calls[0].operation_type == "exec"

    def test_tool_result(self):
        entry = QwenEntry(
            type="tool_result", uuid="tr1", session_id="s1",
            timestamp="2026-04-01T10:00:20Z", cwd="/tmp/proj",
            function_responses=[QwenFunctionResponse(
                call_id="fc1", name="run_shell_command",
                output="success",
            )],
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_RESULT

    def test_system_event(self):
        entry = QwenEntry(
            type="system", uuid="sys1", session_id="s1",
            timestamp="2026-04-01T10:00:25Z", cwd="/tmp/proj",
            text="instructions", subtype="instructions",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM

    def test_usage_mapped(self):
        entry = QwenEntry(
            type="assistant", uuid="a4", session_id="s1",
            timestamp="2026-04-01T10:00:30Z", cwd="/tmp/proj",
            text="answer",
            usage=QwenUsage(
                prompt_tokens=100, candidates_tokens=50,
                thoughts_tokens=10, total_tokens=160,
            ),
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.tokens is not None
        assert msg.tokens.input_tokens == 100
        assert msg.tokens.output_tokens == 50

    def test_from_real_fixture(self):
        from hub.parsers.qwen_parser import QwenParser
        fixture = FIXTURES_DIR / "qwen_sample.jsonl"
        parser = QwenParser()
        entries = parser.parse_file(fixture)
        messages = [self.adapter.to_unified(e, "test") for e in entries]
        messages = [m for m in messages if m is not None]
        assert len(messages) == 6


class TestQwenAdapterToEvent:
    def setup_method(self):
        self.adapter = QwenAdapter()

    def test_user_event(self):
        entry = QwenEntry(
            type="user", uuid="u1", session_id="s1",
            timestamp="2026-04-01T10:00:00Z",
            text="explain the code",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "user"
        assert evt.provider == Provider.QWEN
        assert evt.summary == "explain the code"

    def test_function_call_event(self):
        entry = QwenEntry(
            type="assistant", uuid="a1", session_id="s1",
            timestamp="2026-04-01T10:00:05Z",
            function_calls=[QwenFunctionCall(
                call_id="fc1", name="run_shell_command",
                args={"command": "ls -la"},
            )],
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "tool_use"
        assert evt.tool_name == "run_shell_command"
        assert "ls -la" in evt.file_path

    def test_tool_result_event(self):
        entry = QwenEntry(
            type="tool_result", uuid="tr1", session_id="s1",
            timestamp="2026-04-01T10:00:10Z",
            function_responses=[QwenFunctionResponse(
                call_id="fc1", name="test", output="ok",
            )],
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "tool_result"

    def test_system_event(self):
        entry = QwenEntry(
            type="system", uuid="sys1", session_id="s1",
            timestamp="2026-04-01T10:00:15Z",
            text="instructions", subtype="instructions",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "system"

    def test_unknown_type_returns_none(self):
        entry = QwenEntry(
            type="unknown_type", uuid="x",
            timestamp="2026-04-01T10:00:00Z",
            text="test",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is None


FIXTURES_DIR = Path(__file__).parent / "fixtures"
