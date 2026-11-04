"""Per-PR stats and closing references (#80) — synthetic Acme fixtures, no network."""
from __future__ import annotations

import argparse
import json
import sqlite3
from unittest.mock import patch

import pytest

from hub.cache.git_store import GitStore, _SCHEMA
from hub.config import RepoConfig
from hub.harvesters.github_harvester import GitHubHarvester, fetch_pr_stats
from hub.integrations.github_client import GitHubClient


def _repo_cfg(path="/Users/u/work/acme/widget", owner="acme", repo="widget"):
    return RepoConfig(path=path, remote_url=f"github.com/{owner}/{repo}",
                      owner=owner, repo=repo, added_at="2026-10-01T10:00:00",
                      github_enabled=True)


@pytest.fixture
def store(tmp_path):
    s = GitStore(db_path=tmp_path / "github.db")
    s.register_repo(_repo_cfg())
    yield s
    s.close()


def _pr_item(number, updated="2026-10-20T12:00:00Z", merged=True):
    return {
        "number": number, "title": f"PR {number}", "state": "closed" if merged else "open",
        "user": {"login": "ownerhandle"}, "assignees": [], "labels": [],
        "created_at": "2026-10-19T12:00:00Z", "updated_at": updated,
        "closed_at": updated if merged else None, "body": "",
        "pull_request": {"merged_at": updated if merged else None},
    }


def _issue_item(number):
    return {
        "number": number, "title": f"Issue {number}", "state": "open",
        "user": {"login": "ownerhandle"}, "assignees": [], "labels": [],
        "created_at": "2026-10-19T12:00:00Z", "updated_at": "2026-10-19T12:00:00Z",
        "body": "",
    }


class FakeClient:
    """Stands in for GitHubClient: canned issues, records stats calls."""

    PR_STATS_BATCH = GitHubClient.PR_STATS_BATCH

    def __init__(self, items, stats=None, fail=False):
        self.items = items
        self.stats = stats or {}
        self.fail = fail
        self.stats_calls: list[list[int]] = []

    def list_issues(self, owner, repo, state="all", etag=None):
        return 200, list(self.items), None

    def get_pr_stats(self, owner, repo, numbers):
        self.stats_calls.append(list(numbers))
        if self.fail:
            return None
        out = {}
        for n in numbers:
            base = self.stats.get(n, {"additions": n, "deletions": 1,
                                      "changed_files": 1, "closing_refs": []})
            upd = next((i["updated_at"] for i in self.items
                        if i["number"] == n), None)
            out[n] = {"number": n, "updated_at": upd,
                      "closing_refs_total": len(base["closing_refs"]), **base}
        return out


# Merged PR #7 closes acme/widget#3 and globex/widget-services#3: another repo,
# same issue number.
_PR7_STATS = {
    "additions": 120, "deletions": 30, "changed_files": 4,
    "closing_refs": [("acme/widget", 3), ("globex/widget-services", 3)],
}


class TestMigration:
    def test_migrates_previous_db_without_touching_issues(self, tmp_path):
        """A v1.28.1 github.db (migrations 1-5) gains the tables; rows intact."""
        db = tmp_path / "github.db"
        conn = sqlite3.connect(db)
        conn.executescript(_SCHEMA)
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                     " name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        for v in range(1, 6):
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                         (v, f"m{v}", "2026-10-01T00:00:00+00:00"))
        conn.execute("INSERT INTO repos (path, remote_url, owner, repo_name, added_at)"
                     " VALUES ('/Users/u/work/acme/widget', 'github.com/acme/widget',"
                     " 'acme', 'widget', '2026-10-01T10:00:00')")
        conn.execute("INSERT INTO github_issues (repo_id, number, title, state,"
                     " created_at, updated_at, is_pull_request, pr_state)"
                     " VALUES (1, 7, 'PR 7', 'closed', '2026-10-19T09:00:00',"
                     " '2026-10-20T09:00:00', 1, 'merged')")
        conn.commit()
        before = conn.execute("SELECT * FROM github_issues").fetchall()
        conn.close()

        for _ in range(2):  # second open: idempotent, nothing re-applied
            s = GitStore(db_path=db)
            s.close()

        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        after = conn.execute("SELECT * FROM github_issues").fetchall()
        conn.close()
        assert {"github_pr_stats", "github_pr_closing_refs"} <= tables
        assert versions == [1, 2, 3, 4, 5, 6, 7]
        assert after == before

    def test_previous_db_pr_listed_as_missing(self, store):
        store.upsert_issues(1, [{"number": 7, "title": "x", "state": "closed",
                                 "created_at": "2026-10-19T09:00:00",
                                 "updated_at": "2026-10-20T09:00:00",
                                 "is_pull_request": 1}])
        assert store.list_prs_missing_stats(1) == [7]


class TestStore:
    def test_upsert_and_read_qualified_refs(self, store):
        store.upsert_pr_stats(1, [{"number": 7, "updated_at": "2026-10-20T12:00:00Z",
                                   "closing_refs_total": 2, **_PR7_STATS}])
        st = store.get_pr_stats(1)[7]
        assert (st["additions"], st["deletions"], st["changed_files"]) == (120, 30, 4)
        assert st["closes"] == ["acme/widget#3", "globex/widget-services#3"]

    def test_upsert_replaces_refs(self, store):
        store.upsert_pr_stats(1, [{"number": 7, "updated_at": "a", **_PR7_STATS}])
        store.upsert_pr_stats(1, [{"number": 7, "updated_at": "b", "additions": 1,
                                   "deletions": 0, "changed_files": 1,
                                   "closing_refs": [("initech/widget", 9)]}])
        assert store.get_pr_stats(1)[7]["closes"] == ["initech/widget#9"]
        assert store.get_pr_stats_updated_map(1) == {7: "b"}

    def test_cascade_on_repo_remove(self, store):
        store.upsert_pr_stats(1, [{"number": 7, "updated_at": "a", **_PR7_STATS}])
        store.remove_repo("/Users/u/work/acme/widget")
        conn = store._get_conn()
        assert conn.execute("SELECT COUNT(*) FROM github_pr_stats").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM github_pr_closing_refs").fetchone()[0] == 0


class TestClient:
    def _graphql_response(self, data, errors=None):
        body = {"data": data}
        if errors:
            body["errors"] = errors
        return 200, {}, json.dumps(body).encode()

    def test_parses_batch_with_cross_repo_ref_and_missing_pr(self):
        client = GitHubClient("t")
        data = {"repository": {
            "p7": {"number": 7, "additions": 120, "deletions": 30, "changedFiles": 4,
                   "updatedAt": "2026-10-20T12:00:00Z",
                   "closingIssuesReferences": {"totalCount": 2, "nodes": [
                       {"number": 3, "repository": {"nameWithOwner": "acme/widget"}},
                       {"number": 3, "repository": {
                           "nameWithOwner": "globex/widget-services"}},
                   ]}},
            "p99": None,
        }}
        errors = [{"type": "NOT_FOUND", "path": ["repository", "p99"]}]
        with patch.object(client, "_request",
                          return_value=self._graphql_response(data, errors)) as req:
            res = client.get_pr_stats("acme", "widget", [7, 99])
        assert req.call_count == 1
        sent = json.loads(req.call_args.kwargs["body"])
        assert "p7: pullRequest(number: 7)" in sent["query"]
        assert "closingIssuesReferences" in sent["query"]
        assert sent["variables"] == {"owner": "acme", "repo": "widget"}
        assert set(res) == {7}
        assert res[7]["closing_refs"] == [("acme/widget", 3),
                                          ("globex/widget-services", 3)]
        assert res[7]["changed_files"] == 4
        assert res[7]["closing_refs_total"] == 2

    def test_failure_returns_none(self):
        client = GitHubClient("t")
        with patch.object(client, "_request", return_value=(401, {}, b"")):
            assert client.get_pr_stats("acme", "widget", [7]) is None

    def test_batch_capped(self):
        client = GitHubClient("t")
        with patch.object(client, "_request",
                          return_value=self._graphql_response({"repository": {}})) as req:
            client.get_pr_stats("acme", "widget", list(range(1, 40)))
        q = json.loads(req.call_args.kwargs["body"])["query"]
        assert q.count("pullRequest(") == GitHubClient.PR_STATS_BATCH

    def test_graphql_errors_still_none_by_default(self):
        client = GitHubClient("t")
        with patch.object(client, "_request", return_value=self._graphql_response(
                {"x": 1}, [{"message": "boom"}])):
            assert client.graphql("query { x }") is None
            assert client.graphql("query { x }", allow_partial=True) == {"x": 1}


class TestHarvesterSync:
    def test_merged_pr_stats_and_cross_repo_ref(self, store):
        client = FakeClient([_pr_item(7), _issue_item(3)], stats={7: _PR7_STATS})
        GitHubHarvester(store, client)._poll_issues_prs(1, "acme", "widget")
        assert client.stats_calls == [[7]]  # issues are not asked for
        merged = store.get_pr_pipeline(1)["merged"]
        assert merged[0]["number"] == 7
        assert (merged[0]["additions"], merged[0]["deletions"],
                merged[0]["changed_files"]) == (120, 30, 4)
        assert merged[0]["closes"] == ["acme/widget#3", "globex/widget-services#3"]

    def test_resync_is_idempotent(self, store):
        client = FakeClient([_pr_item(7)], stats={7: _PR7_STATS})
        h = GitHubHarvester(store, client)
        h._poll_issues_prs(1, "acme", "widget")
        rows = store._get_conn().execute(
            "SELECT pr_number, additions, pr_updated_at FROM github_pr_stats").fetchall()
        refs = store._get_conn().execute(
            "SELECT * FROM github_pr_closing_refs ORDER BY 3, 4").fetchall()
        h._poll_issues_prs(1, "acme", "widget")
        assert client.stats_calls == [[7]]  # unchanged PR: no second call
        assert store._get_conn().execute(
            "SELECT pr_number, additions, pr_updated_at FROM github_pr_stats"
        ).fetchall() == rows
        assert store._get_conn().execute(
            "SELECT * FROM github_pr_closing_refs ORDER BY 3, 4").fetchall() == refs

    def test_updated_pr_refetched(self, store):
        client = FakeClient([_pr_item(7)], stats={7: _PR7_STATS})
        h = GitHubHarvester(store, client)
        h._poll_issues_prs(1, "acme", "widget")
        client.items = [_pr_item(7, updated="2026-10-21T08:00:00Z")]
        client.stats = {7: {"additions": 125, "deletions": 30, "changed_files": 5,
                            "closing_refs": [("acme/widget", 3)]}}
        h._poll_issues_prs(1, "acme", "widget")
        assert client.stats_calls == [[7], [7]]
        st = store.get_pr_stats(1)[7]
        assert (st["additions"], st["closes"]) == (125, ["acme/widget#3"])

    def test_stats_failure_keeps_sync(self, store):
        client = FakeClient([_pr_item(7), _issue_item(3)], fail=True)
        GitHubHarvester(store, client)._poll_issues_prs(1, "acme", "widget")
        assert {i["number"] for i in store.get_issues(1)} == {3, 7}
        assert store.get_pr_stats(1) == {}
        pr = store.get_pr_pipeline(1)["merged"][0]
        assert pr["additions"] is None and pr["closes"] == []

    def test_stats_exception_keeps_sync(self, store):
        client = FakeClient([_pr_item(7)])
        client.get_pr_stats = lambda *a: (_ for _ in ()).throw(ValueError("x"))
        GitHubHarvester(store, client)._poll_issues_prs(1, "acme", "widget")
        assert [i["number"] for i in store.get_issues(1)] == [7]

    def test_per_poll_cap(self, store):
        client = FakeClient([_pr_item(n) for n in range(1, 61)])
        GitHubHarvester(store, client)._poll_issues_prs(1, "acme", "widget")
        asked = [n for call in client.stats_calls for n in call]
        assert len(asked) == GitHubHarvester.MAX_PR_STATS_PER_POLL
        assert all(len(c) <= GitHubClient.PR_STATS_BATCH for c in client.stats_calls)
        assert len(store.list_prs_missing_stats(1)) == 10

    def test_existing_unchanged_prs_not_fetched_by_poll(self, store):
        """PRs cached before the upgrade are left to the backfill."""
        store.upsert_issues(1, [{
            "number": 5, "title": "old", "state": "closed",
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "is_pull_request": 1}])
        client = FakeClient([_pr_item(5, updated="2026-10-02T00:00:00Z"), _pr_item(7)])
        GitHubHarvester(store, client)._poll_issues_prs(1, "acme", "widget")
        assert client.stats_calls == [[7]]
        assert store.list_prs_missing_stats(1) == [5]


class TestBackfill:
    def _seed(self, store, numbers):
        store.upsert_issues(1, [{
            "number": n, "title": f"PR {n}", "state": "closed",
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "is_pull_request": 1} for n in numbers])

    def test_fetch_helper_batches(self, store):
        self._seed(store, range(1, 31))
        client = FakeClient([])
        stored, failed = fetch_pr_stats(store, client, 1, "acme", "widget",
                                        store.list_prs_missing_stats(1))
        assert (stored, failed) == (30, 0)
        assert [len(c) for c in client.stats_calls] == [25, 5]

    def _run(self, tmp_path, store, client, **kw):
        from hub import cli
        cfg = type("C", (), {"repos": [_repo_cfg()]})()
        args = argparse.Namespace(path="/Users/u/work/acme/widget",
                                  all_registered=kw.get("all_registered", True),
                                  limit=kw.get("limit", 200))
        with patch("hub.config.load_config", return_value=cfg), \
                patch.object(cli, "_github_client_for", return_value=client), \
                patch.object(GitStore, "DEFAULT_DB_PATH", store.db_path):
            cli.cmd_repo_backfill_pr_stats(args)

    def test_command_limit_and_resume(self, tmp_path, store, capsys):
        self._seed(store, range(1, 31))
        client = FakeClient([])
        self._run(tmp_path, store, client, limit=20)
        assert len(store.list_prs_missing_stats(1)) == 10
        # Newest first
        assert sorted(store.get_pr_stats(1)) == list(range(11, 31))
        self._run(tmp_path, store, client, limit=20)
        assert store.list_prs_missing_stats(1) == []
        self._run(tmp_path, store, client, limit=20)  # nothing left: no call
        assert sum(len(c) for c in client.stats_calls) == 30
        assert "PR stats" in capsys.readouterr().out

    def test_command_failure_exits_nonzero(self, tmp_path, store):
        self._seed(store, [1, 2])
        with pytest.raises(SystemExit) as e:
            self._run(tmp_path, store, FakeClient([], fail=True))
        assert e.value.code == 1
        assert store.list_prs_missing_stats(1) == [2, 1]

    def test_help_documents_command(self, capsys):
        from hub import cli
        with patch("sys.argv", ["mool", "repo", "backfill-pr-stats", "--help"]), \
                pytest.raises(SystemExit):
            cli.main()
        out = capsys.readouterr().out
        assert "--limit" in out and "--all-registered" in out
        assert "Resumable" in out
