"""Utilidades git para MoolMesh."""
from __future__ import annotations

import re
import subprocess


def is_git_repo(path: str) -> bool:
    """Verifica que path es un repositorio git."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-dir"],
            capture_output=True,
            timeout=5
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
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Parsea owner y repo de una URL de GitHub.

    Soporta:
    - git@github.com:owner/repo.git
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo
    - ssh://git@github.com/owner/repo.git

    Returns (owner, repo) o None si no es GitHub.
    """
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",  # SSH y HTTPS
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1), m.group(2)
    return None


def git_fetch(path: str) -> bool:
    """Ejecuta git fetch --all --quiet. Retorna True si exitoso."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "fetch", "--all", "--quiet"],
            capture_output=True,
            timeout=60
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
            text=True,
            timeout=10
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


def git_log_range(path: str, old_sha: str, new_sha: str) -> str:
    """Obtiene git log entre dos SHAs con formato estructurado + numstat.

    Format: SHA|author_name|author_email|timestamp|parent_shas|subject
    Seguido de --numstat output.
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", f"{old_sha}..{new_sha}",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def git_log_since(path: str, since_date: str) -> str:
    """Obtiene git log desde una fecha (para ingesta inicial).

    Mismo formato que git_log_range pero con --since y --all.
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", "--all", f"--since={since_date}",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def git_log_all(path: str) -> str:
    """Obtiene todo el git log sin filtro de fecha.

    Para repos grandes puede tardar varios minutos.
    Timeout extendido a 5 minutos.
    """
    try:
        fmt = "%H|%an|%ae|%aI|%P|%s"
        result = subprocess.run(
            ["git", "-C", path, "log", "--all",
             f"--format={fmt}", "--numstat"],
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
