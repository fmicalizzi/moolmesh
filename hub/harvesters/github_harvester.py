"""GitHub API harvester — polling adaptativo para Issues, PRs, Milestones, Projects."""
from __future__ import annotations

import collections
import json
import threading
import time

from hub.cache.git_store import GitStore
from hub.integrations.github_client import GitHubClient
from hub.log import get as get_logger

_log = get_logger("GitHubHarvester")


class GitHubHarvester:
    """Polls GitHub API para repos registrados.

    Tres threads con intervalos diferentes:
    - Issues/PRs: cada 15s (cambian frecuentemente)
    - Milestones: cada 60s
    - Projects v2: cada 60s
    """

    ISSUE_PR_INTERVAL: float = 15.0
    MILESTONE_INTERVAL: float = 60.0
    PROJECT_INTERVAL: float = 60.0

    def __init__(self, git_store: GitStore, github_client: GitHubClient,
                 sse_buffer: collections.deque | None = None):
        self._store = git_store
        self._client = github_client
        self._sse_buffer = sse_buffer
        self._running = False
        self._threads: list[threading.Thread] = []
        # ETags por repo para conditional requests
        self._etags: dict[str, str] = {}  # "owner/repo/issues" -> etag
        # Cache config para evitar re-read en cada tick
        self._config_cache: dict | None = None
        self._config_cache_time: float = 0
        self._config_cache_ttl: float = 60.0  # 60 segundos

    def start(self) -> None:
        self._running = True
        # Thread 1: Issues/PRs (15s)
        t1 = threading.Thread(target=self._loop_issues_prs, daemon=True)
        # Thread 2: Milestones (60s)
        t2 = threading.Thread(target=self._loop_milestones, daemon=True)
        # Thread 3: Projects v2 (60s)
        t3 = threading.Thread(target=self._loop_projects, daemon=True)
        self._threads = [t1, t2, t3]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._running = False
        for t in self._threads:
            t.join(timeout=5)

    def _loop_issues_prs(self) -> None:
        while self._running:
            try:
                self._poll_all_repos(self._poll_issues_prs)
            except Exception:
                _log.warning("Error polling issues/PRs", exc_info=True)
            time.sleep(self.ISSUE_PR_INTERVAL)

    def _loop_milestones(self) -> None:
        while self._running:
            try:
                self._poll_all_repos(self._poll_milestones)
            except Exception:
                _log.warning("Error polling milestones", exc_info=True)
            time.sleep(self.MILESTONE_INTERVAL)

    def _loop_projects(self) -> None:
        while self._running:
            try:
                self._poll_all_repos(self._poll_projects)
            except Exception:
                _log.warning("Error polling projects", exc_info=True)
            time.sleep(self.PROJECT_INTERVAL)

    def _get_cached_config(self):
        """Retorna config cacheada, recargando si expiró."""
        from hub.config import load_config
        now = time.time()
        if self._config_cache is None or (now - self._config_cache_time) > self._config_cache_ttl:
            self._config_cache = load_config()
            self._config_cache_time = now
        return self._config_cache

    def _poll_all_repos(self, poll_fn) -> None:
        """Itera repos registrados que tengan github_enabled."""
        config = self._get_cached_config()
        for repo_config in config.repos:
            if not self._running or not repo_config.github_enabled:
                continue
            repo_id = self._store.get_repo_id(repo_config.path)
            if repo_id is None:
                continue
            try:
                poll_fn(repo_id, repo_config.owner, repo_config.repo)
            except Exception:
                _log.warning("Error en %s/%s", repo_config.owner, repo_config.repo, exc_info=True)

    def _poll_issues_prs(self, repo_id: int, owner: str, repo: str) -> None:
        """Fetch issues+PRs via REST, detect cambios, upsert en SQLite."""
        etag_key = f"{owner}/{repo}/issues"
        etag = self._etags.get(etag_key)

        status, data, new_etag = self._client.list_issues(
            owner, repo, state="all", etag=etag
        )

        if new_etag:
            self._etags[etag_key] = new_etag
        if status == 304 or data is None:
            return  # Sin cambios

        # Separar issues y PRs, enriquecer PRs con review info
        issues = []
        for item in data:
            is_pr = "pull_request" in item
            issue_dict = {
                "number": item["number"],
                "title": item["title"],
                "state": item["state"],
                "author": item.get("user", {}).get("login", ""),
                "assignees": json.dumps([a["login"] for a in item.get("assignees", [])]),
                "labels": json.dumps([lbl["name"] for lbl in item.get("labels", [])]),
                "milestone_number": item.get("milestone", {}).get("number") if item.get("milestone") else None,
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
                "closed_at": item.get("closed_at"),
                "body": (item.get("body") or "")[:500],  # Truncar para no inflar SQLite
                "is_pull_request": 1 if is_pr else 0,
            }

            if is_pr:
                # Determinar estado del PR
                pr_data = item.get("pull_request", {})
                issue_dict["pr_merged_at"] = pr_data.get("merged_at")
                issue_dict["pr_state"] = self._determine_pr_state(item)
                # head/base branches no vienen en /issues, se rellenan si disponibles
                issue_dict["pr_base_branch"] = ""
                issue_dict["pr_head_branch"] = ""
                issue_dict["pr_review_decision"] = ""

            issues.append(issue_dict)

        self._store.upsert_issues(repo_id, issues)

        # Push cambios significativos a SSE
        if self._sse_buffer is not None and issues:
            self._sse_buffer.append({
                "type": "github_update",
                "subtype": "issues_prs",
                "repo": f"{owner}/{repo}",
                "count": len(issues),
            })

    def _poll_milestones(self, repo_id: int, owner: str, repo: str) -> None:
        """Fetch milestones via REST."""
        status, data, _ = self._client.list_milestones(owner, repo)
        if status != 200 or data is None:
            return

        milestones = []
        for m in data:
            milestones.append({
                "number": m["number"],
                "title": m["title"],
                "state": m["state"],
                "due_on": m.get("due_on"),
                "open_issues": m.get("open_issues", 0),
                "closed_issues": m.get("closed_issues", 0),
                "updated_at": m.get("updated_at", ""),
            })

        self._store.upsert_milestones(repo_id, milestones)

    def _poll_projects(self, repo_id: int, owner: str, repo: str) -> None:
        """Fetch Projects v2 via GraphQL."""
        projects = self._client.get_repo_projects_v2(owner, repo)
        if not projects:
            return

        for project in projects:
            project_id = project["id"]
            project_title = project["title"]

            # Fetch all items (paginated)
            all_items = []
            cursor = None
            while True:
                items, next_cursor, has_next = self._client.get_project_items(
                    project_id, after=cursor
                )
                for item in items:
                    item["project_title"] = project_title
                all_items.extend(items)
                if not has_next:
                    break
                cursor = next_cursor

            self._store.upsert_project_items(repo_id, all_items)

    @staticmethod
    def _determine_pr_state(issue_data: dict) -> str:
        """Mapea un issue (que es PR) a estado del pipeline.

        Estados: draft, review, approved, merged, closed
        """
        pr_data = issue_data.get("pull_request", {})

        if pr_data.get("merged_at"):
            return "merged"
        if issue_data.get("state") == "closed":
            return "closed"
        if issue_data.get("draft"):
            return "draft"

        # Sin info de reviews en /issues endpoint, default a "review"
        return "review"
