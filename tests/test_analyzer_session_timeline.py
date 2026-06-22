"""Tests for SessionTimelineAnalyzer."""

from datetime import datetime, timezone, timedelta

from hub.analyzers.session_timeline import SessionTimelineAnalyzer
from hub.models.base import MessageRole, Provider, UnifiedMessage


def _msg(session_id="s1", role=MessageRole.USER, ts_offset_min=0, provider=Provider.CLAUDE, model="claude-4"):
    return UnifiedMessage(
        id=f"id-{ts_offset_min}", provider=provider, session_id=session_id,
        project="proj", role=role, text="test",
        timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=ts_offset_min),
        model=model,
        cwd="/Users/test/proj",
    )


class TestSessionTimelineAnalyzer:
    def setup_method(self):
        self.analyzer = SessionTimelineAnalyzer()

    def test_name_and_title(self):
        assert self.analyzer.name == "02_timeline_sesiones"

    def test_analyze_empty(self):
        result = self.analyzer.analyze([])
        assert result["total_sessions"] == 0
        assert result["total_duration_min"] == 0
        assert result["sessions"] == []

    def test_single_session(self):
        msgs = [
            _msg("s1", MessageRole.USER, 0),
            _msg("s1", MessageRole.ASSISTANT, 5),
            _msg("s1", MessageRole.TOOL_USE, 10),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total_sessions"] == 1
        s = result["sessions"][0]
        assert s["session_id"] == "s1"
        assert s["messages"] == 3
        assert s["user_messages"] == 1
        assert s["tool_calls"] == 1
        assert s["duration_min"] == 10

    def test_multiple_sessions(self):
        msgs = [
            _msg("s1", MessageRole.USER, 0),
            _msg("s1", MessageRole.ASSISTANT, 30),
            _msg("s2", MessageRole.USER, 60),
            _msg("s2", MessageRole.ASSISTANT, 90),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total_sessions"] == 2
        assert result["total_duration_min"] == 60  # 30 + 30

    def test_sessions_sorted_by_start_desc(self):
        msgs = [
            _msg("s1", MessageRole.USER, 0),
            _msg("s2", MessageRole.USER, 60),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["sessions"][0]["session_id"] == "s2"
        assert result["sessions"][1]["session_id"] == "s1"

    def test_provider_in_session(self):
        msgs = [_msg("s1", MessageRole.USER, 0, provider=Provider.OPENCODE)]
        result = self.analyzer.analyze(msgs)
        assert result["sessions"][0]["provider"] == "opencode"

    def test_model_in_session(self):
        msgs = [_msg("s1", MessageRole.USER, 0, model="mimo-v2.5")]
        result = self.analyzer.analyze(msgs)
        assert result["sessions"][0]["model"] == "mimo-v2.5"

    def test_render_markdown(self):
        msgs = [
            _msg("s1", MessageRole.USER, 0),
            _msg("s1", MessageRole.ASSISTANT, 15),
        ]
        result = self.analyzer.analyze(msgs)
        md = self.analyzer.render_markdown(result)
        assert "# Timeline de Sesiones" in md
        assert "Total sesiones" in md
        assert "15m" in md
