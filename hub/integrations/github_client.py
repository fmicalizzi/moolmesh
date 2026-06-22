"""Cliente GitHub API — REST + GraphQL, zero dependencies."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from hub import USER_AGENT
from hub.log import get as get_logger

_log = get_logger("GitHubClient")


class GitHubClient:
    """Zero-dependency GitHub API client using urllib.

    Soporta:
    - REST con ETags (conditional requests, 304 no cuenta contra rate limit)
    - GraphQL para Projects v2
    - Rate limit tracking automático
    """

    BASE_URL = "https://api.github.com"
    GRAPHQL_URL = "https://api.github.com/graphql"

    def __init__(self, token: str | None = None):
        self._token = token
        self._rate_remaining: int = 5000
        self._rate_reset: float = 0

    # --- Low-level ---

    def _request(self, method: str, url: str,
                 body: bytes | None = None,
                 headers: dict | None = None,
                 timeout: int = 15) -> tuple[int, dict, bytes]:
        """Raw HTTP request via urllib.request.

        Returns (status_code, response_headers_dict, response_body_bytes).
        Maneja rate limit headers automáticamente.
        """
        hdrs = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            hdrs["Authorization"] = f"Bearer {self._token}"
        if headers:
            hdrs.update(headers)

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            status = resp.status
            resp_headers = dict(resp.headers)
            resp_body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = dict(e.headers)
            resp_body = e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log.debug("GitHub API network error: %s %s — %s", method, url, e)
            return 0, {}, b""  # Network error

        # Track rate limit
        if "X-RateLimit-Remaining" in resp_headers:
            try:
                self._rate_remaining = int(resp_headers["X-RateLimit-Remaining"])
            except ValueError:
                pass

        return status, resp_headers, resp_body

    def rest_get(self, path: str, params: dict | None = None,
                 etag: str | None = None) -> tuple[int, Any, str | None]:
        """GET con ETag support.

        Returns (status, parsed_json_or_None, new_etag_or_None).
        - 200 = data changed, new ETag
        - 304 = not modified (no cuenta contra rate limit)
        - 0   = network error
        """
        url = f"{self.BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {}
        if etag:
            headers["If-None-Match"] = etag

        status, resp_headers, body = self._request("GET", url, headers=headers)

        if status == 304:
            return 304, None, etag
        if status == 200:
            new_etag = resp_headers.get("ETag")
            data = json.loads(body) if body else None
            return 200, data, new_etag

        return status, None, None

    def graphql(self, query: str, variables: dict | None = None) -> dict | None:
        """Execute GraphQL query.

        Returns parsed response dict or None on error.
        """
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        status, _, resp_body = self._request(
            "POST", self.GRAPHQL_URL, body=body, headers=headers
        )

        if status == 200 and resp_body:
            result = json.loads(resp_body)
            if "errors" in result:
                return None  # GraphQL error
            return result.get("data")
        return None

    # --- High-level REST ---

    def list_issues(self, owner: str, repo: str, state: str = "all",
                    since: str | None = None, per_page: int = 100,
                    etag: str | None = None) -> tuple[int, list[dict] | None, str | None]:
        """GET /repos/{owner}/{repo}/issues con filtros.

        Retorna issues Y pull requests (GitHub API las mezcla).
        El campo 'pull_request' indica si es PR.
        """
        params = {"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"}
        if since:
            params["since"] = since
        return self.rest_get(f"/repos/{owner}/{repo}/issues", params, etag)

    def list_pulls(self, owner: str, repo: str, state: str = "all",
                   per_page: int = 100) -> tuple[int, list[dict] | None, str | None]:
        """GET /repos/{owner}/{repo}/pulls — PRs con review info."""
        params = {"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"}
        return self.rest_get(f"/repos/{owner}/{repo}/pulls", params)

    def list_milestones(self, owner: str, repo: str,
                        state: str = "all") -> tuple[int, list[dict] | None, str | None]:
        """GET /repos/{owner}/{repo}/milestones."""
        params = {"state": state, "sort": "due_on", "direction": "asc"}
        return self.rest_get(f"/repos/{owner}/{repo}/milestones", params)

    def get_pr_reviews(self, owner: str, repo: str,
                       pr_number: int) -> tuple[int, list[dict] | None, str | None]:
        """GET /repos/{owner}/{repo}/pulls/{pr_number}/reviews."""
        return self.rest_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews")

    # --- High-level GraphQL ---

    def get_repo_projects_v2(self, owner: str, repo: str) -> list[dict]:
        """Lista Projects v2 de un repo.

        Query documentada en PHASE0_RESULTS.md sección 2.
        """
        query = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            projectsV2(first: 10) {
              nodes { id title number }
            }
          }
        }"""
        data = self.graphql(query, {"owner": owner, "repo": repo})
        if data:
            return data.get("repository", {}).get("projectsV2", {}).get("nodes", [])
        return []

    def get_project_items(self, project_id: str,
                          after: str | None = None) -> tuple[list[dict], str | None, bool]:
        """Obtiene items de un Project v2 con status.

        Query documentada en PHASE0_RESULTS.md sección 2.
        Returns (items, end_cursor, has_next_page).
        """
        query = """
        query($projectId: ID!, $after: String) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100, after: $after) {
                nodes {
                  id
                  fieldValues(first: 10) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2SingleSelectField { name } }
                      }
                    }
                  }
                  content {
                    ... on Issue { title number state
                      assignees(first: 5) { nodes { login } }
                    }
                    ... on PullRequest { title number state merged
                      assignees(first: 5) { nodes { login } }
                    }
                    ... on DraftIssue { title id }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }"""
        variables = {"projectId": project_id}
        if after:
            variables["after"] = after

        data = self.graphql(query, variables)
        if not data or "node" not in data:
            return [], None, False

        items_data = data["node"].get("items", {})
        nodes = items_data.get("nodes", [])
        page_info = items_data.get("pageInfo", {})

        # Parse items — extraer status del field "Status"
        items = []
        for node in nodes:
            content = node.get("content", {})
            if not content:
                continue

            # Buscar campo "Status" en fieldValues
            status = None
            for fv in node.get("fieldValues", {}).get("nodes", []):
                field_info = fv.get("field", {})
                if field_info.get("name", "").lower() == "status":
                    status = fv.get("name")
                    break

            # Determinar tipo de contenido
            if "number" in content and "merged" in content:
                content_type = "PullRequest"
            elif "number" in content:
                content_type = "Issue"
            else:
                content_type = "DraftIssue"

            items.append({
                "item_id": node["id"],
                "content_type": content_type,
                "content_number": content.get("number"),
                "title": content.get("title", ""),
                "state": content.get("state", ""),
                "status": status,
                "assignees": [a["login"] for a in content.get("assignees", {}).get("nodes", [])],
            })

        return items, page_info.get("endCursor"), page_info.get("hasNextPage", False)

    @property
    def rate_remaining(self) -> int:
        return self._rate_remaining
