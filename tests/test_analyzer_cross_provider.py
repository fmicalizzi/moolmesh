"""Tests for CrossProviderAnalyzer."""

from datetime import datetime, timezone

from hub.analyzers.cross_provider import CrossProviderAnalyzer
from hub.models.base import MessageRole, Provider, UnifiedMessage


def _msg(provider, role=MessageRole.USER, session_id="s1", model="model-1"):
    return UnifiedMessage(
        id="id-1", provider=provider, session_id=session_id,
        project="proj", role=role, text="test",
        timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        model=model,
    )


class TestCrossProviderAnalyzer:
    def setup_method(self):
        self.analyzer = CrossProviderAnalyzer()

    def test_name_and_title(self):
        assert self.analyzer.name == "10_cross_provider"

    def test_analyze_empty(self):
        result = self.analyzer.analyze([])
        assert result["total_providers"] == 0
        assert result["providers"] == {}

    def test_single_provider(self):
        msgs = [
            _msg(Provider.CLAUDE, MessageRole.USER),
            _msg(Provider.CLAUDE, MessageRole.ASSISTANT),
            _msg(Provider.CLAUDE, MessageRole.TOOL_USE),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total_providers"] == 1
        stats = result["providers"]["claude"]
        assert stats["messages"] == 3
        assert stats["user_messages"] == 1
        assert stats["tool_calls"] == 1
        assert stats["sessions"] == 1

    def test_multiple_providers(self):
        msgs = [
            _msg(Provider.CLAUDE, MessageRole.USER, "s1"),
            _msg(Provider.CODEX, MessageRole.USER, "s2"),
            _msg(Provider.OPENCODE, MessageRole.USER, "s3"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total_providers"] == 3
        assert "claude" in result["providers"]
        assert "codex" in result["providers"]
        assert "opencode" in result["providers"]

    def test_models_collected(self):
        msgs = [
            _msg(Provider.CLAUDE, model="claude-4"),
            _msg(Provider.CLAUDE, model="claude-3.5"),
        ]
        result = self.analyzer.analyze(msgs)
        models = result["providers"]["claude"]["models"]
        assert "claude-4" in models
        assert "claude-3.5" in models

    def test_sessions_counted(self):
        msgs = [
            _msg(Provider.CLAUDE, session_id="s1"),
            _msg(Provider.CLAUDE, session_id="s1"),
            _msg(Provider.CLAUDE, session_id="s2"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["providers"]["claude"]["sessions"] == 2

    def test_render_markdown(self):
        msgs = [
            _msg(Provider.CLAUDE),
            _msg(Provider.OPENCODE),
        ]
        result = self.analyzer.analyze(msgs)
        md = self.analyzer.render_markdown(result)
        assert "# Análisis Cross-Provider" in md
        assert "claude" in md
        assert "opencode" in md
        assert "Providers activos" in md
