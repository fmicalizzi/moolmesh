"""SessionCommitLinker — Correlaciona sesiones AI con commits Git."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from hub.cache.git_store import GitStore
from hub.cache.event_store import EventStore


class SessionCommitLinker:
    """Correlaciona commits con sesiones AI y issues GitHub.

    3 estrategias de linking:
    1. Co-Author: "Co-Authored-By: Claude" → ai_assisted=1
    2. Issue refs: "#42", "fixes #45" → match con github_issues
    3. Timestamp proximity: commit dentro de 10min de una sesión activa
    """

    # Ventana de proximidad temporal para linking por timestamp
    TIMESTAMP_WINDOW = timedelta(minutes=10)

    def __init__(self, git_store: GitStore, event_store: EventStore):
        self._git_store = git_store
        self._event_store = event_store

    def link_by_coauthor(self, commit: dict) -> bool:
        """Detecta si el commit fue asistido por AI via Co-Authored-By.

        Retorna True si se detectó AI co-author.
        El campo ai_assisted ya debería estar seteado por git_harvester,
        pero esto lo re-verifica y puede corregir.
        """
        co_authors = commit.get("co_authors", [])
        if not co_authors:
            message = commit.get("message", "")
            co_authors = re.findall(
                r"Co-Authored-By:\s*(.+)",
                message,
                re.IGNORECASE
            )

        ai_keywords = ["claude", "copilot", "gpt", "codex", "ai", "anthropic", "openai"]
        for author in co_authors:
            if any(kw in author.lower() for kw in ai_keywords):
                return True
        return False

    def link_by_issue_ref(self, commit: dict) -> list[int]:
        """Extrae issue numbers referenciados en el commit message.

        Retorna list de issue numbers: [42, 45]
        """
        issue_refs = commit.get("issue_refs", [])
        if not issue_refs:
            message = commit.get("message", "")
            issue_refs = re.findall(r"#(\d+)", message)

        numbers = []
        for ref in issue_refs:
            if isinstance(ref, str):
                # Puede ser "#42" o "42"
                num = ref.lstrip("#")
                try:
                    numbers.append(int(num))
                except ValueError:
                    pass
            elif isinstance(ref, int):
                numbers.append(ref)

        return numbers

    def link_by_timestamp(self, commit: dict, repo_path: str) -> str | None:
        """Busca sesión AI activa al momento del commit.

        Busca en event_store eventos dentro de TIMESTAMP_WINDOW del commit.
        Retorna session_id si encuentra match, None si no.
        """
        ts_str = commit.get("timestamp", "")
        if not ts_str:
            return None

        try:
            # Soportar tanto naive (nuevo) como aware (datos legacy)
            commit_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if commit_ts.tzinfo is not None:
                commit_ts = commit_ts.astimezone().replace(tzinfo=None)
        except (ValueError, TypeError):
            return None

        # Ventana de búsqueda
        since = (commit_ts - self.TIMESTAMP_WINDOW).isoformat()
        until = (commit_ts + self.TIMESTAMP_WINDOW).isoformat()

        # Buscar eventos en esa ventana
        events = self._event_store.query(
            since=since,
            limit=50,
        )

        # Filtrar por cwd que coincida con repo_path
        for ev in events:
            ev_ts = ev.get("timestamp", "")
            ev_cwd = ev.get("cwd", "")

            if ev_cwd and repo_path in ev_cwd:
                if ev_ts >= since and ev_ts <= until:
                    session_id = ev.get("session_id")
                    if session_id:
                        return session_id

        return None

    def run_batch(self, repo_id: int, repo_path: str = "") -> dict:
        """Correlaciona todos los commits sin linkear de un repo.

        Retorna stats: {processed, ai_assisted, issue_linked, session_linked}
        Persiste ai_assisted y session_id en la base de datos.
        """
        commits = self._git_store.get_commits(repo_id, limit=500)

        stats = {
            "processed": 0,
            "ai_assisted": 0,
            "issue_linked": 0,
            "session_linked": 0,
        }

        for commit in commits:
            stats["processed"] += 1
            sha = commit.get("sha")
            if not sha:
                continue

            ai_assisted = False
            session_id = None

            # 1. Co-Author check
            if self.link_by_coauthor(commit):
                ai_assisted = True
                stats["ai_assisted"] += 1

            # 2. Issue refs
            issues = self.link_by_issue_ref(commit)
            if issues:
                stats["issue_linked"] += 1

            # 3. Timestamp proximity (solo si tenemos repo_path)
            if repo_path and not commit.get("session_id"):
                session_id = self.link_by_timestamp(commit, repo_path)
                if session_id:
                    stats["session_linked"] += 1

            # Persistir correlaciones si cambiaron
            if ai_assisted or session_id:
                self._git_store.update_commit_correlations(
                    repo_id, sha,
                    ai_assisted=ai_assisted or commit.get("ai_assisted", False),
                    session_id=session_id or commit.get("session_id")
                )

        return stats
