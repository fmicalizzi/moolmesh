"""Tests for Codex adapter."""

from hub.adapters.codex_adapter import CodexAdapter
from hub.models.base import MessageRole, Provider
from hub.models.codex import CodexEntry, CodexFunctionCall, CodexFunctionOutput


class TestCodexAdapterToUnified:
    def setup_method(self):
        self.adapter = CodexAdapter()

    def test_session_meta_to_system(self):
        entry = CodexEntry(
            event_type="session_meta", timestamp="2026-04-01T10:00:00Z",
            session_id="abc123", cwd="/Users/test/project",
            cli_version="0.98.0", model_provider="openai", source="vscode",
        )
        msg = self.adapter.to_unified(entry, "test-project")
        assert msg is not None
        assert msg.provider == Provider.CODEX
        assert msg.role == MessageRole.SYSTEM
        assert msg.session_id == "abc123"
        assert msg.cwd == "/Users/test/project"
        assert msg.model == "openai"
        assert msg.project == "test-project"

    def test_event_msg_to_user(self):
        entry = CodexEntry(
            event_type="event_msg", timestamp="2026-04-01T10:00:05Z",
            event_msg_text="fix the bug", role="user",
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.USER
        assert msg.text == "fix the bug"

    def test_response_item_message_to_assistant(self):
        entry = CodexEntry(
            event_type="response_item", timestamp="2026-04-01T10:00:10Z",
            payload_type="message", role="assistant", text="I'll fix it",
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.ASSISTANT
        assert "I'll fix it" in msg.text

    def test_function_call_to_tool_use(self):
        fc = CodexFunctionCall(call_id="fc1", name="shell", arguments='{"command":"git diff"}')
        entry = CodexEntry(
            event_type="response_item", timestamp="2026-04-01T10:00:15Z",
            payload_type="function_call", function_call=fc,
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "shell"
        assert msg.tool_calls[0].operation_type == "exec"

    def test_function_output_to_tool_result(self):
        fo = CodexFunctionOutput(call_id="fc1", output="diff output")
        entry = CodexEntry(
            event_type="response_item", timestamp="2026-04-01T10:00:20Z",
            payload_type="function_call_output", function_output=fo,
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_RESULT

    def test_reasoning_to_thinking(self):
        entry = CodexEntry(
            event_type="response_item", timestamp="2026-04-01T10:00:25Z",
            payload_type="reasoning", reasoning_text="thinking about the bug",
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.THINKING

    def test_token_count_to_summary(self):
        entry = CodexEntry(
            event_type="token_count", timestamp="2026-04-01T10:00:30Z",
            token_input=1200, token_output=350, token_cached_input=800,
            token_reasoning=120, token_total=2470,
            session_id="abc123", cwd="/Users/test/project",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.SUMMARY
        assert msg.tokens is not None
        assert msg.tokens.input_tokens == 1200
        assert msg.tokens.output_tokens == 470  # output + reasoning
        assert msg.tokens.cache_read == 800

    def test_session_context_propagated_to_unified(self):
        """Verify that session_id and cwd from entry are passed to UnifiedMessage."""
        entry = CodexEntry(
            event_type="event_msg", timestamp="2026-04-01T10:00:05Z",
            event_msg_text="test", role="user",
            session_id="propagated-id", cwd="/propagated/path",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.session_id == "propagated-id"
        assert msg.cwd == "/propagated/path"

    def test_from_real_fixture(self):
        from hub.parsers.codex_parser import CodexParser
        from pathlib import Path
        fixture = Path(__file__).parent / "fixtures" / "codex_sample.jsonl"
        parser = CodexParser()
        entries = parser.parse_file(fixture)
        messages = [self.adapter.to_unified(e, "test") for e in entries]
        messages = [m for m in messages if m is not None]
        assert len(messages) == 7
        # All should have session_id and cwd from the stateful parser
        for m in messages:
            assert m.session_id == "abc123-def456"
            assert m.cwd == "/Users/test/project"


class TestCodexAdapterToEvent:
    def setup_method(self):
        self.adapter = CodexAdapter()

    def test_session_meta_event(self):
        entry = CodexEntry(
            event_type="session_meta", timestamp="2026-04-01T10:00:00Z",
            session_id="abc123", cwd="/Users/test/project",
            cli_version="0.98.0",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "system"
        assert evt.provider == Provider.CODEX
        assert "session start" in evt.summary

    def test_event_msg_to_user_event(self):
        entry = CodexEntry(
            event_type="event_msg", timestamp="2026-04-01T10:00:05Z",
            event_msg_text="fix the bug", role="user",
            session_id="abc123", cwd="/Users/test/project",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "user"
        assert evt.summary == "fix the bug"

    def test_token_count_event(self):
        entry = CodexEntry(
            event_type="token_count", timestamp="2026-04-01T10:00:30Z",
            token_input=1200, token_output=350, token_cached_input=800,
            token_reasoning=120, token_total=2470,
            session_id="abc123", cwd="/Users/test/project",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "summary"
        assert evt.tokens is not None
        assert evt.tokens["input"] == 1200
        assert evt.tokens["output"] == 350

    def test_function_call_event(self):
        fc = CodexFunctionCall(call_id="fc1", name="shell", arguments='{"command":"git diff"}')
        entry = CodexEntry(
            event_type="response_item", timestamp="2026-04-01T10:00:15Z",
            payload_type="function_call", function_call=fc,
            session_id="abc123", cwd="/Users/test/project",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.event_type == "tool_use"
        assert evt.tool_name == "shell"
        assert "git diff" in evt.file_path


class TestCodexAdapterEdgeCases:
    def test_function_call_malformed_arguments(self):
        """Function call with invalid JSON arguments should not crash."""
        entry = CodexEntry(
            event_type="response_item",
            payload_type="function_call",
            function_call=CodexFunctionCall(
                call_id="fc-bad",
                name="shell",
                arguments="esto no es json {{{",
            ),
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE
        assert len(msg.tool_calls) == 1
        # Fallback: arguments stored as raw string
        assert msg.tool_calls[0].input_data == {"raw": "esto no es json {{{"}


class TestCodexAdapterSystemPrompts:
    """Verify system prompts are not classified as user messages."""

    def test_developer_role_maps_to_system(self):
        """Messages with role='developer' should be SYSTEM, not USER."""
        entry = CodexEntry(
            event_type="response_item",
            payload_type="message",
            role="developer",
            text="You are a coding assistant. Follow these rules...",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM

    def test_event_msg_with_xml_system_prompt(self):
        """event_msg containing <permissions> should be SYSTEM."""
        entry = CodexEntry(
            event_type="event_msg",
            event_msg_text="<permissions>\nYou can read and write files.\n</permissions>",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM

    def test_event_msg_with_skills_instructions(self):
        """event_msg containing <skills_instructions> should be SYSTEM."""
        entry = CodexEntry(
            event_type="event_msg",
            event_msg_text="<skills_instructions>\nUse these tools...\n</skills_instructions>",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM

    def test_event_msg_with_environment_context(self):
        """event_msg containing <environment_context> should be SYSTEM."""
        entry = CodexEntry(
            event_type="event_msg",
            event_msg_text="<environment_context>\nOS: Linux\nShell: bash\n</environment_context>",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM

    def test_real_user_message_stays_user(self):
        """Normal user input should remain USER."""
        entry = CodexEntry(
            event_type="event_msg",
            event_msg_text="fix the login bug in auth.py",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.USER

    def test_short_you_are_stays_user(self):
        """Short message starting with 'You are' should stay USER (not system)."""
        entry = CodexEntry(
            event_type="event_msg",
            event_msg_text="You are wrong about the API endpoint",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.USER

    def test_response_item_user_with_system_content(self):
        """response_item with role='user' but system content should be SYSTEM."""
        entry = CodexEntry(
            event_type="response_item",
            payload_type="message",
            role="user",
            text="<permissions>\nFull access granted.\n</permissions>",
        )
        adapter = CodexAdapter()
        msg = adapter.to_unified(entry, "test")
        assert msg is not None
        assert msg.role == MessageRole.SYSTEM
