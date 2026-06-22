"""Tests for OpenCode adapter."""

from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.models.base import MessageRole, Provider
from hub.models.opencode import OpenCodeEntry, OpenCodeToolCall


class TestOpenCodeAdapterToUnified:
    def setup_method(self):
        self.adapter = OpenCodeAdapter()

    def test_text_user_to_unified(self):
        entry = OpenCodeEntry(
            session_id="sess-1", message_id="msg-1",
            part_type="text", role="user", text="Fix the bug",
            timestamp="2026-06-01T10:00:00Z",
            model_id="mimo-v2.5", cwd="/Users/test/myapp",
            project_dir="/Users/test/myapp",
        )
        msg = self.adapter.to_unified(entry, "myapp")
        assert msg is not None
        assert msg.provider == Provider.OPENCODE
        assert msg.role == MessageRole.USER
        assert msg.text == "Fix the bug"
        assert msg.session_id == "sess-1"
        assert msg.project == "myapp"
        assert msg.model == "mimo-v2.5"

    def test_text_assistant_to_unified(self):
        entry = OpenCodeEntry(
            part_type="text", role="assistant", text="I'll fix it.",
            timestamp="2026-06-01T10:00:01Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.ASSISTANT

    def test_reasoning_to_thinking(self):
        entry = OpenCodeEntry(
            part_type="reasoning", role="assistant", text="Thinking...",
            timestamp="2026-06-01T10:00:02Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.THINKING

    def test_tool_to_tool_use(self):
        tc = OpenCodeToolCall(name="read", input_data={"path": "/src/app.py"}, tool_id="tc-1")
        entry = OpenCodeEntry(
            part_type="tool", role="assistant", text="file content",
            timestamp="2026-06-01T10:00:03Z", session_id="s1", message_id="m1",
            tool_call=tc,
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "read"
        assert msg.tool_calls[0].operation_type == "read"

    def test_file_part_to_tool_use(self):
        tc = OpenCodeToolCall(name="file_read", input_data={"path": "/src/lib.py"})
        entry = OpenCodeEntry(
            part_type="file", role="assistant", text="/src/lib.py",
            timestamp="2026-06-01T10:00:04Z", session_id="s1", message_id="m1",
            tool_call=tc,
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE

    def test_patch_to_tool_use(self):
        tc = OpenCodeToolCall(name="file_edit", input_data={"files": ["/src/app.py"]})
        entry = OpenCodeEntry(
            part_type="patch", role="assistant", text="[patch: /src/app.py]",
            timestamp="2026-06-01T10:00:05Z", session_id="s1", message_id="m1",
            tool_call=tc, files_affected=["/src/app.py"],
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.TOOL_USE
        assert msg.tool_calls[0].file_path == "/src/app.py"
        assert msg.tool_calls[0].operation_type == "write"

    def test_step_finish_tokens(self):
        entry = OpenCodeEntry(
            part_type="step-finish", role="assistant",
            timestamp="2026-06-01T10:00:06Z", session_id="s1", message_id="m1",
            token_input=1500, token_output=300, token_reasoning=100,
            token_cache_read=1000, token_cache_write=200,
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.SUMMARY
        assert msg.tokens is not None
        assert msg.tokens.input_tokens == 1500
        assert msg.tokens.output_tokens == 400  # output + reasoning
        assert msg.tokens.cache_read == 1000
        assert msg.tokens.cache_creation == 200

    def test_step_finish_zero_tokens_no_usage(self):
        entry = OpenCodeEntry(
            part_type="step-finish", role="assistant",
            timestamp="2026-06-01T10:00:06Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.tokens is None

    def test_compaction_to_summary(self):
        entry = OpenCodeEntry(
            part_type="compaction", role="assistant", text="Session summary.",
            timestamp="2026-06-01T10:00:07Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.role == MessageRole.SUMMARY

    def test_unknown_part_type_returns_none(self):
        entry = OpenCodeEntry(
            part_type="unknown_type", role="assistant",
            timestamp="2026-06-01T10:00:08Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is None

    def test_cwd_propagated(self):
        entry = OpenCodeEntry(
            part_type="text", role="user", text="test",
            cwd="/Users/test/myapp", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.cwd == "/Users/test/myapp"

    def test_timestamp_parsed(self):
        entry = OpenCodeEntry(
            part_type="text", role="user", text="test",
            timestamp="2026-06-01T10:00:00Z", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.timestamp is not None
        assert msg.timestamp.year == 2026

    def test_invalid_timestamp(self):
        entry = OpenCodeEntry(
            part_type="text", role="user", text="test",
            timestamp="not-a-date", session_id="s1", message_id="m1",
        )
        msg = self.adapter.to_unified(entry, "proj")
        assert msg is not None
        assert msg.timestamp is None


class TestOpenCodeAdapterToEvent:
    def setup_method(self):
        self.adapter = OpenCodeAdapter()

    def test_text_to_event(self):
        entry = OpenCodeEntry(
            part_type="text", role="user", text="Fix the bug",
            timestamp="2026-06-01T10:00:00Z", session_id="s1", message_id="m1",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.provider == Provider.OPENCODE
        assert evt.event_type == "user"
        assert evt.summary == "Fix the bug"

    def test_tool_event_has_tool_name(self):
        tc = OpenCodeToolCall(name="bash", input_data={"command": "ls -la"})
        entry = OpenCodeEntry(
            part_type="tool", role="assistant", text="output",
            timestamp="2026-06-01T10:00:01Z", session_id="s1", message_id="m1",
            tool_call=tc,
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is not None
        assert evt.tool_name == "bash"

    def test_unknown_type_returns_none(self):
        entry = OpenCodeEntry(
            part_type="unknown", role="assistant",
            timestamp="2026-06-01T10:00:02Z", session_id="s1", message_id="m1",
        )
        evt = self.adapter.to_event(entry, "proj")
        assert evt is None


class TestClassifyOperation:
    def test_read_ops(self):
        assert OpenCodeAdapter._classify_operation("read") == "read"
        assert OpenCodeAdapter._classify_operation("file_read") == "read"

    def test_write_ops(self):
        assert OpenCodeAdapter._classify_operation("write") == "write"
        assert OpenCodeAdapter._classify_operation("edit") == "write"
        assert OpenCodeAdapter._classify_operation("file_edit") == "write"

    def test_exec_ops(self):
        assert OpenCodeAdapter._classify_operation("bash") == "exec"

    def test_search_ops(self):
        assert OpenCodeAdapter._classify_operation("grep") == "search"
        assert OpenCodeAdapter._classify_operation("glob") == "search"
        assert OpenCodeAdapter._classify_operation("list") == "search"

    def test_other_ops(self):
        assert OpenCodeAdapter._classify_operation("webfetch") == "other"
        assert OpenCodeAdapter._classify_operation("question") == "other"
