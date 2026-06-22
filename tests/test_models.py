"""Tests for base models."""

from hub.models.base import (
    MessageRole,
    Provider,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)


def test_provider_values():
    assert Provider.CLAUDE.value == "claude"
    assert Provider.CODEX.value == "codex"
    assert Provider.QWEN.value == "qwen"


def test_message_role_values():
    assert MessageRole.USER.value == "user"
    assert MessageRole.TOOL_USE.value == "tool_use"


def test_token_usage_total():
    t = TokenUsage(input_tokens=100, output_tokens=50)
    assert t.total == 150


def test_token_usage_total_with_cache():
    t = TokenUsage(input_tokens=100, output_tokens=50, cache_creation=200, cache_read=300)
    assert t.total_with_cache == 650


def test_tool_call_creation():
    tc = ToolCall(name="Bash", input_data={"command": "ls"}, file_path=None, operation_type="exec")
    assert tc.name == "Bash"
    assert tc.input_data == {"command": "ls"}


def test_unified_message_defaults():
    msg = UnifiedMessage(
        id="test", provider=Provider.CLAUDE, session_id="s1",
        project="proj", role=MessageRole.USER, text="hello",
    )
    assert msg.tool_calls == []
    assert msg.is_sidechain is False
    assert msg.raw is None


def test_unified_event_to_dict():
    evt = UnifiedEvent(
        provider=Provider.CLAUDE, project="my-project",
        event_type="user", timestamp="2026-03-15T10:00:00.000Z",
        summary="Show me the config", session_id="s1",
        tokens={"input": 100, "output": 50}, tool_name=None,
    )
    d = evt.to_dict()
    assert d["provider"] == "claude"
    assert d["project"] == "my-project"
    assert d["event_type"] == "user"
    assert d["tokens"] == {"input": 100, "output": 50}
    assert "tool_name" not in d  # None values excluded


def test_unified_event_to_dict_with_tool():
    evt = UnifiedEvent(
        provider=Provider.CLAUDE, project="proj",
        event_type="tool_use", timestamp="2026-01-01T00:00:00Z",
        summary="Bash: ls -la", tool_name="Bash",
        file_path="/tmp/test",
    )
    d = evt.to_dict()
    assert d["tool_name"] == "Bash"
    assert d["file_path"] == "/tmp/test"


def test_unified_event_to_json():
    evt = UnifiedEvent(
        provider=Provider.CLAUDE, project="proj",
        event_type="user", timestamp="2026-01-01T00:00:00Z",
        summary="test",
    )
    j = evt.to_json()
    assert '"provider": "claude"' in j
