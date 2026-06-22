"""Tests para L1 Stats computation."""
import pytest
from hub.cache.git_store import GitStore
from hub.digests.stats import compute_daily_stats, compute_weekly_stats


class TestDailyStats:
    def test_empty_day(self, tmp_path):
        """Día sin actividad retorna zeros."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        stats = compute_daily_stats(store, repo_id, "2026-04-16")

        assert stats["commits"] == 0
        assert stats["loc_added"] == 0
        assert stats["prs_merged"] == []
        assert stats["issues_closed"] == []
        store.close()

    def test_with_commits(self, tmp_path):
        """Día con commits retorna stats correctos."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc123", "author_name": "Franco", "author_email": "f@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "fix bug",
             "is_merge": False, "files": [
                 {"file_path": "main.py", "insertions": 10, "deletions": 3}
             ]},
        ])

        stats = compute_daily_stats(store, repo_id, "2026-04-16")

        assert stats["commits"] == 1
        assert stats["loc_added"] == 10
        assert stats["loc_removed"] == 3
        assert len(stats["authors"]) == 1
        store.close()

    def test_filters_by_date(self, tmp_path):
        """Solo cuenta commits del día pedido."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-15T10:00:00", "message": "yesterday"},
            {"sha": "def", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "today"},
        ])

        stats = compute_daily_stats(store, repo_id, "2026-04-16")
        assert stats["commits"] == 1
        store.close()


class TestWeeklyStats:
    def test_week_range(self, tmp_path):
        """Semana completa incluye lunes a domingo."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        # Monday and Friday commits
        store.store_commits(repo_id, [
            {"sha": "mon", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-13T10:00:00", "message": "monday"},
            {"sha": "fri", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-17T10:00:00", "message": "friday"},
        ])

        stats = compute_weekly_stats(store, repo_id, "2026-04-13")
        assert stats["commits"] == 2
        store.close()


def _mock_config():
    from hub.config import RepoConfig
    return RepoConfig(
        path="/tmp/test-repo",
        remote_url="github.com/test/repo",
        owner="test",
        repo="repo",
        added_at="2026-04-16T00:00:00",
    )
