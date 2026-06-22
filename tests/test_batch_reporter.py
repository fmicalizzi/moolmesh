"""Tests for batch_reporter module."""

from datetime import datetime, timezone

from hub.batch_reporter import _filter_by_time, _safe_dirname
from hub.models.base import MessageRole, Provider, UnifiedMessage


class TestSafeDirname:
    def test_simple_name(self):
        assert _safe_dirname("myproject") == "myproject"

    def test_slash_converted_to_dash(self):
        assert _safe_dirname("path/proj") == "path-proj"

    def test_special_chars_replaced(self):
        assert _safe_dirname("proj<name>") == "proj_name_"


class TestFilterByTime:
    def test_filters_old_messages(self):
        msgs = [
            UnifiedMessage(
                id="m1", provider=Provider.CLAUDE, session_id="s1",
                project="p", role=MessageRole.USER, text="old",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            UnifiedMessage(
                id="m2", provider=Provider.CLAUDE, session_id="s1",
                project="p", role=MessageRole.USER, text="new",
                timestamp=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
        ]
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = _filter_by_time(msgs, since)
        assert len(result) == 1
        assert result[0].text == "new"

    def test_skips_messages_without_timestamp(self):
        msgs = [
            UnifiedMessage(
                id="m1", provider=Provider.CLAUDE, session_id="s1",
                project="p", role=MessageRole.USER, text="no-ts",
                timestamp=None,
            ),
        ]
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = _filter_by_time(msgs, since)
        assert len(result) == 0

    def test_empty_list(self):
        result = _filter_by_time([], datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == []


class TestLoadProjectMessages:
    def test_loads_claude_messages_from_fixture(self):
        """Verify load_project_messages works with a real Claude fixture."""
        from hub.discovery import DiscoveredProject
        from hub.batch_reporter import load_project_messages
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures" / "claude_sample.jsonl"
        proj = DiscoveredProject(
            name="test",
            path="/tmp/test",
            provider=Provider.CLAUDE,
            session_dir=fixture.parent,
            session_files=[fixture],
            encoded_name="test",
        )
        msgs = load_project_messages(proj)
        assert len(msgs) > 0
        assert msgs[0].provider == Provider.CLAUDE
