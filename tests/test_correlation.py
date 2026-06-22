"""Tests para SessionCommitLinker."""
import pytest
from unittest.mock import MagicMock
from hub.correlation.linker import SessionCommitLinker


class TestLinkByCoauthor:
    def test_claude_detected(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"co_authors": ["Co-Authored-By: Claude <noreply@anthropic.com>"]}
        assert linker.link_by_coauthor(commit) is True

    def test_copilot_detected(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"co_authors": ["Co-Authored-By: GitHub Copilot"]}
        assert linker.link_by_coauthor(commit) is True

    def test_human_not_detected(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"co_authors": ["Co-Authored-By: John Smith <john@example.com>"]}
        assert linker.link_by_coauthor(commit) is False

    def test_no_coauthors(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"message": "normal commit", "co_authors": []}
        assert linker.link_by_coauthor(commit) is False

    def test_coauthor_from_message(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"message": "fix bug\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"}
        assert linker.link_by_coauthor(commit) is True


class TestLinkByIssueRef:
    def test_simple_refs(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"issue_refs": ["#42", "#45"]}
        assert linker.link_by_issue_ref(commit) == [42, 45]

    def test_refs_from_message(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"message": "fixes #42 and closes #45", "issue_refs": []}
        assert linker.link_by_issue_ref(commit) == [42, 45]

    def test_no_refs(self):
        linker = SessionCommitLinker(MagicMock(), MagicMock())
        commit = {"message": "normal commit", "issue_refs": []}
        assert linker.link_by_issue_ref(commit) == []


class TestLinkByTimestamp:
    def test_match_found(self):
        mock_event_store = MagicMock()
        mock_event_store.query.return_value = [
            {"timestamp": "2026-04-16T10:05:00",
             "cwd": "/tmp/test-repo/src",
             "session_id": "session-123"},
        ]

        linker = SessionCommitLinker(MagicMock(), mock_event_store)
        # Soportar tanto naive (nuevo) como aware (legacy)
        commit = {"timestamp": "2026-04-16T10:08:00"}

        result = linker.link_by_timestamp(commit, "/tmp/test-repo")
        assert result == "session-123"

    def test_no_match(self):
        mock_event_store = MagicMock()
        mock_event_store.query.return_value = []

        linker = SessionCommitLinker(MagicMock(), mock_event_store)
        commit = {"timestamp": "2026-04-16T10:00:00"}

        result = linker.link_by_timestamp(commit, "/tmp/test-repo")
        assert result is None


class TestRunBatch:
    def test_batch_stats(self):
        mock_git_store = MagicMock()
        mock_git_store.get_commits.return_value = [
            {"sha": "abc", "message": "fix #42\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
             "co_authors": ["Claude <noreply@anthropic.com>"],
             "issue_refs": ["#42"], "session_id": None, "timestamp": "2026-04-16T10:00:00"},
            {"sha": "def", "message": "update readme",
             "co_authors": [], "issue_refs": [], "session_id": None, "timestamp": "2026-04-16T11:00:00"},
        ]
        mock_event_store = MagicMock()
        mock_event_store.query.return_value = []

        linker = SessionCommitLinker(mock_git_store, mock_event_store)
        stats = linker.run_batch(1, "/tmp/test-repo")

        assert stats["processed"] == 2
        assert stats["ai_assisted"] == 1
        assert stats["issue_linked"] == 1
