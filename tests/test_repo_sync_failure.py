"""Issue #34 — mool repo sync distingue 'git falló' de '0 commits nuevos'.

Cubre el contrato str | None de los helpers git_log_*, su propagación en
GitHarvester.ingest_history (sentinel GIT_READ_FAILED) y en el daemon loop
(_fetch_and_ingest, que no debe crashear con None), y el reporte al usuario
en cmd_repo_sync / cmd_repo_add (error + exit no-cero, no "0 commits").
"""
import pytest
from unittest.mock import MagicMock, patch

from hub import git_utils
from hub.cache.git_store import GitStore
from hub.harvesters.git_harvester import GitHarvester, GIT_READ_FAILED
from hub.cli import cmd_repo_sync, cmd_repo_add


@pytest.fixture
def store(tmp_path) -> GitStore:
    db = tmp_path / "github.db"
    s = GitStore(db)
    yield s
    s.close()


# --- 1. Contrato de los helpers: None en fallo, "" / contenido en éxito ---

_HELPERS = [
    ("git_log_range", ("/x", "a", "b")),
    ("git_log_since", ("/x", "2020-01-01")),
    ("git_log_all", ("/x",)),
]


@pytest.mark.parametrize("name,args", _HELPERS)
def test_helper_returns_none_on_git_failure(monkeypatch, name, args):
    """rc≠0 → None (git falló), no "" (que significaría 0 commits)."""
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 128
            stdout = ""
            stderr = "fatal: bad revision"
        return R()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert getattr(git_utils, name)(*args) is None


@pytest.mark.parametrize("name,args", _HELPERS)
def test_helper_returns_empty_on_success_no_commits(monkeypatch, name, args):
    """rc==0 con stdout vacío → "" (git ok, 0 commits), no None."""
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert getattr(git_utils, name)(*args) == ""


@pytest.mark.parametrize("name,args", _HELPERS)
def test_helper_returns_stdout_on_success_with_commits(monkeypatch, name, args):
    """rc==0 con contenido → devuelve el stdout tal cual."""
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "some|log|output\n"
            stderr = ""
        return R()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert getattr(git_utils, name)(*args) == "some|log|output\n"


@pytest.mark.parametrize("name,args", _HELPERS)
def test_helper_returns_none_on_exception(monkeypatch, name, args):
    """Excepción de subprocess → None (no ""), y no propaga."""
    def fake_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert getattr(git_utils, name)(*args) is None


# --- 2. Propagación en ingest_history: sentinel de fallo vs 0 vs N ---

def _register(store):
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
    return store.get_repo_id("/path/to/repo")


@patch("hub.harvesters.git_harvester.git_log_since")
def test_ingest_history_returns_sentinel_on_failure(mock_since, store):
    """git_log_since None → ingest_history devuelve GIT_READ_FAILED (-1)."""
    _register(store)
    mock_since.return_value = None
    harvester = GitHarvester(store)
    assert harvester.ingest_history("/path/to/repo", days=14) == GIT_READ_FAILED


@patch("hub.harvesters.git_harvester.get_remote_refs")
@patch("hub.harvesters.git_harvester.git_log_since")
def test_ingest_history_returns_zero_on_empty(mock_since, mock_refs, store):
    """git_log_since "" → 0 commits (git ok), no sentinel."""
    _register(store)
    mock_since.return_value = ""
    mock_refs.return_value = {}
    harvester = GitHarvester(store)
    assert harvester.ingest_history("/path/to/repo", days=14) == 0


@patch("hub.harvesters.git_harvester.git_log_all")
def test_ingest_history_all_returns_sentinel_on_failure(mock_all, store):
    """days=None con git_log_all None → GIT_READ_FAILED."""
    _register(store)
    mock_all.return_value = None
    harvester = GitHarvester(store)
    assert harvester.ingest_history("/path/to/repo", days=None) == GIT_READ_FAILED


# --- 3. Daemon loop: _fetch_and_ingest tolera None sin crashear ---

@patch("hub.harvesters.git_harvester.git_fetch")
@patch("hub.harvesters.git_harvester.get_remote_refs")
@patch("hub.harvesters.git_harvester.git_log_range")
def test_daemon_fetch_and_ingest_tolerates_none(mock_range, mock_refs,
                                                 mock_fetch, store):
    """git_log_range None (fallo) en el daemon → no crashea, no ingesta, y NO
    avanza el cursor de la ref fallida (se reintenta en el próximo ciclo en vez
    de perder commits en silencio, §4)."""
    repo_id = _register(store)
    old = "old123sha456" * 4
    new = "new123sha456" * 4
    store.update_refs(repo_id, {"refs/remotes/origin/main": old[:40]})
    mock_fetch.return_value = True
    mock_refs.return_value = {"refs/remotes/origin/main": new[:40]}
    mock_range.return_value = None  # git falló en el rango

    harvester = GitHarvester(store)
    # No debe levantar excepción.
    harvester._fetch_and_ingest(store.list_repos()[0])

    assert store.count_commits(repo_id) == 0
    # La ref fallida NO se avanzó: sigue en old para reintentar en el próximo
    # ciclo (si se hubiera avanzado a new, esos commits se perderían).
    assert store.get_refs(repo_id)["refs/remotes/origin/main"] == old[:40]


@patch("hub.harvesters.git_harvester.git_fetch")
@patch("hub.harvesters.git_harvester.get_remote_refs")
@patch("hub.harvesters.git_harvester.git_log_range")
def test_daemon_advances_healthy_refs_despite_one_failure(mock_range, mock_refs,
                                                          mock_fetch, store):
    """Con dos refs que avanzaron y una falla: la sana se ingesta y avanza; la
    fallida no avanza. Un fallo aislado no bloquea el resto del repo (§4)."""
    repo_id = _register(store)
    old_ok = "aaa" * 20
    new_ok = "bbb" * 20
    old_bad = "ccc" * 20
    new_bad = "ddd" * 20
    store.update_refs(repo_id, {
        "refs/remotes/origin/ok": old_ok[:40],
        "refs/remotes/origin/bad": old_bad[:40],
    })
    mock_fetch.return_value = True
    mock_refs.return_value = {
        "refs/remotes/origin/ok": new_ok[:40],
        "refs/remotes/origin/bad": new_bad[:40],
    }

    def range_side_effect(path, old_sha, new_sha):
        if new_sha == new_ok[:40]:
            return ("abc123def456abc123def456abc123def456abc1|John|j@x.dev|"
                    "2026-04-10T10:00:00+00:00||msg\n1\t0\tf.py\n")
        return None  # la ref "bad" falla

    mock_range.side_effect = range_side_effect

    GitHarvester(store)._fetch_and_ingest(store.list_repos()[0])

    assert store.count_commits(repo_id) == 1
    refs = store.get_refs(repo_id)
    assert refs["refs/remotes/origin/ok"] == new_ok[:40]   # sana avanzó
    assert refs["refs/remotes/origin/bad"] == old_bad[:40]  # fallida no avanzó


# --- 4. cmd_repo_sync / cmd_repo_add: error + exit no-cero en fallo ---

class _Args:
    def __init__(self, path, days=14, all_history=False, no_github=True):
        self.path = path
        self.days = days
        self.all_history = all_history
        self.no_github = no_github


def _mock_registered(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    resolved = str(repo_path.resolve())

    mock_config = MagicMock()
    mock_repo = MagicMock()
    mock_repo.path = resolved
    mock_repo.owner = "owner"
    mock_repo.repo = "repo"
    mock_config.repos = [mock_repo]
    return repo_path, resolved, mock_config


def test_repo_sync_reports_error_and_exits_nonzero_on_failure(tmp_path, capsys):
    """git falló → mensaje rojo de error + SystemExit≠0, no "0 commits"."""
    repo_path, resolved, mock_config = _mock_registered(tmp_path)

    with patch("hub.config.load_config", return_value=mock_config), \
         patch("hub.cache.git_store.GitStore") as mock_store_class, \
         patch("hub.harvesters.git_harvester.GitHarvester") as mock_harv_class:
        mock_store = MagicMock()
        mock_store.get_repo_id.return_value = 1
        mock_store_class.return_value = mock_store
        mock_harv = MagicMock()
        mock_harv.ingest_history.return_value = GIT_READ_FAILED
        mock_harv_class.return_value = mock_harv

        with pytest.raises(SystemExit) as exc:
            cmd_repo_sync(_Args(str(repo_path)))

    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "Error reading git" in captured.out
    assert "0 new commits" not in captured.out  # no mentimos con 0
    mock_store.close.assert_called_once()  # cerramos antes de salir


def test_repo_sync_reports_success_on_zero_commits(tmp_path, capsys):
    """0 commits reales (git ok) → mensaje normal, sin error, sin exit."""
    repo_path, resolved, mock_config = _mock_registered(tmp_path)

    with patch("hub.config.load_config", return_value=mock_config), \
         patch("hub.cache.git_store.GitStore") as mock_store_class, \
         patch("hub.harvesters.git_harvester.GitHarvester") as mock_harv_class:
        mock_store = MagicMock()
        mock_store.get_repo_id.return_value = 1
        mock_store_class.return_value = mock_store
        mock_harv = MagicMock()
        mock_harv.ingest_history.return_value = 0
        mock_harv_class.return_value = mock_harv

        cmd_repo_sync(_Args(str(repo_path)))  # no SystemExit

    captured = capsys.readouterr()
    assert "Synced: 0 new commits ingested" in captured.out
    assert "Error reading git" not in captured.out


def test_repo_add_warns_without_exit_on_backfill_failure(tmp_path, capsys):
    """cmd_repo_add: el add SÍ tuvo éxito (repo registrado); un fallo del
    backfill del historial se avisa (yellow) SIN exit no-cero y sin reportar
    "0 commits" (#34). El retry se sugiere vía `mool repo sync`."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    mock_repo_config = MagicMock()
    mock_repo_config.owner = "owner"
    mock_repo_config.repo = "repo"

    empty_config = MagicMock()
    empty_config.repos = []

    with patch("hub.config.add_repo", return_value=mock_repo_config), \
         patch("hub.config.load_config", return_value=empty_config), \
         patch("hub.config.save_config"), \
         patch("hub.cache.git_store.GitStore") as mock_store_class, \
         patch("hub.harvesters.git_harvester.GitHarvester") as mock_harv_class:
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store
        mock_harv = MagicMock()
        mock_harv.ingest_history.return_value = GIT_READ_FAILED
        mock_harv_class.return_value = mock_harv

        cmd_repo_add(_Args(str(repo_path)))  # no SystemExit — el add funcionó

    captured = capsys.readouterr()
    assert "Registered owner/repo" in captured.out
    assert "Could not read git history" in captured.out
    assert "Ingested" not in captured.out  # no imprime "Ingested -1 commits"
