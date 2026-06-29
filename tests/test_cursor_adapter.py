"""Tests for the Cursor adapter."""

from datetime import datetime

from hub.adapters.cursor_adapter import CursorAdapter
from hub.models.base import MessageRole, Provider
from hub.models.cursor import CursorBubble, CursorComposer


def _composer():
    return CursorComposer(
        composer_id="c1", name="Fix the parser", project="myproj",
        cwd="/home/u/dev/myproj", model="claude-4", created_at=1_700_000_000_000,
        last_updated_at=1_700_000_500_000, total_lines_added=12,
        total_lines_removed=3, files_changed_count=2, unified_mode="agent",
    )


def test_to_event_user_bubble():
    b = CursorBubble(composer_id="c1", bubble_id="b1", bubble_type=1,
                     text="please fix it", composer=_composer())
    adapter = CursorAdapter()
    ev = adapter.to_event(b, "myproj")
    assert ev is not None
    assert ev.provider == Provider.CURSOR
    assert ev.event_type == MessageRole.USER.value
    assert ev.summary == "please fix it"
    assert ev.cwd == "/home/u/dev/myproj"
    assert ev.session_id == "c1"
    assert ev.timestamp.startswith("2023-")  # derived from composer ms


def test_to_event_assistant_tokens_and_tool():
    b = CursorBubble(composer_id="c1", bubble_id="b2", bubble_type=2,
                     text="done", token_count=99, tool_name="edit_file",
                     file_path="hub/x.py", composer=_composer())
    ev = CursorAdapter().to_event(b, "myproj")
    assert ev.event_type == MessageRole.ASSISTANT.value
    assert ev.tokens == {"input": 0, "output": 99}
    assert ev.tool_name == "edit_file"
    assert ev.file_path == "hub/x.py"


def test_to_event_unknown_type_skipped():
    b = CursorBubble(composer_id="c1", bubble_id="b3", bubble_type=0, text="x")
    assert CursorAdapter().to_event(b, "myproj") is None


def test_to_session_meta_carries_stats():
    b = CursorBubble(composer_id="c1", bubble_id="b1", bubble_type=1,
                     text="hi", composer=_composer())
    meta = CursorAdapter().to_session_meta(b, "myproj")
    assert meta.id == "c1"
    assert meta.provider == Provider.CURSOR
    assert meta.title == "Fix the parser"
    assert meta.model == "claude-4"
    assert meta.metadata["total_lines_added"] == 12
    assert meta.metadata["files_changed_count"] == 2


def test_event_timestamp_falls_back_to_now_without_composer():
    b = CursorBubble(composer_id="c1", bubble_id="b1", bubble_type=1, text="hi")
    ts = CursorAdapter().event_timestamp(b)
    # Parseable ISO 8601, year >= 2024 (i.e. "now", not the epoch).
    assert datetime.fromisoformat(ts).year >= 2024
