"""Configuración persistente para MoolMesh."""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".moolmesh"


def _toml_escape(value: str) -> str:
    r"""Escapa un string para embeber en TOML basic string (entre comillas dobles).

    TOML spec sección 2.4: los escapes válidos son \, ", \n, \r, \t, \uXXXX, \UXXXXXXXX.
    Cualquier otro \X es inválido y causa TOMLDecodeError al leer.

    Orden crítico: escapar \ primero para no doble-escapar.
    """
    value = value.replace("\\", "\\\\")   # 1. backslash → double backslash (PRIMERO)
    value = value.replace('"', '\\"')      # 2. comilla doble
    value = value.replace("\n", "\\n")    # 3. newline
    value = value.replace("\r", "\\r")    # 4. carriage return
    value = value.replace("\t", "\\t")    # 5. tab
    return value


def _normalize_path(path: str) -> str:
    """Normaliza un path cross-platform, incluyendo paths MSYS2 de Git Bash.

    Git Bash en Windows convierte 'C:\\Users\\x' a '/c/Users/x'.
    En Windows nativo, Path('/c/Users/x').resolve() daría 'C:\\c\\Users\\x' (mal).
    """
    if os.name == "nt":  # Windows
        # Detectar formato MSYS2: /c/... o /d/... etc.
        import re
        m = re.match(r'^/([a-zA-Z])/(.*)', path.replace("\\", "/"))
        if m:
            drive, rest = m.group(1).upper(), m.group(2)
            path = f"{drive}:\\{rest.replace('/', chr(92))}"
    return str(Path(path).resolve())


CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class RepoConfig:
    path: str               # ruta absoluta al repo
    remote_url: str         # "github.com/owner/repo"
    owner: str              # "owner"
    repo: str               # "repo"
    added_at: str           # ISO timestamp
    github_enabled: bool = True


@dataclass
class HubConfig:
    repos: list[RepoConfig] = field(default_factory=list)
    github_token: str = ""           # vacío = auto-detect
    github_handle: str = ""          # para "Tus Pendientes" en digest
    # --- LLM (sección canónica) ---
    llm_provider: str = "ollama"
    llm_api_url: str = "https://ollama.com/api"
    llm_model: str = "qwen3.5:35b-cloud"
    llm_api_key: str = ""
    # --- Ollama legacy (backward compat, solo lectura) ---
    ollama_api_url: str = "https://ollama.com/api"
    ollama_model: str = "qwen3.5:35b-cloud"
    ollama_api_key: str = ""


def _serialize_toml(config: HubConfig) -> str:
    """Serializa HubConfig a formato TOML manualmente.
    
    tomllib solo lee, no escribe. Implementamos un writer simple.
    """
    lines = []
    
    # Sección [github]
    lines.append("[github]")
    lines.append(f'token = "{_toml_escape(config.github_token)}"')
    lines.append("")

    # Sección [llm] (reemplaza [ollama])
    lines.append("[llm]")
    lines.append(f'provider = "{_toml_escape(config.llm_provider)}"')
    lines.append(f'api_url = "{_toml_escape(config.llm_api_url)}"')
    lines.append(f'model = "{_toml_escape(config.llm_model)}"')
    lines.append(f'api_key = "{_toml_escape(config.llm_api_key)}"')
    lines.append("")

    # Sección [user]
    lines.append("[user]")
    lines.append(f'github_handle = "{_toml_escape(config.github_handle)}"')
    lines.append("")

    # Sección [[repos]] - array de tablas
    for repo in config.repos:
        lines.append("[[repos]]")
        lines.append(f'path = "{_toml_escape(repo.path)}"')
        lines.append(f'remote_url = "{_toml_escape(repo.remote_url)}"')
        lines.append(f'owner = "{_toml_escape(repo.owner)}"')
        lines.append(f'repo = "{_toml_escape(repo.repo)}"')
        lines.append(f'added_at = "{_toml_escape(repo.added_at)}"')
        lines.append(f'github_enabled = {str(repo.github_enabled).lower()}')
        lines.append("")
    
    return "\n".join(lines)


def load_config() -> HubConfig:
    """Lee config.toml. Si no existe, retorna defaults."""
    if not CONFIG_PATH.exists():
        return HubConfig()
    
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)
    
    config = HubConfig()

    # Parse github section (solo token, no handle — fuente única es [user])
    if "github" in data:
        github = data["github"]
        config.github_token = github.get("token", "")

    # Parse [llm] section (canónica) con fallback a [ollama]
    if "llm" in data:
        llm = data["llm"]
        config.llm_provider = llm.get("provider", "ollama")
        config.llm_api_url = llm.get("api_url", config.llm_api_url)
        config.llm_model = llm.get("model", config.llm_model)
        config.llm_api_key = llm.get("api_key", "")
    elif "ollama" in data:
        ollama = data["ollama"]
        config.llm_provider = "ollama"
        config.llm_api_url = ollama.get("api_url", config.llm_api_url)
        config.llm_model = ollama.get("model", config.llm_model)
        config.llm_api_key = ollama.get("api_key", "")

    # Siempre leer [ollama] para backward compat de campos legacy
    if "ollama" in data:
        ollama = data["ollama"]
        config.ollama_api_url = ollama.get("api_url", config.ollama_api_url)
        config.ollama_model = ollama.get("model", config.ollama_model)
        config.ollama_api_key = ollama.get("api_key", "")

    # Parse user section (fuente canónica para github_handle)
    if "user" in data:
        user = data["user"]
        config.github_handle = user.get("github_handle", "")
    
    # Parse repos array
    if "repos" in data:
        for repo_data in data["repos"]:
            repo = RepoConfig(
                path=repo_data.get("path", ""),
                remote_url=repo_data.get("remote_url", ""),
                owner=repo_data.get("owner", ""),
                repo=repo_data.get("repo", ""),
                added_at=repo_data.get("added_at", ""),
                github_enabled=repo_data.get("github_enabled", True),
            )
            config.repos.append(repo)
    
    return config


def save_config(config: HubConfig) -> None:
    """Escribe config.toml de forma atómica. Crea directorio si no existe."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    toml_content = _serialize_toml(config)

    # Escritura atómica: escribir a .tmp y luego rename
    # os.replace() es atómico en POSIX y en Windows (desde Python 3.3)
    tmp_path = CONFIG_PATH.with_suffix(".toml.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(toml_content)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        # Limpiar archivo temporal si algo falló
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def get_github_token(config: HubConfig) -> str | None:
    """Obtiene token en orden: config > GITHUB_TOKEN env > gh auth token.
    
    Timeout de 5s para gh auth token para evitar bloqueos.
    """
    # 1. Token explícito en config
    if config.github_token:
        return config.github_token
    
    # 2. GITHUB_TOKEN env var
    if token := os.getenv("GITHUB_TOKEN"):
        return token
    
    # 3. gh auth token (subprocess, timeout 5s)
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    return None


def add_repo(path: str, no_github: bool = False) -> RepoConfig:
    """Registra un repo: detecta remote, crea RepoConfig, guarda en config.
    
    Raises ValueError si no es un repo git o no tiene remote.
    """
    from hub.git_utils import is_git_repo, get_remote_url, parse_github_remote
    
    # 1. Validar que es un repo git
    if not is_git_repo(path):
        raise ValueError(f"No es un repositorio git: {path}")
    
    # 2. Detectar remote origin
    remote_url = get_remote_url(path)
    if not remote_url:
        raise ValueError(f"El repo no tiene remote 'origin' configurado: {path}")
    
    # 3. Parsear owner/repo
    parsed = parse_github_remote(remote_url)
    if not parsed:
        raise ValueError(f"No se pudo parsear el remote como GitHub: {remote_url}")
    
    owner, repo_name = parsed
    
    # 4. Crear RepoConfig
    repo_config = RepoConfig(
        path=_normalize_path(path),
        remote_url=f"github.com/{owner}/{repo_name}",
        owner=owner,
        repo=repo_name,
        added_at=datetime.now(timezone.utc).isoformat(),
        github_enabled=not no_github,
    )
    
    return repo_config


def remove_repo(path: str) -> bool:
    """Elimina repo de config. Retorna True si existía."""
    config = load_config()

    # Normalizar el path de entrada para comparación robusta cross-platform
    normalized_target = _normalize_path(path)

    original_count = len(config.repos)
    config.repos = [r for r in config.repos if _normalize_path(r.path) != normalized_target]

    if len(config.repos) < original_count:
        save_config(config)
        return True
    return False


def list_repos() -> list[RepoConfig]:
    """Lista repos registrados."""
    config = load_config()
    return config.repos
