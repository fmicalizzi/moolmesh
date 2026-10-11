"""Utilidades git para MoolMesh."""
from __future__ import annotations

import re
import subprocess

from hub.log import get

_log = get("GitUtils")

# git escribe UTF-8 en su salida; forzamos la decodificación a UTF-8 con
# errors="replace" para no depender del locale del proceso (en Windows cae a
# cp1252 y revienta con mensajes de commit acentuados — issue #32).
_DECODE = {"encoding": "utf-8", "errors": "replace"}


def is_git_repo(path: str) -> bool:
    """Verifica que path es un repositorio git."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-dir"],
            capture_output=True,
            timeout=5,
            **_DECODE,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def get_remote_url(path: str, remote: str = "origin") -> str | None:
    """Obtiene URL del remote. None si no tiene."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", remote],
            capture_output=True,
            timeout=5,
            **_DECODE,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


# [esquema://[user@]]  o  [user@] (scp-like), host github.com (con www. opcional)
# y exactamente dos segmentos: owner/repo. Anclada: `notgithub.com/o/r`,
# `github.com.evil.com/o/r` y `evil.com/github.com/o/r` NO son GitHub (#69).
_GITHUB_REMOTE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?|(?:[\w.+-]+@)?)"
    r"(?:www\.)?github\.com(?::\d+)?[:/]"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$",
    re.IGNORECASE,
)


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Parsea owner y repo de una URL de GitHub.

    Soporta (host sin distinguir mayúsculas; owner/repo conservan las suyas):
    - git@github.com:owner/repo.git
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo  (con o sin barra final, con o sin .git)
    - https://user@github.com/owner/repo.git
    - ssh://git@github.com/owner/repo.git
    - git://github.com/owner/repo
    - repos con punto en el nombre (owner/my.repo)

    Dos formas "laxas" se mantienen A PROPÓSITO (decisión #72, fijadas con tests):
    - sin esquema, ``github.com/owner/repo`` (también ``github.com:owner/repo``):
      es el formato canónico que ``add_repo`` guarda en ``[[repos]].remote_url``
      y con el que el resto del sistema identifica un repo registrado —
      rechazarla rompería repos ya registrados;
    - ``www.github.com`` (con o sin esquema): inofensivo, sigue siendo GitHub.

    Normaliza con strip(), sin barras finales y sin un único ``.git`` final.
    Otros hosts (gitlab.com, notgithub.com, github.com.evil.com) → None.

    Returns (owner, repo) o None si no es GitHub.
    """
    if not url:
        return None
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    m = _GITHUB_REMOTE.match(url)
    if m:
        return m.group("owner"), m.group("repo")
    return None


def git_fetch(path: str) -> bool:
    """Ejecuta git fetch --all --quiet. Retorna True si exitoso."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "fetch", "--all", "--quiet"],
            capture_output=True,
            timeout=60,
            **_DECODE,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def get_remote_refs(path: str) -> dict[str, str]:
    """Retorna dict de ref_name -> commit_sha para todas las refs remotas.

    Usa: git for-each-ref refs/remotes/ --format='%(refname) %(objectname)'
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "for-each-ref", "refs/remotes/",
             "--format=%(refname) %(objectname)"],
            capture_output=True,
            timeout=10,
            **_DECODE,
        )
        refs = {}
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line and " " in line:
                    ref, sha = line.split(" ", 1)
                    refs[ref] = sha
        return refs
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}


def git_log_range(path: str, old_sha: str, new_sha: str) -> str | None:
    """Obtiene git log entre dos SHAs con formato estructurado + numstat.

    Format: SHA|author_name|author_email|timestamp|parent_shas|subject
    Seguido de --numstat output.

    Devuelve el stdout (posiblemente "" cuando no hay commits) en éxito, o
    None si git falló (rc≠0 o excepción). El caller debe distinguir None
    ("git falló") de "" ("0 commits") para no reportar un fallo como cero (§4).
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", f"{old_sha}..{new_sha}",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            timeout=30,
            **_DECODE,
        )
        if result.returncode == 0:
            return result.stdout
        # No confundir un fallo de git con "no hay commits" (§4): logueamos
        # con contexto y devolvemos None en vez de "" en silencio.
        _log.warning("git log %s..%s falló en %s (rc=%s): %s",
                     old_sha, new_sha, path, result.returncode,
                     result.stderr.strip())
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _log.warning("git log %s..%s no ejecutó en %s", old_sha, new_sha,
                     path, exc_info=True)
        return None


def git_log_since(path: str, since_date: str) -> str | None:
    """Obtiene git log desde una fecha (para ingesta inicial).

    Mismo formato que git_log_range pero con --since y --all. Devuelve "" en
    éxito sin commits y None si git falló (ver git_log_range).
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", "--all", f"--since={since_date}",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            timeout=60,
            **_DECODE,
        )
        if result.returncode == 0:
            return result.stdout
        _log.warning("git log --since=%s falló en %s (rc=%s): %s",
                     since_date, path, result.returncode,
                     result.stderr.strip())
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _log.warning("git log --since=%s no ejecutó en %s", since_date, path,
                     exc_info=True)
        return None


def git_log_all(path: str) -> str | None:
    """Obtiene todo el git log sin filtro de fecha.

    Para repos grandes puede tardar varios minutos.
    Timeout extendido a 5 minutos. Devuelve "" en éxito sin commits y None si
    git falló (ver git_log_range).
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", "--all",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            timeout=300,
            **_DECODE,
        )
        if result.returncode == 0:
            return result.stdout
        _log.warning("git log --all falló en %s (rc=%s): %s",
                     path, result.returncode, result.stderr.strip())
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _log.warning("git log --all no ejecutó en %s", path, exc_info=True)
        return None
