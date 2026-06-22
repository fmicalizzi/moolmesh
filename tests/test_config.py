"""Tests for config module."""
import subprocess
import tomllib

import pytest

from hub.config import (
    HubConfig, RepoConfig, load_config, save_config,
    get_github_token, add_repo, remove_repo, list_repos,
    _toml_escape
)
from hub.git_utils import parse_github_remote


@pytest.fixture
def temp_config_path(tmp_path, monkeypatch):
    """Use a temporary config path for tests."""
    config_dir = tmp_path / ".moolmesh"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    
    # Monkeypatch the CONFIG_PATH in the module
    import hub.config as config_module
    original_path = config_module.CONFIG_PATH
    original_dir = config_module.CONFIG_DIR
    config_module.CONFIG_PATH = config_path
    config_module.CONFIG_DIR = config_dir
    
    yield config_path
    
    # Restore original paths
    config_module.CONFIG_PATH = original_path
    config_module.CONFIG_DIR = original_dir


class TestLoadConfigDefault:
    def test_load_config_default_no_file(self, temp_config_path):
        """Sin archivo retorna HubConfig vacío."""
        config = load_config()
        assert isinstance(config, HubConfig)
        assert config.repos == []
        assert config.github_token == ""
        assert config.ollama_model == "qwen3.5:35b-cloud"


class TestSaveAndLoadRoundtrip:
    def test_save_and_load_roundtrip(self, temp_config_path):
        """save → load preserva datos."""
        config = HubConfig(
            repos=[
                RepoConfig(
                    path="/path/to/repo",
                    remote_url="github.com/owner/repo",
                    owner="owner",
                    repo="repo",
                    added_at="2026-04-10T10:00:00",
                    github_enabled=True,
                )
            ],
            github_handle="testuser",
            llm_model="llama3",
        )

        save_config(config)
        loaded = load_config()

        assert len(loaded.repos) == 1
        assert loaded.repos[0].owner == "owner"
        assert loaded.repos[0].repo == "repo"
        assert loaded.github_handle == "testuser"
        assert loaded.llm_model == "llama3"


class TestParseGitHubRemote:
    def test_parse_github_remote_ssh(self):
        """git@github.com:owner/repo.git"""
        result = parse_github_remote("git@github.com:owner/repo.git")
        assert result == ("owner", "repo")

    def test_parse_github_remote_https(self):
        """https://github.com/owner/repo.git"""
        result = parse_github_remote("https://github.com/owner/repo.git")
        assert result == ("owner", "repo")

    def test_parse_github_remote_no_git_suffix(self):
        """https://github.com/owner/repo"""
        result = parse_github_remote("https://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_parse_github_remote_not_github(self):
        """gitlab URLs should return None."""
        result = parse_github_remote("https://gitlab.com/owner/repo.git")
        assert result is None


class TestGetGitHubToken:
    def test_get_github_token_from_config(self, temp_config_path):
        """Prioridad 1: token explícito en config."""
        config = HubConfig(github_token="config_token")
        assert get_github_token(config) == "config_token"

    def test_get_github_token_from_env(self, temp_config_path, monkeypatch):
        """Prioridad 2: GITHUB_TOKEN env var."""
        monkeypatch.setenv("GITHUB_TOKEN", "env_token")
        config = HubConfig(github_token="")
        assert get_github_token(config) == "env_token"

    def test_get_github_token_from_gh(self, temp_config_path, monkeypatch):
        """Prioridad 3: fallback a gh auth token."""
        # Mock subprocess.run to simulate gh auth token
        def mock_run(*args, **kwargs):
            class MockResult:
                returncode = 0
                stdout = "gh_token\n"
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        
        config = HubConfig(github_token="")
        assert get_github_token(config) == "gh_token"


class TestAddRepo:
    def test_add_repo_not_git(self, tmp_path):
        """ValueError si no es git repo."""
        with pytest.raises(ValueError, match="No es un repositorio git"):
            add_repo(str(tmp_path / "not_a_repo"))

    def test_add_repo_no_remote(self, tmp_path):
        """ValueError si no tiene remote."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        
        # Initialize git repo but no remote
        import subprocess
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        
        with pytest.raises(ValueError, match="no tiene remote"):
            add_repo(str(repo_path))

    def test_add_repo_success(self, tmp_path):
        """Detecta remote, crea RepoConfig."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        
        import subprocess
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:testuser/testrepo.git"],
            cwd=repo_path,
            capture_output=True
        )
        
        config = add_repo(str(repo_path))
        
        assert config.owner == "testuser"
        assert config.repo == "testrepo"
        assert config.remote_url == "github.com/testuser/testrepo"
        assert config.path == str(repo_path.resolve())


class TestRemoveRepo:
    def test_remove_repo(self, temp_config_path):
        """Elimina del config."""
        config = HubConfig(
            repos=[
                RepoConfig(
                    path="/path/to/remove",
                    remote_url="github.com/owner/repo",
                    owner="owner",
                    repo="repo",
                    added_at="2026-04-10T10:00:00",
                    github_enabled=True,
                ),
                RepoConfig(
                    path="/path/to/keep",
                    remote_url="github.com/owner/repo2",
                    owner="owner",
                    repo="repo2",
                    added_at="2026-04-10T10:00:00",
                    github_enabled=True,
                )
            ]
        )
        save_config(config)
        
        found = remove_repo("/path/to/remove")
        
        assert found is True
        repos = list_repos()
        assert len(repos) == 1
        assert repos[0].path == "/path/to/keep"

    def test_remove_repo_not_found(self, temp_config_path):
        """Retorna False si no existe."""
        config = HubConfig(repos=[])
        save_config(config)
        
        found = remove_repo("/nonexistent/path")
        
        assert found is False


class TestGitHubHandle:
    """Tests for github_handle single source of truth."""

    def test_github_handle_from_user_only(self, tmp_path):
        """Config sin [github].handle carga [user].github_handle correctamente."""
        import hub.config

        # Create config with only [user].github_handle
        config_content = """
[user]
github_handle = "testuser123"

[[repos]]
path = "/test/repo"
remote_url = "github.com/o/r"
owner = "o"
repo = "r"
added_at = "2026-04-10T10:00:00"
github_enabled = true
"""
        # Write to temp config file
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(config_content)

        # Monkey-patch CONFIG_PATH temporarily
        orig_path = hub.config.CONFIG_PATH
        try:
            hub.config.CONFIG_PATH = config_file
            config = load_config()
            assert config.github_handle == "testuser123"
        finally:
            hub.config.CONFIG_PATH = orig_path

    def test_github_handle_legacy_github_section_ignored(self, tmp_path):
        """[github].handle legacy se ignora, usa [user].github_handle."""
        import hub.config

        # Create config with BOTH legacy [github].handle and [user].github_handle
        config_content = """
[github]
token = ""
handle = "legacy_user"

[user]
github_handle = "canonical_user"

[[repos]]
path = "/test/repo"
remote_url = "github.com/o/r"
owner = "o"
repo = "r"
added_at = "2026-04-10T10:00:00"
github_enabled = true
"""
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(config_content)

        orig_path = hub.config.CONFIG_PATH
        try:
            hub.config.CONFIG_PATH = config_file
            config = load_config()
            # Should use [user].github_handle (canonical source)
            assert config.github_handle == "canonical_user"
        finally:
            hub.config.CONFIG_PATH = orig_path


class TestLlmConfig:
    """Tests para la nueva sección [llm] y backward compat con [ollama]."""

    def test_load_llm_section(self, tmp_path):
        """Config con [llm] carga campos llm_* correctamente."""
        import hub.config
        config_content = """
[llm]
provider = "openrouter"
api_url = "https://openrouter.ai/api/v1"
model = "google/gemini-2.5-flash"
api_key = "sk-or-v1-test"
"""
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(config_content)

        orig_path = hub.config.CONFIG_PATH
        try:
            hub.config.CONFIG_PATH = config_file
            config = load_config()
            assert config.llm_provider == "openrouter"
            assert config.llm_api_url == "https://openrouter.ai/api/v1"
            assert config.llm_model == "google/gemini-2.5-flash"
            assert config.llm_api_key == "sk-or-v1-test"
        finally:
            hub.config.CONFIG_PATH = orig_path

    def test_fallback_ollama_to_llm(self, tmp_path):
        """Config sin [llm] pero con [ollama] mapea a campos llm_*."""
        import hub.config
        config_content = """
[ollama]
api_url = "https://ollama.com/api"
model = "llama3:8b"
api_key = "ollama-key"
"""
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(config_content)

        orig_path = hub.config.CONFIG_PATH
        try:
            hub.config.CONFIG_PATH = config_file
            config = load_config()
            assert config.llm_provider == "ollama"
            assert config.llm_api_url == "https://ollama.com/api"
            assert config.llm_model == "llama3:8b"
            assert config.llm_api_key == "ollama-key"
        finally:
            hub.config.CONFIG_PATH = orig_path

    def test_llm_roundtrip(self, tmp_path, monkeypatch):
        """save con [llm] → load → mismos valores."""
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        config = HubConfig(
            llm_provider="openrouter",
            llm_api_url="https://openrouter.ai/api/v1",
            llm_model="google/gemini-2.5-flash",
            llm_api_key="sk-test",
        )
        save_config(config)
        loaded = load_config()

        assert loaded.llm_provider == "openrouter"
        assert loaded.llm_api_url == "https://openrouter.ai/api/v1"
        assert loaded.llm_model == "google/gemini-2.5-flash"
        assert loaded.llm_api_key == "sk-test"

    def test_legacy_ollama_not_rewritten(self, tmp_path):
        """Archivo con [ollama] se lee bien sin reescribir."""
        import hub.config
        config_content = """
[ollama]
api_url = "https://ollama.com/api"
model = "qwen3.5:35b-cloud"
api_key = "test"
"""
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(config_content)

        orig_path = hub.config.CONFIG_PATH
        try:
            hub.config.CONFIG_PATH = config_file
            load_config()
            # El archivo original no debe haberse modificado
            content = config_file.read_text()
            assert "[ollama]" in content
            assert "[llm]" not in content
        finally:
            hub.config.CONFIG_PATH = orig_path


class TestTomlEscape:
    """Verifica que _toml_escape produce TOML parseable y round-trip exacto."""

    def _roundtrip(self, value: str) -> str:
        """Escapa value, lo embebe en TOML, parsea y retorna el resultado."""
        toml_str = f'x = "{_toml_escape(value)}"\n'
        return tomllib.loads(toml_str)["x"]

    def test_simple_string(self):
        assert self._roundtrip("hello world") == "hello world"

    def test_windows_path(self):
        path = r"C:\Users\franco\mi proyecto"
        assert self._roundtrip(path) == path

    def test_windows_path_deep(self):
        path = r"C:\Users\franco\Documents\my project\sub folder"
        assert self._roundtrip(path) == path

    def test_path_with_spaces_linux(self):
        path = "/home/franco/mi proyecto con espacios"
        assert self._roundtrip(path) == path

    def test_unicode_path(self):
        path = "/home/usuario/proyectos/café-app"
        assert self._roundtrip(path) == path

    def test_double_backslash(self):
        # Un path UNC de Windows: \\server\share
        path = r"\\server\share\proyecto"
        assert self._roundtrip(path) == path

    def test_double_quote_in_value(self):
        value = 'Mi "proyecto" especial'
        assert self._roundtrip(value) == value

    def test_newline_in_value(self):
        value = "linea1\nlinea2"
        assert self._roundtrip(value) == value

    def test_tab_in_value(self):
        value = "campo\tvalor"
        assert self._roundtrip(value) == value

    def test_empty_string(self):
        assert self._roundtrip("") == ""

    def test_backslash_escape_order(self):
        # Verifica que \\ no se doble-escape a \\\\
        # "a\b" → escape → "a\\b" → TOML parse → "a\b"
        value = "a\\b"
        assert self._roundtrip(value) == value

    def test_mixed_special_chars(self):
        value = r'C:\Users\"franco"\path' + "\nwith newline"
        assert self._roundtrip(value) == value


class TestSerializeDeserialize:
    """Verifica que save_config/load_config hace round-trip correcto."""

    def test_windows_path_roundtrip(self, tmp_path, monkeypatch):
        """Un path Windows se guarda y se recupera idéntico."""
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        config = HubConfig(repos=[
            RepoConfig(
                path=r"C:\Users\franco\mi proyecto",
                remote_url="github.com/franco/mi-proyecto",
                owner="franco",
                repo="mi-proyecto",
                added_at="2026-04-18T00:00:00+00:00",
            )
        ])
        save_config(config)
        loaded = load_config()

        assert loaded.repos[0].path == r"C:\Users\franco\mi proyecto"

    def test_linux_path_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        config = HubConfig(repos=[
            RepoConfig(
                path="/home/usuario/proyecto con espacios",
                remote_url="github.com/usuario/proyecto",
                owner="usuario",
                repo="proyecto",
                added_at="2026-04-18T00:00:00+00:00",
            )
        ])
        save_config(config)
        loaded = load_config()

        assert loaded.repos[0].path == "/home/usuario/proyecto con espacios"

    def test_multiple_repos_roundtrip(self, tmp_path, monkeypatch):
        """Varios repos se preservan todos en orden."""
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        paths = [r"C:\repo1", "/home/user/repo2", "/tmp/repo con espacios/3"]
        config = HubConfig(repos=[
            RepoConfig(path=p, remote_url="github.com/x/y", owner="x", repo="y",
                       added_at="2026-04-18T00:00:00+00:00")
            for p in paths
        ])
        save_config(config)
        loaded = load_config()

        assert [r.path for r in loaded.repos] == paths

    def test_atomic_write_produces_valid_file(self, tmp_path, monkeypatch):
        """El archivo .tmp no queda en disco tras escritura exitosa."""
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        save_config(HubConfig())

        assert (tmp_path / "config.toml").exists()
        assert not (tmp_path / "config.toml.tmp").exists()

    def test_toml_is_parseable_by_tomllib(self, tmp_path, monkeypatch):
        """El TOML generado pasa el parser de stdlib sin errores."""
        monkeypatch.setattr("hub.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_PATH", tmp_path / "config.toml")

        config = HubConfig(repos=[
            RepoConfig(
                path=r"C:\Users\test\project",
                remote_url="github.com/test/project",
                owner="test",
                repo="project",
                added_at="2026-04-18T00:00:00+00:00",
            )
        ])
        save_config(config)

        with open(tmp_path / "config.toml", "rb") as f:
            data = tomllib.load(f)   # no debe lanzar TOMLDecodeError
        assert data["repos"][0]["path"] == r"C:\Users\test\project"
