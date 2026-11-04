"""Git harvester — periodic fetch + commit ingestion."""
from __future__ import annotations

import collections
import re
import threading
from datetime import datetime, timedelta, timezone

from hub.cache.git_store import GitStore
from hub.git_utils import (
    git_fetch, get_remote_refs, git_log_range, git_log_since, git_log_all
)
from hub.log import get as get_logger

_log = get_logger("GitHarvester")

# Sentinel de ingest_history: git falló leyendo el historial (rc≠0 o excepción),
# distinto de 0 (git ok, sin commits nuevos). El caller CLI lo traduce a error +
# exit no-cero en vez del engañoso "0 commits" (issue #34).
GIT_READ_FAILED: int = -1


class GitHarvester:
    """Periodically fetches registered repos and ingests new commits."""

    FETCH_INTERVAL: float = 120.0  # 2 minutos

    def __init__(self, git_store: GitStore,
                 sse_buffer: collections.deque | None = None):
        self._store = git_store
        self._sse_buffer = sse_buffer
        self._running = False
        # Set by stop(): the loop waits on it instead of sleeping (#96).
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Excluded repos already logged as skipped (#95), logged once each.
        self._excluded_logged: set = set()

    def start(self) -> None:
        """Arranca en daemon thread."""
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._harvest_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Detiene el harvester."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _harvest_loop(self) -> None:
        """Loop principal: para cada repo registrado, fetch + ingest."""
        while self._running:
            try:
                repos = self._store.list_repos()
                excl = self._exclusions()
                for repo in repos:
                    if not self._running:
                        break
                    if repo.get("kind") in ("board", "remote"):
                        continue  # no clone (#93 board, #94 remote): no git
                    if excl.matches(repo.get("owner"), repo.get("repo_name")):
                        self._skip_excluded(repo)
                        continue  # confidential (#95): never read
                    self._fetch_and_ingest(repo)
            except Exception:
                _log.warning("Error en ciclo de harvesting", exc_info=True)
            self._stop_event.wait(self.FETCH_INTERVAL)

    def _exclusions(self):
        """Excluded owners/repos (#95), re-read each cycle from the config."""
        from hub.config import Exclusions, exclusions_of, load_config
        try:
            return exclusions_of(load_config())
        except Exception:
            _log.warning("No pude leer las exclusiones de la config", exc_info=True)
            return Exclusions()

    def _skip_excluded(self, repo: dict) -> None:
        """Log once per repo that an excluded, already-registered repo is skipped."""
        rid = repo.get("id")
        if rid in self._excluded_logged:
            return
        self._excluded_logged.add(rid)
        _log.info("Repo excluido (exclude_owners/exclude_repos), no se lee: %s/%s",
                  repo.get("owner", "?"), repo.get("repo_name", "?"))

    def _fetch_and_ingest(self, repo: dict) -> None:
        """Fetch un repo, detectar refs nuevas, ingestar commits."""
        try:
            repo_id = repo["id"]
            repo_path = repo["path"]

            # 1. Fetch
            if not git_fetch(repo_path):
                return  # Error de red o repo no accesible

            self._store.update_last_fetch(repo_id)

            # 2. Comparar refs
            old_refs = self._store.get_refs(repo_id)
            new_refs = get_remote_refs(repo_path)

            if not new_refs:
                return

            # 3. Detectar branches que avanzaron
            new_commits = []
            failed_refs: set[str] = set()
            for ref, new_sha in new_refs.items():
                old_sha = old_refs.get(ref)
                if old_sha == new_sha:
                    continue  # Sin cambios en esta ref

                if old_sha is None:
                    # Branch nueva — no procesamos aquí, se maneja en ingesta inicial
                    continue

                # Branch avanzó: obtener commits entre old y new
                log_output = git_log_range(repo_path, old_sha, new_sha)
                if log_output is None:
                    # git falló en este rango (ya logueado en git_utils). En el
                    # daemon no abortamos: saltamos esta ref y seguimos con el
                    # resto del repo/loop, pero NO avanzamos su cursor (paso 5),
                    # para reintentar en el próximo ciclo en vez de perder esos
                    # commits en silencio (§4).
                    failed_refs.add(ref)
                    continue
                if log_output:
                    branch_name = ref.replace("refs/remotes/origin/", "")
                    commits = self._parse_git_log(log_output, branch_name)
                    new_commits.extend(commits)

            # 4. Almacenar
            if new_commits:
                stored = self._store.store_commits(repo_id, new_commits)
                # Push a SSE si hay buffer
                # Nota: git_log_range(old_sha, new_sha) produce rango atómico sin duplicados
                if self._sse_buffer is not None and stored > 0:
                    for commit in new_commits:
                        self._sse_buffer.append({
                            "type": "git_commit",
                            "repo": f"{repo.get('owner', '')}/{repo.get('repo_name', '')}",
                            **commit
                        })

            # 5. Actualizar refs — excluyendo las que fallaron, para no avanzar
            # su cursor y reintentarlas en el próximo ciclo (§4).
            refs_to_store = ({r: s for r, s in new_refs.items()
                              if r not in failed_refs}
                             if failed_refs else new_refs)
            if refs_to_store:
                self._store.update_refs(repo_id, refs_to_store)
        except Exception:
            _log.warning("Error procesando repo %s", repo.get("path", "?"), exc_info=True)
            return

    @staticmethod
    def _normalize_timestamp(ts: str) -> str:
        """Normaliza timestamp ISO 8601 a hora local naive (sin zona horaria).

        Guardamos en hora local para que los digests agrupen por fecha de calendario
        del desarrollador. Un commit a las 23:30 local debe aparecer en el digest
        de ese día, no del siguiente (que ocurría al normalizar a UTC).

        Ej: 2026-04-16T23:30:00-03:00 -> 2026-04-16T23:30:00
        """
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(ts, fmt)
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt.isoformat(timespec="seconds")
            except ValueError:
                continue
        return ts

    def _parse_git_log(self, output: str, branch: str = "") -> list[dict]:
        """Parsea output de git log con formato estructurado + numstat.

        Formato esperado por línea de commit:
        SHA|author_name|author_email|timestamp|parent_shas|subject

        Seguido de líneas numstat (opcionales):
        insertions\tdeletions\tfile_path
        """
        commits = []
        current_commit = None
        current_files = []

        for line in output.split("\n"):
            line = line.strip()
            if not line:
                if current_commit:
                    # Finalizar commit anterior
                    current_commit["files"] = current_files
                    current_commit["files_changed"] = len(current_files)
                    current_commit["insertions"] = sum(f.get("insertions", 0) for f in current_files)
                    current_commit["deletions"] = sum(f.get("deletions", 0) for f in current_files)
                    commits.append(current_commit)
                    current_commit = None
                    current_files = []
                continue

            parts = line.split("|", 5)
            if len(parts) == 6 and len(parts[0]) == 40:
                # Es una línea de commit
                if current_commit:
                    current_commit["files"] = current_files
                    current_commit["files_changed"] = len(current_files)
                    current_commit["insertions"] = sum(f.get("insertions", 0) for f in current_files)
                    current_commit["deletions"] = sum(f.get("deletions", 0) for f in current_files)
                    commits.append(current_commit)
                    current_files = []

                sha, author_name, author_email, timestamp, parents, subject = parts
                parent_list = parents.split() if parents else []
                is_merge = len(parent_list) > 1

                issue_refs = self._extract_issue_refs(subject)
                ai_assisted, co_authors = self._detect_ai_coauthor(subject)

                # Extraer branch del mensaje de merge si no tenemos branch
                commit_branch = branch
                if is_merge and not commit_branch:
                    extracted = self._extract_branch_from_merge(subject)
                    if extracted:
                        commit_branch = extracted

                current_commit = {
                    "sha": sha,
                    "author_name": author_name,
                    "author_email": author_email,
                    "timestamp": self._normalize_timestamp(timestamp),
                    "message": subject,
                    "is_merge": is_merge,
                    "branch": commit_branch,
                    "issue_refs": issue_refs,
                    "co_authors": co_authors,
                    "ai_assisted": ai_assisted,
                }
            elif "\t" in line:
                # Es una línea numstat: ins\tdel\tfile
                numstat_parts = line.split("\t", 2)
                if len(numstat_parts) == 3:
                    ins, dels, fpath = numstat_parts
                    try:
                        current_files.append({
                            "file_path": fpath,
                            "insertions": int(ins) if ins != "-" else 0,
                            "deletions": int(dels) if dels != "-" else 0,
                        })
                    except ValueError:
                        pass

        # No olvidar el último commit
        if current_commit:
            current_commit["files"] = current_files
            current_commit["files_changed"] = len(current_files)
            current_commit["insertions"] = sum(f.get("insertions", 0) for f in current_files)
            current_commit["deletions"] = sum(f.get("deletions", 0) for f in current_files)
            commits.append(current_commit)

        return commits

    @staticmethod
    def _extract_issue_refs(message: str) -> list[str]:
        """Extrae referencias a issues: #42, fixes #45, closes #12."""
        return re.findall(r"(?:fixes?|closes?|resolves?)?\s*#(\d+)", message, re.IGNORECASE)

    @staticmethod
    def _extract_branch_from_merge(message: str) -> str | None:
        """Extrae nombre de branch de mensaje de merge commit.

        Pattern: "Merge pull request #N from owner/branch-name"
        Retorna branch-name o None.
        """
        # Pattern: "Merge pull request #123 from owner/branch-name"
        m = re.search(r"Merge pull request #\d+ from\s+\S+/(.+?)(?:\s*$|\s+\(|$)", message)
        if m:
            return m.group(1).strip()
        # Alternative: "Merge branch 'feature/name'"
        m = re.search(r"Merge branch ['\"](.+?)['\"]", message)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _detect_ai_coauthor(message: str) -> tuple[bool, list[str]]:
        """Detecta Co-Authored-By. Flag si contiene AI agent."""
        co_authors = re.findall(r"Co-Authored-By:\s*(.+)", message, re.IGNORECASE)
        ai_keywords = ["claude", "codex", "copilot", "gpt", "anthropic", "openai"]
        ai_assisted = any(
            any(kw in ca.lower() for kw in ai_keywords)
            for ca in co_authors
        )
        return ai_assisted, co_authors

    def ingest_history(self, repo_path: str, days: int | None = 14) -> int:
        """Ingesta inicial. days=None = historial completo.

        Se llama al hacer `mool repo add` / `mool repo sync`. Retorna el número
        de commits ingestados, o GIT_READ_FAILED (-1) si git falló leyendo el
        historial — distinto de 0 (git ok, sin commits nuevos), para que el
        caller CLI reporte el fallo en vez del engañoso "0 commits" (#34).
        """
        repo_id = self._store.get_repo_id(repo_path)
        if repo_id is None:
            # Repo no está en el store: ingesta imposible. No es "0 commits" —
            # señalamos el fallo hacia arriba igual que un fallo de git (#34/§4).
            _log.warning("ingest_history: repo %s no está en el store", repo_path)
            return GIT_READ_FAILED

        if days is None:
            log_output = git_log_all(repo_path)
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            log_output = git_log_since(repo_path, since)

        if log_output is None:
            # git falló (ya logueado en git_utils): señalamos hacia arriba.
            return GIT_READ_FAILED
        if not log_output:
            # git ok, sin commits nuevos.
            return 0

        commits = self._parse_git_log(log_output)
        stored = self._store.store_commits(repo_id, commits)

        # Guardar refs actuales como baseline
        refs = get_remote_refs(repo_path)
        if refs:
            self._store.update_refs(repo_id, refs)

        return stored
