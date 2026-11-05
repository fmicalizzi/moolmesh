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
    if path.startswith(("board:", "remote:")):
        return path  # a board (#93) / remote-only (#94) repo key, not a path
    if os.name == "nt":  # Windows
        # Detectar formato MSYS2: /c/... o /d/... etc.
        import re
        m = re.match(r'^/([a-zA-Z])/(.*)', path.replace("\\", "/"))
        if m:
            drive, rest = m.group(1).upper(), m.group(2)
            path = f"{drive}:\\{rest.replace('/', chr(92))}"
    return str(Path(path).resolve())


CONFIG_PATH = CONFIG_DIR / "config.toml"
# Default for `[daemon] stale_after_minutes` (issue #76).
DEFAULT_STALE_AFTER_MINUTES = 60
# Default for `[daemon] full_text_max_chars` (issue #52): characters of full
# text stored per event (head + tail kept over it).
DEFAULT_FULL_TEXT_MAX_CHARS = 256_000


@dataclass
class RepoConfig:
    path: str               # ruta absoluta al repo
    remote_url: str         # "github.com/owner/repo"
    owner: str              # "owner"
    repo: str               # "repo"
    added_at: str           # ISO timestamp
    github_enabled: bool = True
    # "code" (default) = a code repo: git fetch + commit ingestion + GitHub sync.
    # "board" = an issues-only backlog (#93): no git, only the GitHub issues sync.
    # "remote" = a code repo followed only through the API (#94): no clone, no
    # git history; issues, PRs and PR stats are synced like any code repo.
    kind: str = "code"


REPO_KINDS = ("code", "board", "remote")
# Kinds with no local clone: the git harvester never runs for them (#93, #94).
NO_GIT_KINDS = ("board", "remote")


def repo_has_git(repo: object) -> bool:
    """True when ``repo`` (a RepoConfig or a ``repos`` row dict) has a clone."""
    kind = repo.get("kind") if isinstance(repo, dict) else getattr(repo, "kind", "code")
    return normalize_repo_kind(kind) not in NO_GIT_KINDS


def normalize_repo_kind(value: object) -> str:
    """Return a valid repo kind; anything unknown falls back to ``"code"``."""
    kind = str(value or "code").strip().lower()
    return kind if kind in REPO_KINDS else "code"


# Default max recursion depth for a marked workspace root (relative to the root).
WORKSPACE_ROOT_DEFAULT_MAX_DEPTH = 6


@dataclass
class WorkspaceRoot:
    """A marked filesystem root the workspace watcher observes (issue #21).

    Opt-in by design: with no roots configured the watcher observes nothing.
    ``max_depth`` bounds the recursive scan (relative to ``path``); ``excludes``
    are *extra* directory names to prune, on top of the built-in defaults in
    the watcher. Root ``/`` is valid (autonomous-agent server) but never a
    default.
    """
    path: str                       # ruta absoluta a la raíz marcada
    max_depth: int = WORKSPACE_ROOT_DEFAULT_MAX_DEPTH
    excludes: list[str] = field(default_factory=list)
    added_at: str = ""              # ISO timestamp


@dataclass
class HubConfig:
    repos: list[RepoConfig] = field(default_factory=list)
    github_token: str = ""           # vacío = auto-detect
    github_handle: str = ""          # para "Tus Pendientes" en digest
    # Emails de commit del owner (issue #36, decisión 1A). Un commit es "propio"
    # si su email de autor está en esta lista (comparación sin mayúsculas);
    # vacía → no se marca nada (comportamiento previo). NUNCA se exponen los
    # emails ni conteos por autor en ninguna superficie: sólo un booleano.
    user_emails: list[str] = field(default_factory=list)
    # Zona horaria del "día local" (issue #81): nombre IANA, vacío = zona del
    # sistema. Se valida al usarse (hub.localtime.resolve_timezone): un nombre
    # desconocido cae a la zona del sistema con un aviso en el log, sin
    # reescribir el valor del usuario.
    timezone: str = ""
    # --- LLM (sección canónica) ---
    llm_provider: str = "ollama"
    llm_api_url: str = "https://ollama.com/api"
    llm_model: str = "qwen3.5:35b-cloud"
    llm_api_key: str = ""
    # --- Ollama legacy (backward compat, solo lectura) ---
    ollama_api_url: str = "https://ollama.com/api"
    ollama_model: str = "qwen3.5:35b-cloud"
    ollama_api_key: str = ""
    # --- Workspace filesystem watcher (issue #21, opt-in) ---
    workspace_roots: list[WorkspaceRoot] = field(default_factory=list)
    hide_project_names: bool = False  # enmascara nombres de carpeta en la superficie visible
    # Capa apagable de monitoreo de CARPETAS (issue #27). Gatea SÓLO el watcher
    # de filesystem — agentes, GitHub y portfolio quedan intactos. Default True
    # (preserva el comportamiento actual: el watcher igual sólo arranca si hay
    # workspace_roots marcados).
    filesystem_monitoring: bool = True
    # Atribución sesiones→workspace agendada en el daemon (issue #39). Default
    # True: el portfolio se mantiene vivo sin `mool workspace backfill` manual.
    # INDEPENDIENTE de filesystem_monitoring (ése gatea sólo el watcher de
    # carpetas); sólo lee events.db (mode=ro).
    auto_attribution: bool = True
    # --- Jerarquía cliente/org del portfolio (issue #29, Unit 3) ---
    # Orgs reales del owner que son CLIENTES (agrupan sus proyectos en un nodo
    # cliente). Curable. Vacío → auto-seed en el read-layer SÓLO si se conoce la
    # identidad del owner (personal_orgs no vacío); si no, el portfolio queda
    # plano (comportamiento previo). Se comparan normalizando (lstrip('_')+lower).
    client_orgs: list[str] = field(default_factory=list)
    # Org(es) del propio owner: sus proyectos se muestran SUELTOS (sin nodo
    # cliente). Default: el github_handle si está seteado. Sin identidad de owner
    # no hay jerarquía (portfolio plano).
    personal_orgs: list[str] = field(default_factory=list)
    # Override manual para bordes: project_key → nombre de cliente. Gana sobre
    # la escalera git-org/carpeta.
    client_overrides: dict[str, str] = field(default_factory=dict)
    # Alias de dueño (issue #95): dueño entero (org o cuenta personal) → cliente.
    # Un repo nuevo de ese dueño cae en el cliente sin override. Precedencia:
    # client_overrides > client_owner_aliases > personal_orgs/client_orgs > resto.
    client_owner_aliases: dict[str, str] = field(default_factory=dict)
    # Registro externo de clientes (issue #95): ruta a un TOML compartido entre
    # máquinas (ver ``load_client_registry``). Sólo lectura; si falta o es
    # inválido se avisa en el log y se sigue con la config local.
    client_registry: str = ""
    # Repos confidenciales que NUNCA se leen (issue #95): ni git, ni sync de
    # GitHub, ni `repo add`, ni portfolio, ni MCP. Dueños enteros y owner/repo.
    exclude_owners: list[str] = field(default_factory=list)
    exclude_repos: list[str] = field(default_factory=list)
    # Registro cargado (no se serializa: nunca se copia a config.toml).
    registry: "ClientRegistry | None" = field(default=None, repr=False,
                                              compare=False)
    # --- Orden por config (issue #36, decisiones 3/4/5; #64 parte 2) ---
    # Alias de proyectos: origen (ruta o clave) → proyecto canónico (clave o
    # ruta). Los workspaces del origen (y sus hijos) suman su historia al
    # canónico y no aparecen como fila propia. Destino inexistente → aviso.
    project_aliases: dict[str, str] = field(default_factory=dict)
    # Contenedores de referencia: la carpeta y sus hijos van a la sección
    # "contenedores / referencia", nunca como proyecto (funciona aunque la
    # carpeta ya no exista en disco).
    reference_containers: list[str] = field(default_factory=list)
    # Contenedores temporales: los hijos DIRECTOS van a una sección "Temporal"
    # visible y plegada, conservando su estado e historia.
    temporary_containers: list[str] = field(default_factory=list)
    # --- [daemon] (issue #76) ---
    # Minutes after which the last git / GitHub API sync counts as stale: MCP
    # tools, the dashboard and `mool doctor` then flag the data as not current.
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES
    # Tope de caracteres del texto completo guardado por evento (issue #52):
    # sobre el tope se conservan la cabeza y la cola. 0 = sin tope.
    full_text_max_chars: int = DEFAULT_FULL_TEXT_MAX_CHARS
    # --- [team] (issue #83, opt-in) ---
    # Desglose por persona (login de GitHub) en el outcome del portfolio y en
    # los digests. Default False: apagado, la salida es la de siempre.
    per_contributor: bool = False
    # login -> alias; varios logins con el mismo alias se suman.
    contributor_aliases: dict[str, str] = field(default_factory=dict)
    # Logins extra que cuentan como bot (además de dependabot, *[bot], app/*).
    bot_logins: list[str] = field(default_factory=list)

    # --- Vista efectiva cliente/exclusiones (issue #95): local + registro ---

    def effective_client_orgs(self) -> list[str]:
        """``client_orgs`` local unido al del registro (orden estable)."""
        out = list(self.client_orgs)
        if self.registry:
            out += [o for o in self.registry.client_orgs if o not in out]
        return out

    def effective_owner_aliases(self) -> dict[str, str]:
        """Alias de dueño normalizados (``lower``); la config local gana."""
        merged: dict[str, str] = {}
        if self.registry:
            merged.update(self.registry.client_owner_aliases)
        merged.update(self.client_owner_aliases)
        return {_norm_owner(k): v for k, v in merged.items() if _norm_owner(k)}

    def effective_client_overrides(self) -> dict[str, str]:
        """``client_overrides`` del registro con la config local encima."""
        merged: dict[str, str] = {}
        if self.registry:
            merged.update(self.registry.client_overrides)
        merged.update(self.client_overrides)
        return merged

    def exclusions(self) -> "Exclusions":
        """Dueños y repos excluidos (local ∪ registro), normalizados."""
        owners = list(self.exclude_owners)
        repos = list(self.exclude_repos)
        if self.registry:
            owners += self.registry.exclude_owners
            repos += self.registry.exclude_repos
        return Exclusions.build(owners, repos)

    def is_excluded(self, owner: str | None, repo: str | None) -> bool:
        """True when ``owner/repo`` must never be read (issue #95)."""
        return self.exclusions().matches(owner, repo)


def _norm_owner(name: object) -> str:
    """GitHub owner/repo names compare case-insensitively."""
    return str(name or "").strip().lower()


@dataclass(frozen=True)
class Exclusions:
    """Confidential owners/repos that are never read (issue #95)."""
    owners: frozenset[str] = frozenset()
    repos: frozenset[str] = frozenset()   # "owner/repo", lowercased

    @classmethod
    def build(cls, owners: list[str], repos: list[str]) -> "Exclusions":
        o = {_norm_owner(x).strip("/") for x in owners if _norm_owner(x)}
        r = set()
        for spec in repos:
            parsed = parse_board_slug(str(spec))
            if parsed:
                r.add(f"{parsed[0]}/{parsed[1]}".lower())
        return cls(frozenset(o), frozenset(r))

    def __bool__(self) -> bool:
        return bool(self.owners or self.repos)

    def matches(self, owner: str | None, repo: str | None) -> bool:
        o = _norm_owner(owner)
        if not o:
            return False
        if o in self.owners:
            return True
        return f"{o}/{_norm_owner(repo)}" in self.repos

    def matches_remote(self, remote: str | None) -> bool:
        """Match a ``github.com/owner/repo`` remote or a ``git_remote:`` key."""
        if not remote or not self:
            return False
        r = remote
        if r.startswith("git_remote:"):
            r = r[len("git_remote:"):]
        parsed = parse_board_slug(r)
        return bool(parsed) and self.matches(*parsed)


def exclusions_of(config: object) -> Exclusions:
    """The exclusions of ``config``; empty for anything not a real HubConfig.

    Callers outside this module go through here so a stand-in config (tests,
    a partial object) never reads as "everything excluded".
    """
    fn = getattr(config, "exclusions", None)
    res = fn() if callable(fn) else None
    return res if isinstance(res, Exclusions) else Exclusions()


# --- Registro externo de clientes (issue #95) ---

CLIENT_REGISTRY_VERSION = 1


@dataclass
class ClientRegistry:
    """A shared, read-only client attribution file (issue #95).

    Format (TOML, ``version = 1``)::

        version = 1
        client_orgs = ["acme"]
        exclude_owners = ["initech"]
        exclude_repos = ["globex/secret-plans"]

        [client_owner_aliases]
        "acme-labs" = "acme"

        [client_overrides]
        "git_remote:github.com/globex/widget-services" = "acme"

    Every key is optional. ``personal_orgs`` is deliberately absent: who is
    "me" is per machine. Merged under the local ``[workspace]`` values (local
    wins on a conflicting key; lists are unioned).
    """
    path: str = ""
    client_orgs: list[str] = field(default_factory=list)
    client_owner_aliases: dict[str, str] = field(default_factory=dict)
    client_overrides: dict[str, str] = field(default_factory=dict)
    exclude_owners: list[str] = field(default_factory=list)
    exclude_repos: list[str] = field(default_factory=list)


# (path, mtime or error) already warned about — a broken registry is logged
# once, not on every config reload of the harvester loops.
_registry_warned: set[tuple[str, str]] = set()


def _registry_warn(path: str, why: str) -> None:
    if (path, why) in _registry_warned:
        return
    _registry_warned.add((path, why))
    from hub.log import get as get_logger
    get_logger("Config").warning(
        "Registro de clientes %s: %s; sigo con la config local", path, why)


def _str_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(v).strip() for v in value if str(v).strip()]


def _str_map(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {str(k).strip(): str(v).strip() for k, v in value.items()
            if str(k).strip() and isinstance(v, str) and v.strip()}


def resolve_registry_path(path: str) -> Path:
    """``~`` expanded; a relative path is relative to ``~/.moolmesh``."""
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else CONFIG_DIR / p


def load_client_registry(path: str) -> ClientRegistry | None:
    """Read the shared client registry (read-only, never written).

    Missing file, unreadable TOML or an unknown ``version`` → a warning in
    the log and ``None`` (the local config alone applies). A key with the
    wrong type is skipped with a warning; the rest of the file still applies.
    """
    if not path:
        return None
    p = resolve_registry_path(path)
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        _registry_warn(str(p), "no existe")
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        _registry_warn(str(p), f"no se pudo leer ({type(e).__name__})")
        return None
    version = data.get("version", CLIENT_REGISTRY_VERSION)
    if version != CLIENT_REGISTRY_VERSION:
        _registry_warn(str(p), f"version {version!r} no soportada")
        return None
    reg = ClientRegistry(path=str(p))
    for key, conv in (("client_orgs", _str_list),
                      ("exclude_owners", _str_list),
                      ("exclude_repos", _str_list),
                      ("client_owner_aliases", _str_map),
                      ("client_overrides", _str_map)):
        if key not in data:
            continue
        val = conv(data[key])
        if val is None:
            _registry_warn(str(p), f"'{key}' tiene un tipo inválido, lo ignoro")
            continue
        setattr(reg, key, val)
    return reg


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
    # Emails propios (decisión 1A) — sólo si tienen contenido, como el resto
    # de las listas opcionales.
    if config.user_emails:
        emails = ", ".join(f'"{_toml_escape(e)}"' for e in config.user_emails)
        lines.append(f'emails = [{emails}]')
    if config.timezone:
        lines.append(f'timezone = "{_toml_escape(config.timezone)}"')
    lines.append("")

    # Sección [workspace] (privacidad — escalar; DEBE ir antes de cualquier
    # array-de-tablas o TOML anidaría el flag bajo la última tabla).
    lines.append("[workspace]")
    lines.append(f'hide_project_names = {str(config.hide_project_names).lower()}')
    lines.append(f'filesystem_monitoring = {str(config.filesystem_monitoring).lower()}')
    lines.append(f'auto_attribution = {str(config.auto_attribution).lower()}')
    # Jerarquía cliente/org (issue #29) — arrays/tabla inline; sólo se escriben
    # si tienen contenido, para no ensuciar configs que no usan la capa. Van
    # DENTRO de [workspace] (escalares/inline antes de cualquier array-de-tablas).
    if config.client_orgs:
        orgs = ", ".join(f'"{_toml_escape(o)}"' for o in config.client_orgs)
        lines.append(f'client_orgs = [{orgs}]')
    if config.personal_orgs:
        porgs = ", ".join(f'"{_toml_escape(o)}"' for o in config.personal_orgs)
        lines.append(f'personal_orgs = [{porgs}]')
    if config.client_overrides:
        pairs = ", ".join(
            f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
            for k, v in config.client_overrides.items()
        )
        lines.append(f'client_overrides = {{{pairs}}}')
    # Issue #95 — alias de dueño, registro externo y exclusiones (sólo con
    # contenido; el registro cargado NUNCA se copia aquí).
    if config.client_owner_aliases:
        pairs = ", ".join(
            f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
            for k, v in config.client_owner_aliases.items()
        )
        lines.append(f'client_owner_aliases = {{{pairs}}}')
    if config.client_registry:
        lines.append(f'client_registry = "{_toml_escape(config.client_registry)}"')
    if config.exclude_owners:
        vals = ", ".join(f'"{_toml_escape(o)}"' for o in config.exclude_owners)
        lines.append(f'exclude_owners = [{vals}]')
    if config.exclude_repos:
        vals = ", ".join(f'"{_toml_escape(o)}"' for o in config.exclude_repos)
        lines.append(f'exclude_repos = [{vals}]')
    # Orden por config (decisiones 3/4/5) — mismas reglas: sólo con contenido.
    if config.project_aliases:
        pairs = ", ".join(
            f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
            for k, v in config.project_aliases.items()
        )
        lines.append(f'project_aliases = {{{pairs}}}')
    if config.reference_containers:
        refs = ", ".join(f'"{_toml_escape(p)}"' for p in config.reference_containers)
        lines.append(f'reference_containers = [{refs}]')
    if config.temporary_containers:
        temps = ", ".join(f'"{_toml_escape(p)}"' for p in config.temporary_containers)
        lines.append(f'temporary_containers = [{temps}]')
    lines.append("")

    # Sección [daemon] (issue #76) — sólo si difiere del default, como las
    # listas opcionales; escalares antes de cualquier array-de-tablas.
    daemon_lines = []
    if config.stale_after_minutes != DEFAULT_STALE_AFTER_MINUTES:
        daemon_lines.append(f"stale_after_minutes = {int(config.stale_after_minutes)}")
    if config.full_text_max_chars != DEFAULT_FULL_TEXT_MAX_CHARS:
        daemon_lines.append(f"full_text_max_chars = {int(config.full_text_max_chars)}")
    if daemon_lines:
        lines.append("[daemon]")
        lines.extend(daemon_lines)
        lines.append("")

    # Sección [team] (issue #83) — sólo si se usa: un config sin la capa no
    # cambia al guardarse.
    team_lines = []
    if config.per_contributor:
        team_lines.append("per_contributor = true")
    if config.contributor_aliases:
        pairs = ", ".join(
            f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
            for k, v in config.contributor_aliases.items()
        )
        team_lines.append(f"contributor_aliases = {{{pairs}}}")
    if config.bot_logins:
        vals = ", ".join(f'"{_toml_escape(b)}"' for b in config.bot_logins)
        team_lines.append(f"bot_logins = [{vals}]")
    if team_lines:
        lines.append("[team]")
        lines.extend(team_lines)
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
        if normalize_repo_kind(repo.kind) != "code":
            lines.append(f'kind = "{normalize_repo_kind(repo.kind)}"')
        lines.append("")

    # Sección [[workspace_roots]] - array de tablas (issue #21, opt-in)
    for root in config.workspace_roots:
        excludes = ", ".join(f'"{_toml_escape(e)}"' for e in root.excludes)
        lines.append("[[workspace_roots]]")
        lines.append(f'path = "{_toml_escape(root.path)}"')
        lines.append(f'max_depth = {int(root.max_depth)}')
        lines.append(f'excludes = [{excludes}]')
        lines.append(f'added_at = "{_toml_escape(root.added_at)}"')
        lines.append("")

    return "\n".join(lines)


def _parse_stale_after(value) -> int:
    """``[daemon] stale_after_minutes``: a positive integer, else the default."""
    if isinstance(value, bool):
        return DEFAULT_STALE_AFTER_MINUTES
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STALE_AFTER_MINUTES
    return minutes if minutes > 0 else DEFAULT_STALE_AFTER_MINUTES


def _parse_full_text_max(value) -> int:
    """``[daemon] full_text_max_chars``: an integer >= 0 (0 = no cap), else the default."""
    if isinstance(value, bool):
        return DEFAULT_FULL_TEXT_MAX_CHARS
    try:
        chars = int(value)
    except (TypeError, ValueError):
        return DEFAULT_FULL_TEXT_MAX_CHARS
    return chars if chars >= 0 else DEFAULT_FULL_TEXT_MAX_CHARS


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
        # Emails propios (decisión 1A) — lista, vacía por defecto.
        config.user_emails = [
            str(e).strip() for e in user.get("emails", []) if str(e).strip()
        ]
        # Zona horaria (issue #81) — string; cualquier otro tipo se ignora.
        tz_name = user.get("timezone", "")
        if isinstance(tz_name, str):
            config.timezone = tz_name.strip()
        else:
            from hub.log import get as get_logger
            get_logger("Config").warning(
                "[user] timezone debe ser un nombre IANA entre comillas; "
                "uso la zona del sistema")
    
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
                kind=normalize_repo_kind(repo_data.get("kind", "code")),
            )
            config.repos.append(repo)

    # Parse [workspace] section (privacidad, issue #21)
    if "workspace" in data:
        ws = data["workspace"]
        config.hide_project_names = bool(ws.get("hide_project_names", False))
        # Ausente → True: un config viejo (sin la clave) mantiene el monitoreo on.
        config.filesystem_monitoring = bool(ws.get("filesystem_monitoring", True))
        # Ausente → True (issue #39): configs viejos obtienen la atribución agendada.
        config.auto_attribution = bool(ws.get("auto_attribution", True))
        # Jerarquía cliente/org (issue #29) — todas opcionales.
        config.client_orgs = [str(o) for o in ws.get("client_orgs", []) if str(o)]
        config.personal_orgs = [str(o) for o in ws.get("personal_orgs", []) if str(o)]
        config.client_overrides = {
            str(k): str(v) for k, v in dict(ws.get("client_overrides", {})).items()
            if str(k) and str(v)
        }
        # Issue #95 — alias de dueño, registro externo, exclusiones.
        config.client_owner_aliases = _str_map(
            ws.get("client_owner_aliases", {})) or {}
        reg_path = ws.get("client_registry", "")
        config.client_registry = reg_path.strip() if isinstance(reg_path, str) else ""
        config.exclude_owners = _str_list(ws.get("exclude_owners", [])) or []
        config.exclude_repos = _str_list(ws.get("exclude_repos", [])) or []
        if config.client_registry:
            config.registry = load_client_registry(config.client_registry)
        # Orden por config (decisiones 3/4/5) — todas opcionales, default vacío.
        config.project_aliases = {
            str(k): str(v) for k, v in dict(ws.get("project_aliases", {})).items()
            if str(k) and str(v)
        }
        config.reference_containers = [
            str(p) for p in ws.get("reference_containers", []) if str(p).strip()
        ]
        config.temporary_containers = [
            str(p) for p in ws.get("temporary_containers", []) if str(p).strip()
        ]

    # Parse [daemon] section (issue #76) — umbral de datos viejos.
    if "daemon" in data:
        config.stale_after_minutes = _parse_stale_after(
            data["daemon"].get("stale_after_minutes"))
        if "full_text_max_chars" in data["daemon"]:
            config.full_text_max_chars = _parse_full_text_max(
                data["daemon"]["full_text_max_chars"])

    # Parse [team] section (issue #83) — desglose por persona, opt-in.
    if isinstance(data.get("team"), dict):
        team = data["team"]
        flag = team.get("per_contributor", False)
        if isinstance(flag, bool):
            config.per_contributor = flag
        else:
            from hub.log import get as get_logger
            get_logger("Config").warning(
                "[team] per_contributor debe ser true o false; queda apagado")
        config.contributor_aliases = _str_map(
            team.get("contributor_aliases", {})) or {}
        config.bot_logins = _str_list(team.get("bot_logins", [])) or []

    # Parse [[workspace_roots]] array (issue #21, opt-in)
    if "workspace_roots" in data:
        for root_data in data["workspace_roots"]:
            root = WorkspaceRoot(
                path=root_data.get("path", ""),
                max_depth=int(root_data.get("max_depth", WORKSPACE_ROOT_DEFAULT_MAX_DEPTH)),
                excludes=list(root_data.get("excludes", [])),
                added_at=root_data.get("added_at", ""),
            )
            if root.path:
                config.workspace_roots.append(root)

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
        from hub.git_utils import no_window
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **no_window(),
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


# Path prefix of a board repo registered without a local clone (#93). The
# ``path`` stays the repo's unique key in config and github.db; this prefix
# makes it unmistakably not a filesystem path.
BOARD_PATH_PREFIX = "board:"


def parse_board_slug(spec: str) -> tuple[str, str] | None:
    """``owner/repo`` (or ``github.com/owner/repo``, or a GitHub URL) → parts.

    Returns ``None`` when ``spec`` is not of that shape.
    """
    s = (spec or "").strip()
    if s.startswith(BOARD_PATH_PREFIX):
        s = s[len(BOARD_PATH_PREFIX):]
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if s.startswith("github.com/"):
        s = s[len("github.com/"):]
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    if any(ch.isspace() for ch in s):
        return None
    return parts[0], parts[1]


def board_repo_config(spec: str) -> RepoConfig:
    """RepoConfig for a board repo given as ``owner/repo`` (#93).

    No local clone is needed: a board is an issues-only backlog, so no git
    runs for it. Raises ValueError when ``spec`` is not ``owner/repo``.
    """
    parsed = parse_board_slug(spec)
    if parsed is None:
        raise ValueError(f"Se esperaba owner/repo: {spec}")
    owner, repo_name = parsed
    remote = f"github.com/{owner}/{repo_name}"
    return RepoConfig(
        path=f"{BOARD_PATH_PREFIX}{remote}",
        remote_url=remote,
        owner=owner,
        repo=repo_name,
        added_at=datetime.now(timezone.utc).isoformat(),
        github_enabled=True,
        kind="board",
    )


# Path prefix of a remote-only code repo (#94): followed through the GitHub
# API alone, with no local clone. Same key shape as a board, distinct kind.
REMOTE_PATH_PREFIX = "remote:"


def remote_repo_config(spec: str) -> RepoConfig:
    """RepoConfig for a remote-only code repo given as ``owner/repo`` (#94).

    Raises ValueError when ``spec`` is not ``owner/repo``.
    """
    parsed = parse_board_slug(spec)
    if parsed is None:
        raise ValueError(f"Se esperaba owner/repo: {spec}")
    owner, repo_name = parsed
    remote = f"github.com/{owner}/{repo_name}"
    return RepoConfig(
        path=f"{REMOTE_PATH_PREFIX}{remote}",
        remote_url=remote,
        owner=owner,
        repo=repo_name,
        added_at=datetime.now(timezone.utc).isoformat(),
        github_enabled=True,
        kind="remote",
    )


def keyed_repo_path(kind: str, owner: str, repo: str) -> str:
    """The config/github.db key of a clone-less repo (board or remote)."""
    prefix = BOARD_PATH_PREFIX if kind == "board" else REMOTE_PATH_PREFIX
    return f"{prefix}github.com/{owner}/{repo}"


def find_repo_by_slug(repos: list[RepoConfig], spec: str) -> RepoConfig | None:
    """The clone-less registered repo whose ``owner/repo`` matches ``spec``.

    Case-insensitive (GitHub names are). Only board/remote repos: a code repo
    is addressed by its path. ``None`` when ``spec`` is not ``owner/repo`` or
    nothing matches.
    """
    parsed = parse_board_slug(spec)
    if parsed is None:
        return None
    want = (parsed[0].lower(), parsed[1].lower())
    for r in repos:
        if not repo_has_git(r) and (r.owner.lower(), r.repo.lower()) == want:
            return r
    return None


def rename_repo_in_config(old_path: str, owner: str, repo: str) -> str | None:
    """Point the config entry ``old_path`` at ``owner/repo`` (#94).

    A transfer or a rename seen through the repo's numeric GitHub id. Updates
    ``owner``, ``repo`` and ``remote_url``; a clone-less repo's key (``path``)
    is re-derived, a code repo keeps its filesystem path. Returns the entry's
    (possibly new) path, or ``None`` when ``old_path`` is not registered.
    """
    config = load_config()
    for r in config.repos:
        if r.path != old_path:
            continue
        r.owner = owner
        r.repo = repo
        r.remote_url = f"github.com/{owner}/{repo}"
        if not repo_has_git(r):
            r.path = keyed_repo_path(r.kind, owner, repo)
        save_config(config)
        return r.path
    return None


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


# --- Workspace roots (issue #21, filesystem watcher opt-in) ---

def add_workspace_root(
    path: str,
    max_depth: int = WORKSPACE_ROOT_DEFAULT_MAX_DEPTH,
    excludes: list[str] | None = None,
) -> WorkspaceRoot:
    """Marca una raíz para el filesystem watcher y la persiste en config.

    Idempotente: si el path ya está marcado, actualiza max_depth/excludes en
    lugar de duplicar. No requiere que sea un repo git (ése es justamente el
    caso de uso: carpetas sin git ni sesiones). Root ``/`` es válido.
    """
    config = load_config()
    normalized = _normalize_path(path)
    root = WorkspaceRoot(
        path=normalized,
        max_depth=int(max_depth),
        excludes=list(excludes or []),
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    # Reemplazar si ya existe (idempotente), preservando added_at original.
    existing_at = None
    for r in config.workspace_roots:
        if _normalize_path(r.path) == normalized:
            existing_at = r.added_at
    if existing_at:
        root.added_at = existing_at
    config.workspace_roots = [
        r for r in config.workspace_roots if _normalize_path(r.path) != normalized
    ]
    config.workspace_roots.append(root)
    save_config(config)
    return root


def remove_workspace_root(path: str) -> bool:
    """Desmarca una raíz. Retorna True si existía."""
    config = load_config()
    normalized = _normalize_path(path)
    original = len(config.workspace_roots)
    config.workspace_roots = [
        r for r in config.workspace_roots if _normalize_path(r.path) != normalized
    ]
    if len(config.workspace_roots) < original:
        save_config(config)
        return True
    return False


def list_workspace_roots() -> list[WorkspaceRoot]:
    """Lista raíces marcadas para el filesystem watcher."""
    return load_config().workspace_roots


def masked_label(label: str, hide: bool) -> str:
    """Enmascara un label visible cuando ``hide_project_names`` está activo.

    Pura y determinística: hashea el *display label* (remote/root/dir), NUNCA
    el ``workspace_key`` — la key es el handle de join y debe seguir estable
    para ``get_workspace_sessions``/``get_workspace_touches``.
    """
    if not hide or not label:
        return label
    import hashlib
    digest = hashlib.sha256(label.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"hidden:{digest}"
