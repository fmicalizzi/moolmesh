"""Tests para mool repo sync (T1)."""
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from hub.cli import cmd_repo_sync


class MockArgs:
    """Simple mock for argparse.Namespace."""

    def __init__(self, path, days=14, all_history=False):
        self.path = path
        self.days = days
        self.all_history = all_history


class TestRepoSync:
    """Tests para el subcomando repo sync."""

    def test_sync_unregistered_repo_shows_error(self, tmp_path, capsys):
        """Repo no registrado → mensaje de error claro."""
        repo_path = tmp_path / "unregistered_repo"
        repo_path.mkdir()

        # Patch el módulo de config, no hub.cli que importa dentro de funciones
        with patch("hub.config.load_config") as mock_load_config:
            # Config sin repos
            mock_config = MagicMock()
            mock_config.repos = []
            mock_load_config.return_value = mock_config

            args = MockArgs(str(repo_path))
            cmd_repo_sync(args)

        captured = capsys.readouterr()
        assert "Not registered" in captured.out
        assert "mool repo add" in captured.out

    def test_sync_already_registered_ingests(self, tmp_path, capsys):
        """Repo registrado → llama a ingest_history con los parámetros correctos."""
        repo_path = tmp_path / "registered_repo"
        repo_path.mkdir()
        # Crear un git repo mínimo
        (repo_path / ".git").mkdir()

        with patch("hub.config.load_config") as mock_load_config, \
             patch("hub.cache.git_store.GitStore") as mock_store_class, \
             patch("hub.harvesters.git_harvester.GitHarvester") as mock_harvester_class:

            # Mock config con el repo registrado
            mock_config = MagicMock()
            mock_repo = MagicMock()
            mock_repo.path = str(repo_path.resolve())
            mock_repo.owner = "testowner"
            mock_repo.repo = "testrepo"
            mock_config.repos = [mock_repo]
            mock_load_config.return_value = mock_config

            # Mock GitStore
            mock_store = MagicMock()
            mock_store.get_repo_id.return_value = 1
            mock_store_class.return_value = mock_store

            # Mock GitHarvester
            mock_harvester = MagicMock()
            mock_harvester.ingest_history.return_value = 5
            mock_harvester_class.return_value = mock_harvester

            args = MockArgs(str(repo_path), days=30)
            cmd_repo_sync(args)

        captured = capsys.readouterr()
        assert "Synced: 5 new commits ingested" in captured.out
        mock_harvester.ingest_history.assert_called_once_with(str(repo_path.resolve()), days=30)

    def test_sync_all_flag(self, tmp_path, capsys):
        """--all → days=None en ingest_history."""
        repo_path = tmp_path / "registered_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("hub.config.load_config") as mock_load_config, \
             patch("hub.cache.git_store.GitStore") as mock_store_class, \
             patch("hub.harvesters.git_harvester.GitHarvester") as mock_harvester_class:

            mock_config = MagicMock()
            mock_repo = MagicMock()
            mock_repo.path = str(repo_path.resolve())
            mock_repo.owner = "testowner"
            mock_repo.repo = "testrepo"
            mock_config.repos = [mock_repo]
            mock_load_config.return_value = mock_config

            mock_store = MagicMock()
            mock_store.get_repo_id.return_value = 1
            mock_store_class.return_value = mock_store

            mock_harvester = MagicMock()
            mock_harvester.ingest_history.return_value = 100
            mock_harvester_class.return_value = mock_harvester

            args = MockArgs(str(repo_path), all_history=True)
            cmd_repo_sync(args)

        captured = capsys.readouterr()
        assert "full history" in captured.out
        # days debe ser None cuando all_history=True
        mock_harvester.ingest_history.assert_called_once()
        call_args = mock_harvester.ingest_history.call_args
        assert call_args[1]["days"] is None

    def test_sync_days_flag(self, tmp_path, capsys):
        """--days 30 → days=30 en ingest_history."""
        repo_path = tmp_path / "registered_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("hub.config.load_config") as mock_load_config, \
             patch("hub.cache.git_store.GitStore") as mock_store_class, \
             patch("hub.harvesters.git_harvester.GitHarvester") as mock_harvester_class:

            mock_config = MagicMock()
            mock_repo = MagicMock()
            mock_repo.path = str(repo_path.resolve())
            mock_repo.owner = "testowner"
            mock_repo.repo = "testrepo"
            mock_config.repos = [mock_repo]
            mock_load_config.return_value = mock_config

            mock_store = MagicMock()
            mock_store.get_repo_id.return_value = 1
            mock_store_class.return_value = mock_store

            mock_harvester = MagicMock()
            mock_harvester.ingest_history.return_value = 42
            mock_harvester_class.return_value = mock_harvester

            args = MockArgs(str(repo_path), days=30)
            cmd_repo_sync(args)

        captured = capsys.readouterr()
        assert "last 30 days" in captured.out
        mock_harvester.ingest_history.assert_called_once_with(str(repo_path.resolve()), days=30)

    def test_sync_repo_not_in_gitstore(self, tmp_path, capsys):
        """Repo registrado en config pero no en GitStore → error."""
        repo_path = tmp_path / "registered_repo"
        repo_path.mkdir()

        with patch("hub.config.load_config") as mock_load_config, \
             patch("hub.cache.git_store.GitStore") as mock_store_class:

            mock_config = MagicMock()
            mock_repo = MagicMock()
            mock_repo.path = str(repo_path.resolve())
            mock_repo.owner = "testowner"
            mock_repo.repo = "testrepo"
            mock_config.repos = [mock_repo]
            mock_load_config.return_value = mock_config

            # GitStore no encuentra el repo
            mock_store = MagicMock()
            mock_store.get_repo_id.return_value = None
            mock_store_class.return_value = mock_store

            args = MockArgs(str(repo_path))
            cmd_repo_sync(args)

        captured = capsys.readouterr()
        assert "Not found in GitStore" in captured.out
