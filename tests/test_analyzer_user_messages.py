"""Tests for UserMessagesAnalyzer."""

from datetime import datetime, timezone

from hub.analyzers.user_messages import UserMessagesAnalyzer
from hub.models.base import MessageRole, Provider, UnifiedMessage


def _msg(role, text="test", session_id="s1", provider=Provider.CLAUDE, project="proj"):
    return UnifiedMessage(
        id="id-1", provider=provider, session_id=session_id,
        project=project, role=role, text=text,
        timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
    )


class TestUserMessagesAnalyzer:
    def setup_method(self):
        self.analyzer = UserMessagesAnalyzer()

    def test_name_and_title(self):
        assert self.analyzer.name == "01_historico_mensajes_usuario"
        assert self.analyzer.title == "Histórico de Mensajes del Usuario"

    def test_analyze_empty(self):
        result = self.analyzer.analyze([])
        assert result["total"] == 0
        assert result["messages"] == []
        assert result["avg_length"] == 0
        assert result["by_session"] == {}

    def test_analyze_filters_user_only(self):
        msgs = [
            _msg(MessageRole.USER, "hello"),
            _msg(MessageRole.ASSISTANT, "hi"),
            _msg(MessageRole.TOOL_USE, "tool"),
            _msg(MessageRole.USER, "another"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total"] == 2
        assert len(result["messages"]) == 2

    def test_analyze_skips_empty_text(self):
        msgs = [
            _msg(MessageRole.USER, ""),
            _msg(MessageRole.USER, "   "),
            _msg(MessageRole.USER, "valid"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["total"] == 1

    def test_avg_length(self):
        msgs = [
            _msg(MessageRole.USER, "abcd"),
            _msg(MessageRole.USER, "abcdef"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["avg_length"] == 5  # (4+6)//2

    def test_by_session(self):
        msgs = [
            _msg(MessageRole.USER, "a", session_id="s1"),
            _msg(MessageRole.USER, "b", session_id="s1"),
            _msg(MessageRole.USER, "c", session_id="s2"),
        ]
        result = self.analyzer.analyze(msgs)
        assert result["by_session"]["s1"] == 2
        assert result["by_session"]["s2"] == 1

    def test_preserves_full_text(self):
        long_text = "x" * 5000
        msgs = [_msg(MessageRole.USER, long_text)]
        result = self.analyzer.analyze(msgs)
        assert len(result["messages"][0]["text"]) == 5000

    def test_compact_render_truncates_display(self):
        long_text = "x" * 5000
        analyzer = UserMessagesAnalyzer(complete=False)
        result = analyzer.analyze([_msg(MessageRole.USER, long_text)])
        md = analyzer.render_markdown(result)
        assert "xxxxx" in md
        assert "x" * 200 not in md

    def test_complete_render_shows_full(self):
        long_text = "x" * 5000
        analyzer = UserMessagesAnalyzer(complete=True)
        result = analyzer.analyze([_msg(MessageRole.USER, long_text)])
        md = analyzer.render_markdown(result)
        assert long_text in md

    def test_render_markdown(self):
        result = self.analyzer.analyze([_msg(MessageRole.USER, "test input")])
        md = self.analyzer.render_markdown(result)
        assert "# Histórico de Mensajes del Usuario" in md
        assert "test input" in md
        assert "Total mensajes del usuario" in md

    def test_render_empty(self):
        result = self.analyzer.analyze([])
        md = self.analyzer.render_markdown(result)
        assert "Total mensajes del usuario" in md
        assert "0" in md
