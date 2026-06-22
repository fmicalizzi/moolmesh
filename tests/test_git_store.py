"""Tests for GitStore (SQLite persistence for Git data)."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hub.cache.git_store import GitStore


@pytest.fixture
def store(tmp_path) -> GitStore:
    """Create a temporary GitStore."""
    db = tmp_path / "github.db"
    s = GitStore(db)
    yield s
    s.close()


class TestCreateSchema:
    def test_all_tables_created(self, store):
        """Todas las tablas se crean."""
        with store._lock:
            cursor = store._get_conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor.fetchall()}
        
        expected = {
            "repos", "git_refs", "git_commits", "commit_files",
            "github_issues", "github_milestones", "github_project_items",
            "daily_digests", "api_cache"
        }
        assert expected.issubset(tables)


class TestRegisterRepo:
    def test_register_repo(self, store):
        """Insert y get_repo_id."""
        from hub.config import RepoConfig
        
        repo = RepoConfig(
            path="/path/to/repo",
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        
        repo_id = store.register_repo(repo)
        assert repo_id is not None
        
        # Should be able to retrieve
        retrieved_id = store.get_repo_id("/path/to/repo")
        assert retrieved_id == repo_id

    def test_register_duplicate_updates(self, store):
        """UNIQUE constraint - reemplaza."""
        from hub.config import RepoConfig
        
        repo1 = RepoConfig(
            path="/path/to/repo",
            remote_url="github.com/owner/repo",
            owner="owner",
            repo="repo",
            added_at="2026-04-10T10:00:00",
            github_enabled=True,
        )
        
        repo2 = RepoConfig(
            path="/path/to/repo",
            remote_url="github.com/owner/repo2",
            owner="owner",
            repo="repo2",
            added_at="2026-04-10T11:00:00",
            github_enabled=True,
        )
        
        id1 = store.register_repo(repo1)
        id2 = store.register_repo(repo2)
        
        # Should be same ID (REPLACE)
        repos = store.list_repos()
        assert len(repos) == 1
        assert repos[0]["repo_name"] == "repo2"


class TestListRepos:
    def test_list_repos(self, store):
        """Retorna todos."""
        from hub.config import RepoConfig
        
        for i in range(3):
            repo = RepoConfig(
                path=f"/path/to/repo{i}",
                remote_url=f"github.com/owner/repo{i}",
                owner="owner",
                repo=f"repo{i}",
                added_at=f"2026-04-10T10:00:0{i}",
                github_enabled=True,
            )
            store.register_repo(repo)
        
        repos = store.list_repos()
        assert len(repos) == 3


class TestRemoveRepo:
    def test_remove_repo(self, store):
        """delete + cascade."""
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
        
        # Add a commit to test cascade
        repo_id = store.get_repo_id("/path/to/repo")
        store.store_commits(repo_id, [{
            "sha": "abc123",
            "author_name": "Test",
            "author_email": "test@test.com",
            "timestamp": "2026-04-10T10:00:00",
            "message": "Test commit",
            "files": [],
        }])
        
        removed = store.remove_repo("/path/to/repo")
        assert removed is True
        
        # Repo and commits should be gone
        assert store.get_repo_id("/path/to/repo") is None
        assert store.count_commits(repo_id) == 0


class TestStoreCommits:
    def test_store_commits_with_dedup(self, store):
        """insert con dedup."""
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
        
        commits = [
            {
                "sha": "abc123" * 4,  # 40 chars
                "author_name": "Test User",
                "author_email": "test@test.com",
                "timestamp": "2026-04-10T10:00:00",
                "message": "First commit",
                "files": [],
            },
            {
                "sha": "def456" * 4,
                "author_name": "Test User",
                "author_email": "test@test.com",
                "timestamp": "2026-04-10T10:01:00",
                "message": "Second commit",
                "files": [],
            }
        ]
        
        stored = store.store_commits(repo_id, commits)
        assert stored == 2
        
        # Try to store duplicates
        stored2 = store.store_commits(repo_id, commits)
        assert stored2 == 0  # No new commits

    def test_store_commits_with_files(self, store):
        """insert en commit_files."""
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
        
        commits = [{
            "sha": "abc123" * 4,
            "author_name": "Test User",
            "author_email": "test@test.com",
            "timestamp": "2026-04-10T10:00:00",
            "message": "Test commit",
            "files": [
                {"file_path": "src/main.py", "insertions": 10, "deletions": 2},
                {"file_path": "tests/test.py", "insertions": 5, "deletions": 0},
            ],
        }]
        
        store.store_commits(repo_id, commits)
        
        # Check hot files
        hot_files = store.get_hot_files(repo_id)
        assert len(hot_files) == 2
        assert hot_files[0]["changes"] == 1


class TestGetCommits:
    def test_get_commits_since(self, store):
        """filtro por timestamp."""
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
        
        commits = [
            {
                "sha": f"abc{i:03d}" * 8,
                "author_name": "Test",
                "author_email": "test@test.com",
                "timestamp": f"2026-04-{i:02d}T10:00:00",
                "message": f"Commit {i}",
                "files": [],
            }
            for i in range(1, 6)
        ]
        
        store.store_commits(repo_id, commits)
        
        # Get commits since April 4
        result = store.get_commits(repo_id, since="2026-04-04T00:00:00")
        assert len(result) == 2  # Commits on 04 and 05


class TestAuthorStats:
    def test_get_author_stats(self, store):
        """GROUP BY correcto."""
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
        
        commits = [
            {
                "sha": "abc123" * 4,
                "author_name": "Alice",
                "author_email": "alice@test.com",
                "timestamp": "2026-04-10T10:00:00",
                "message": "Commit 1",
                "files": [{"file_path": "a.py", "insertions": 10, "deletions": 5}],
            },
            {
                "sha": "def456" * 4,
                "author_name": "Bob",
                "author_email": "bob@test.com",
                "timestamp": "2026-04-10T10:01:00",
                "message": "Commit 2",
                "files": [{"file_path": "b.py", "insertions": 20, "deletions": 10}],
            },
            {
                "sha": "ghi789" * 4,
                "author_name": "Alice",
                "author_email": "alice@test.com",
                "timestamp": "2026-04-10T10:02:00",
                "message": "Commit 3",
                "files": [{"file_path": "c.py", "insertions": 5, "deletions": 2}],
            }
        ]
        
        store.store_commits(repo_id, commits)
        
        stats = store.get_author_stats(repo_id)
        assert len(stats) == 2
        
        alice = next(s for s in stats if s["author_name"] == "Alice")
        assert alice["commits"] == 2
        assert alice["insertions"] == 15


class TestHotFiles:
    def test_get_hot_files(self, store):
        """JOIN + GROUP BY."""
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
        
        commits = [
            {
                "sha": f"abc{i:03d}" * 8,
                "author_name": "Test",
                "author_email": "test@test.com",
                "timestamp": "2026-04-10T10:00:00",
                "message": f"Commit {i}",
                "files": [
                    {"file_path": "hot.py", "insertions": 10, "deletions": 2},
                ] + ([{"file_path": "cold.py", "insertions": 1, "deletions": 0}] if i < 2 else []),
            }
            for i in range(5)
        ]
        
        store.store_commits(repo_id, commits)
        
        hot_files = store.get_hot_files(repo_id)
        # hot.py appears in all 5 commits, cold.py only in 2
        assert hot_files[0]["file_path"] == "hot.py"
        assert hot_files[0]["changes"] == 5
        assert hot_files[1]["file_path"] == "cold.py"
        assert hot_files[1]["changes"] == 2


class TestRefs:
    def test_refs_update_and_get(self, store):
        """roundtrip."""
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
        
        refs = {
            "refs/remotes/origin/main": "abc123" * 4,
            "refs/remotes/origin/develop": "def456" * 4,
        }
        
        store.update_refs(repo_id, refs)
        retrieved = store.get_refs(repo_id)
        
        assert retrieved == refs


class TestThreadSafety:
    def test_concurrent_writes(self, store):
        """concurrent access con Lock."""
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
        
        errors = []
        
        def write_commits(thread_id):
            try:
                for i in range(10):
                    commit = {
                        "sha": f"{thread_id:02d}{i:02d}" + "a" * 36,
                        "author_name": f"User{thread_id}",
                        "author_email": f"user{thread_id}@test.com",
                        "timestamp": f"2026-04-10T10:{thread_id:02d}:{i:02d}",
                        "message": f"Commit {thread_id}-{i}",
                        "files": [],
                    }
                    store.store_commits(repo_id, [commit])
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=write_commits, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert errors == [], f"Concurrent writes failed: {errors}"
        assert store.count_commits(repo_id) == 50  # 5 threads * 10 commits


class TestMigrations:
    """Tests for the versioned migrations system."""

    def test_migrations_run_once(self, tmp_path):
        """Después de GitStore() × 2, schema_migrations tiene exactamente 2 filas."""
        db = tmp_path / "test_migrations.db"

        # First init
        store1 = GitStore(db)
        with store1._lock:
            cursor = store1._get_conn().execute("SELECT version FROM schema_migrations")
            first_versions = {row[0] for row in cursor.fetchall()}
        store1.close()

        # Second init (simulate restart)
        store2 = GitStore(db)
        with store2._lock:
            cursor = store2._get_conn().execute("SELECT version FROM schema_migrations")
            second_versions = {row[0] for row in cursor.fetchall()}
        store2.close()

        assert first_versions == second_versions
        assert len(second_versions) == 3  # 3 migrations registered

    def test_migration_1_converts_local(self, tmp_path):
        """Commit con -06:00 se convierte a UTC en migration 1 (mig 3 luego lo pasa a local naive)."""
        from hub.cache.git_store import _mig_1_normalize_timestamps
        from hub.config import RepoConfig

        db = tmp_path / "test_migration_conv.db"
        store = GitStore(db)

        # Register a repo first (required for foreign key constraint)
        repo = RepoConfig(
            path="/tmp/test", remote_url="github.com/t/t", owner="t", repo="t",
            added_at="2026-04-10T10:00:00", github_enabled=True,
        )
        repo_id = store.register_repo(repo)

        # Insert directly to bypass normalization
        with store._lock:
            conn = store._get_conn()
            conn.execute(
                "INSERT INTO git_commits (repo_id, sha, author_name, author_email, timestamp, message, is_merge) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, "abc123", "Test", "t@test.com", "2026-04-10T10:00:00-06:00", "Test", 0)
            )
            conn.commit()

            # Run migration directly
            updated = _mig_1_normalize_timestamps(conn)
            assert updated == 1

            # Verify conversion to UTC (mig 3 handles local naive conversion separately)
            cursor = conn.execute("SELECT timestamp FROM git_commits WHERE sha='abc123'")
            new_ts = cursor.fetchone()[0]
            assert new_ts == "2026-04-10T16:00:00+00:00"

        store.close()

    def test_migration_3_utc_to_local(self, tmp_path):
        """Migration 3: timestamps UTC se convierten a hora local naive."""
        from hub.cache.git_store import _mig_3_utc_to_local
        from hub.config import RepoConfig

        db = tmp_path / "test_mig3.db"
        store = GitStore(db)

        repo = RepoConfig(
            path="/tmp/test3", remote_url="github.com/t/t3", owner="t", repo="t3",
            added_at="2026-04-10T10:00:00", github_enabled=True,
        )
        repo_id = store.register_repo(repo)

        # Insert UTC timestamp directly (simulates legacy data after mig 1)
        with store._lock:
            conn = store._get_conn()
            conn.execute(
                "INSERT INTO git_commits (repo_id, sha, author_name, author_email, timestamp, message, is_merge) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, "utc123", "Test", "t@test.com", "2026-04-10T16:00:00+00:00", "Test", 0)
            )
            conn.commit()

            updated = _mig_3_utc_to_local(conn)
            assert updated == 1

            cursor = conn.execute("SELECT timestamp FROM git_commits WHERE sha='utc123'")
            new_ts = cursor.fetchone()[0]
            # Debe ser local naive (sin +00:00, sin Z)
            assert "+" not in new_ts
            assert new_ts.endswith("Z") is False
            assert "T" in new_ts  # Still ISO format

        store.close()

    def test_list_cached_digests_max_level(self, store):
        """L2 + L3 misma fecha → retorna L3."""
        from hub.config import RepoConfig
        from datetime import datetime, timezone

        repo = RepoConfig(
            path="/path/to/repo", remote_url="github.com/o/r", owner="o", repo="r",
            added_at="2026-04-10T10:00:00", github_enabled=True,
        )
        repo_id = store.register_repo(repo)
        date = "2026-04-17"
        now = datetime.now(timezone.utc).isoformat()

        # Store L2 first, then L3
        store.store_digest(repo_id, date, "daily", 2, {"level": 2})
        store.store_digest(repo_id, date, "daily", 3, {"level": 3})

        digests = store.list_cached_digests(repo_id, "daily", 30)
        assert len(digests) == 1
        assert digests[0]["level"] == 3  # Should return max level


class TestCommitDays:
    """Tests for get_commit_days method (T7)."""

    def test_get_commit_days_returns_distinct_dates(self, store):
        """get_commit_days retorna fechas distintas con commits."""
        from hub.config import RepoConfig

        repo = RepoConfig(
            path="/path/to/repo", remote_url="github.com/o/r", owner="o", repo="r",
            added_at="2026-04-10T10:00:00", github_enabled=True,
        )
        repo_id = store.register_repo(repo)

        # Insertar commits en diferentes días
        commits = [
            {
                "sha": "abc123",
                "author_name": "Alice",
                "author_email": "alice@example.com",
                "timestamp": "2026-04-15T10:00:00",
                "message": "Commit 1",
                "files": [],
            },
            {
                "sha": "def456",
                "author_name": "Bob",
                "author_email": "bob@example.com",
                "timestamp": "2026-04-15T15:00:00",  # Mismo día
                "message": "Commit 2",
                "files": [],
            },
            {
                "sha": "ghi789",
                "author_name": "Charlie",
                "author_email": "charlie@example.com",
                "timestamp": "2026-04-17T09:00:00",  # Día diferente
                "message": "Commit 3",
                "files": [],
            },
        ]
        store.store_commits(repo_id, commits)

        # Debe retornar solo 2 días distintos (15 y 17)
        days = store.get_commit_days(repo_id, limit=30)
        assert len(days) == 2
        assert "2026-04-15" in days
        assert "2026-04-17" in days
        assert "2026-04-16" not in days  # No hay commits este día

    def test_get_commit_days_respects_limit(self, store):
        """get_commit_days respeta el parámetro limit."""
        from hub.config import RepoConfig

        repo = RepoConfig(
            path="/path/to/repo", remote_url="github.com/o/r", owner="o", repo="r",
            added_at="2026-04-10T10:00:00", github_enabled=True,
        )
        repo_id = store.register_repo(repo)

        # Insertar commits en 5 días diferentes
        commits = []
        for i in range(5):
            commits.append({
                "sha": f"sha{i}",
                "author_name": "Alice",
                "author_email": "alice@example.com",
                "timestamp": f"2026-04-{10+i}T10:00:00",
                "message": f"Commit {i}",
                "files": [],
            })
        store.store_commits(repo_id, commits)

        # Con limit=3 debe retornar solo 3 días (los más recientes)
        days = store.get_commit_days(repo_id, limit=3)
        assert len(days) == 3
        # Los días más recientes primero (ORDER BY day DESC)
        assert days[0] == "2026-04-14"
