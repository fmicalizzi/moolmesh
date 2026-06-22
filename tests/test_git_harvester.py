"""Tests for GitHarvester."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hub.harvesters.git_harvester import GitHarvester
from hub.cache.git_store import GitStore


@pytest.fixture
def store(tmp_path) -> GitStore:
    """Create a temporary GitStore."""
    db = tmp_path / "github.db"
    s = GitStore(db)
    yield s
    s.close()


class TestParseGitLog:
    def test_parse_git_log_basic(self, store):
        """output real parseado correctamente."""
        harvester = GitHarvester(store)
        
        output = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00|parent123|Initial commit
10	2	src/main.py
5	0	tests/test.py

"""
        
        commits = harvester._parse_git_log(output)
        
        assert len(commits) == 1
        assert commits[0]["sha"] == "abc123def456abc123def456abc123def456abc1"
        assert commits[0]["author_name"] == "John Doe"
        assert commits[0]["message"] == "Initial commit"
        assert commits[0]["files_changed"] == 2
        assert commits[0]["insertions"] == 15
        assert commits[0]["deletions"] == 2

    def test_parse_git_log_with_numstat(self, store):
        """archivos extraídos."""
        harvester = GitHarvester(store)
        
        output = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00||Test commit
100	50	src/large_file.py
-	-	binary_file.bin
"""
        
        commits = harvester._parse_git_log(output)
        
        assert len(commits) == 1
        assert len(commits[0]["files"]) == 2
        assert commits[0]["files"][0]["file_path"] == "src/large_file.py"
        assert commits[0]["files"][0]["insertions"] == 100
        assert commits[0]["files"][1]["insertions"] == 0  # Binary file

    def test_parse_git_log_merge_commit(self, store):
        """is_merge=True con 2 parents."""
        harvester = GitHarvester(store)
        
        output = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00|parent1 parent2|Merge pull request #1
5	5	src/file.py
"""
        
        commits = harvester._parse_git_log(output)
        
        assert len(commits) == 1
        assert commits[0]["is_merge"] is True


class TestExtractIssueRefs:
    def test_extract_issue_refs(self, store):
        """#42, fixes #45, closes #12."""
        harvester = GitHarvester(store)
        
        assert harvester._extract_issue_refs("Fix bug #42") == ["42"]
        assert harvester._extract_issue_refs("fixes #45") == ["45"]
        assert harvester._extract_issue_refs("closes #12") == ["12"]
        assert harvester._extract_issue_refs("resolves #100 and fixes #200") == ["100", "200"]

    def test_extract_issue_refs_empty(self, store):
        """mensaje sin refs."""
        harvester = GitHarvester(store)
        
        assert harvester._extract_issue_refs("Just a regular commit") == []
        assert harvester._extract_issue_refs("") == []


class TestDetectAICoauthor:
    def test_detect_ai_coauthor_claude(self, store):
        """Co-Authored-By: Claude."""
        harvester = GitHarvester(store)
        
        ai_assisted, co_authors = harvester._detect_ai_coauthor(
            "Some commit\n\nCo-Authored-By: Claude <claude@anthropic.com>"
        )
        
        assert ai_assisted is True
        assert len(co_authors) == 1

    def test_detect_ai_coauthor_copilot(self, store):
        """Co-Authored-By: GitHub Copilot."""
        harvester = GitHarvester(store)
        
        ai_assisted, co_authors = harvester._detect_ai_coauthor(
            "Some commit\n\nCo-Authored-By: GitHub Copilot <copilot@github.com>"
        )
        
        assert ai_assisted is True

    def test_detect_ai_coauthor_none(self, store):
        """sin co-author."""
        harvester = GitHarvester(store)
        
        ai_assisted, co_authors = harvester._detect_ai_coauthor(
            "Just a regular commit"
        )
        
        assert ai_assisted is False
        assert co_authors == []


class TestIngestHistory:
    @patch("hub.harvesters.git_harvester.git_log_since")
    @patch("hub.harvesters.git_harvester.get_remote_refs")
    def test_ingest_history(self, mock_refs, mock_log_since, store, tmp_path):
        """ingesta 14 días (mock subprocess)."""
        from hub.config import RepoConfig
        
        # Setup repo
        repo = RepoConfig(
            path=str(tmp_path / "repo"),
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        store.register_repo(repo)
        
        # Mock git log output
        mock_log_since.return_value = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00||Test commit
10	2	src/main.py
"""
        mock_refs.return_value = {"refs/remotes/origin/main": "abc123def456abc123def456abc123def456abc1"}
        
        harvester = GitHarvester(store)
        count = harvester.ingest_history(str(tmp_path / "repo"), days=14)
        
        assert count == 1
        assert store.count_commits(store.get_repo_id(str(tmp_path / "repo"))) == 1


class TestFetchAndIngest:
    @patch("hub.harvesters.git_harvester.git_fetch")
    @patch("hub.harvesters.git_harvester.get_remote_refs")
    @patch("hub.harvesters.git_harvester.git_log_range")
    def test_fetch_and_ingest_new_commits(self, mock_log_range, mock_refs, mock_fetch, store):
        """ref avanzó, commits ingestados."""
        from hub.config import RepoConfig
        
        repo = RepoConfig(
            path="/path/to/repo",
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        store.register_repo(repo)
        repo_id = store.get_repo_id("/path/to/repo")
        
        # Setup initial refs
        store.update_refs(repo_id, {"refs/remotes/origin/main": "old123sha456old123sha456old123sha456old123sha4"})
        
        # Mock fetch success
        mock_fetch.return_value = True
        
        # Mock new refs (branch advanced)
        mock_refs.return_value = {"refs/remotes/origin/main": "new123sha456new123sha456new123sha456new123sha4"}
        
        # Mock git log output
        mock_log_range.return_value = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00||New commit
10	2	src/main.py
"""
        
        harvester = GitHarvester(store)
        harvester._fetch_and_ingest(store.list_repos()[0])
        
        assert store.count_commits(repo_id) == 1

    @patch("hub.harvesters.git_harvester.git_fetch")
    @patch("hub.harvesters.git_harvester.get_remote_refs")
    def test_fetch_and_ingest_no_changes(self, mock_refs, mock_fetch, store):
        """refs iguales, nada nuevo."""
        from hub.config import RepoConfig
        
        repo = RepoConfig(
            path="/path/to/repo",
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        store.register_repo(repo)
        repo_id = store.get_repo_id("/path/to/repo")
        
        # Setup refs (same sha)
        same_sha = "same_sha" * 5
        store.update_refs(repo_id, {"refs/remotes/origin/main": same_sha})
        
        # Mock fetch success
        mock_fetch.return_value = True
        
        # Mock same refs
        mock_refs.return_value = {"refs/remotes/origin/main": same_sha}
        
        harvester = GitHarvester(store)
        harvester._fetch_and_ingest(store.list_repos()[0])
        
        # Should have no commits
        assert store.count_commits(repo_id) == 0


class TestIngestHistoryDays:
    """Tests for ingest_history with --days and --all."""

    @patch("hub.harvesters.git_harvester.git_log_all")
    @patch("hub.harvesters.git_harvester.get_remote_refs")
    def test_ingest_history_all(self, mock_refs, mock_log_all, store, tmp_path):
        """days=None llama git_log_all, no git_log_since."""
        from hub.config import RepoConfig
        from hub.harvesters.git_harvester import git_log_since

        # Setup repo
        repo = RepoConfig(
            path=str(tmp_path / "repo"),
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        store.register_repo(repo)

        # Mock git log output (no --since filter)
        mock_log_all.return_value = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00||Test commit
10	2	src/main.py
"""
        mock_refs.return_value = {"refs/remotes/origin/main": "abc123"}

        harvester = GitHarvester(store)
        count = harvester.ingest_history(str(tmp_path / "repo"), days=None)

        # Should call git_log_all, not git_log_since
        mock_log_all.assert_called_once()
        assert count == 1

    @patch("hub.harvesters.git_harvester.git_log_since")
    @patch("hub.harvesters.git_harvester.get_remote_refs")
    def test_ingest_history_days(self, mock_refs, mock_log_since, store, tmp_path):
        """days=30 llama git_log_since con since correcto."""
        from hub.config import RepoConfig

        # Setup repo
        repo = RepoConfig(
            path=str(tmp_path / "repo"),
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        store.register_repo(repo)

        # Mock git log output
        mock_log_since.return_value = """abc123def456abc123def456abc123def456abc1|John Doe|john@example.com|2026-04-10T10:00:00+00:00||Test commit
10	2	src/main.py
"""
        mock_refs.return_value = {"refs/remotes/origin/main": "abc123"}

        harvester = GitHarvester(store)
        count = harvester.ingest_history(str(tmp_path / "repo"), days=30)

        # Should call git_log_since with correct since date
        mock_log_since.assert_called_once()
        call_args = mock_log_since.call_args
        assert "2026-" in call_args[0][1]  # Check that since date is passed
        assert count == 1
