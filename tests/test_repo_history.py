"""Full git history (#59): --all-registered, the history_complete flag and the
'desde <fecha>' coverage label.

`repo add/sync --all` marks the repo's history complete in github.db; where it
is not, the production view carries `outcome_complete=False` + the earliest
ingested commit date. `repo sync --all-registered` runs bounded and resumable,
one error per repo never cutting the rest. The GitHub issues/PRs history is also
capped (1000 items in the poller) — `--all` backfills it fully.
"""

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hub.cache.git_store import GitStore
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity, resolve_dir
from hub.harvesters.git_harvester import GitHarvester, GIT_READ_FAILED
from hub.integrations.github_client import GitHubClient
from hub.mcp_server import _get_portfolio_production, _portfolio_production
from tests.test_workspace_store import _mkrepo


class TestHistoryCompleteFlag:
    def test_default_false_and_mark(self, tmp_path):
        from hub.config import RepoConfig
        s = GitStore(tmp_path / "github.db")
        try:
            rid = s.register_repo(RepoConfig(
                path="/x/r", remote_url="github.com/o/r", owner="o", repo="r",
                added_at="2026-01-01T00:00:00"))
            assert s.is_history_complete(rid) is False
            s.mark_history_complete(rid)
            assert s.is_history_complete(rid) is True
            assert s.list_repos()[0]["history_complete"] is True
        finally:
            s.close()

    def test_reregistering_preserves_flag(self, tmp_path):
        from hub.config import RepoConfig
        s = GitStore(tmp_path / "github.db")
        try:
            cfg = RepoConfig(path="/x/r", remote_url="github.com/o/r",
                             owner="o", repo="r", added_at="2026-01-01T00:00:00")
            rid = s.register_repo(cfg)
            s.mark_history_complete(rid)
            assert s.register_repo(cfg) == rid     # UPSERT, not REPLACE
            assert s.is_history_complete(rid) is True
        finally:
            s.close()

    def test_migration_adds_column_to_legacy_db(self, tmp_path):
        db = tmp_path / "legacy.db"
        c = sqlite3.connect(db)
        # Pre-#59 schema + the 3 previous migrations recorded as applied.
        c.execute("""CREATE TABLE repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            remote_url TEXT NOT NULL, owner TEXT NOT NULL, repo_name TEXT NOT NULL,
            added_at TEXT NOT NULL, last_fetch_at TEXT)""")
        c.execute("""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        c.executemany("INSERT INTO schema_migrations VALUES (?,?,?)",
                      [(1, "normalize_timestamps", "x"),
                       (2, "extract_branches", "x"),
                       (3, "utc_to_local", "x")])
        c.execute("INSERT INTO repos (path, remote_url, owner, repo_name,"
                  " added_at) VALUES ('/x/r','github.com/o/r','o','r','x')")
        c.commit()
        c.close()
        s = GitStore(db)
        try:
            assert s.is_history_complete(1) is False
            versions = {r[0] for r in s._conn.execute(
                "SELECT version FROM schema_migrations")}
            assert 4 in versions
        finally:
            s.close()


class TestFullHistoryHelper:
    """`_full_history_ingest` marca `history_complete` sólo cuando TODAS las
    capas aplicables corrieron: git sin acotar + (si aplica) issues/PRs."""

    @pytest.fixture
    def store(self, tmp_path):
        s = GitStore(tmp_path / "github.db")
        yield s
        s.close()

    def _register(self, store, github_enabled=True):
        from hub.config import RepoConfig
        store.register_repo(RepoConfig(
            path="/x/r", remote_url="github.com/o/r", owner="o", repo="r",
            added_at="x", github_enabled=github_enabled))
        return store.get_repo_id("/x/r")

    def _harvester(self, store, count):
        h = MagicMock()
        h.ingest_history.return_value = count
        return h

    def test_local_repo_marks_with_git_alone(self, store):
        from hub.cli import _full_history_ingest
        rid = self._register(store, github_enabled=False)
        res = _full_history_ingest(
            store, rid, MagicMock(github_enabled=False, path="/x/r"),
            self._harvester(store, 7), None)
        assert res == {"ok": True, "commits": 7, "items": 0,
                       "complete": True, "reason": None}
        assert store.is_history_complete(rid) is True

    def test_github_enabled_without_client_is_partial(self, store):
        from hub.cli import _full_history_ingest
        rid = self._register(store)
        res = _full_history_ingest(
            store, rid, MagicMock(github_enabled=True, path="/x/r"),
            self._harvester(store, 2), None)
        assert res["ok"] is True and res["complete"] is False
        assert store.is_history_complete(rid) is False

    def test_github_backfill_failure_keeps_partial(self, store):
        from hub.cli import _full_history_ingest
        rid = self._register(store)
        client = MagicMock()
        client.list_issues.return_value = (500, None, None)
        res = _full_history_ingest(
            store, rid, MagicMock(github_enabled=True, path="/x/r"),
            self._harvester(store, 2), client)
        assert res["complete"] is False and res["reason"] == "github"
        assert store.is_history_complete(rid) is False

    def test_git_failure_is_not_ok(self, store):
        from hub.cli import _full_history_ingest
        rid = self._register(store)
        res = _full_history_ingest(
            store, rid, MagicMock(github_enabled=True, path="/x/r"),
            self._harvester(store, GIT_READ_FAILED), MagicMock())
        assert res["ok"] is False and res["reason"] == "git"
        assert store.is_history_complete(rid) is False


class TestGithubClientUnbounded:
    def test_max_pages_none_fetches_past_the_poller_cap(self):
        client = GitHubClient("tok")
        pages: list[int] = []

        def fake_rest_get(path, params=None, etag=None):
            page = (params or {}).get("page", 1)
            pages.append(page)
            if page <= 12:
                return 200, [{"number": n} for n in range(100)], None
            return 200, [], None

        client.rest_get = fake_rest_get  # type: ignore[method-assign]
        status, data, _ = client.list_issues("o", "r", per_page=100,
                                             max_pages=None)
        assert status == 200
        assert len(data) == 1200       # > the 1000-item poller cap
        assert max(pages) == 13        # the empty page ends the walk

    def test_default_cap_still_applies_to_the_poller(self):
        client = GitHubClient("tok")

        def fake_rest_get(path, params=None, etag=None):
            return 200, [{"number": n} for n in range(100)], None

        client.rest_get = fake_rest_get  # type: ignore[method-assign]
        _, data, _ = client.list_issues("o", "r", per_page=100)
        assert len(data) == 1000       # 10 pages default


def _repo(name, owner="acme", enabled=True):
    r = MagicMock()
    r.path = f"/x/{name}"
    r.owner = owner
    r.repo = name
    r.github_enabled = enabled
    return r


class TestSyncAllRegistered:
    def _args(self, **kw):
        base = dict(path=".", days=14, all_history=False, all_registered=True,
                    no_github=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_one_error_does_not_cut_the_rest_and_summary_counts(self, capsys):
        from hub.cli import cmd_repo_sync
        repos = [_repo("a"), _repo("b"), _repo("c")]
        store = MagicMock()
        store.get_repo_id.side_effect = [1, 2, 3]
        harvester = MagicMock()
        harvester.ingest_history.side_effect = [5, GIT_READ_FAILED, 3]
        with patch("hub.config.load_config") as lc, \
             patch("hub.cache.git_store.GitStore", return_value=store), \
             patch("hub.harvesters.git_harvester.GitHarvester",
                   return_value=harvester):
            lc.return_value = MagicMock(repos=repos)
            with pytest.raises(SystemExit) as exc:
                cmd_repo_sync(self._args())
        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "2/3 repos" in out and "8 new commits" in out
        assert "git read failed" in out
        assert store.get_repo_id.call_count == 3   # kept going

    def test_repo_missing_from_store_is_an_error_not_a_crash(self, capsys):
        from hub.cli import cmd_repo_sync
        store = MagicMock()
        store.get_repo_id.return_value = None
        with patch("hub.config.load_config") as lc, \
             patch("hub.cache.git_store.GitStore", return_value=store), \
             patch("hub.harvesters.git_harvester.GitHarvester"):
            lc.return_value = MagicMock(repos=[_repo("a")])
            with pytest.raises(SystemExit) as exc:
                cmd_repo_sync(self._args())
        assert exc.value.code == 1
        assert "not in GitStore" in capsys.readouterr().out

    def test_all_backfills_full_github_history_for_enabled_repos(self, capsys):
        from hub.cli import cmd_repo_sync
        repos = [_repo("a"), _repo("b", enabled=False)]
        store = MagicMock()
        store.get_repo_id.side_effect = [1, 2]
        harvester = MagicMock()
        harvester.ingest_history.return_value = 2
        client = MagicMock()
        client.list_issues.return_value = (
            200, [{"number": 1, "title": "t", "state": "open",
                   "created_at": "x", "updated_at": "y"}], None)
        with patch("hub.config.load_config") as lc, \
             patch("hub.config.get_github_token", return_value="tok"), \
             patch("hub.cache.git_store.GitStore", return_value=store), \
             patch("hub.harvesters.git_harvester.GitHarvester",
                   return_value=harvester), \
             patch("hub.integrations.github_client.GitHubClient",
                   return_value=client):
            lc.return_value = MagicMock(repos=repos)
            cmd_repo_sync(self._args(all_history=True, no_github=False))
        # Only the github-enabled repo was backfilled, with the unbounded walk.
        assert client.list_issues.call_count == 1
        assert client.list_issues.call_args.kwargs["max_pages"] is None
        store.upsert_issues.assert_called_once()
        out = capsys.readouterr().out
        assert "1 issues/PRs" in out

    def test_no_token_skips_github_without_failing(self, capsys):
        from hub.cli import cmd_repo_sync
        store = MagicMock()
        store.get_repo_id.return_value = 1
        harvester = MagicMock()
        harvester.ingest_history.return_value = 0
        with patch("hub.config.load_config") as lc, \
             patch("hub.config.get_github_token", return_value=None), \
             patch("hub.cache.git_store.GitStore", return_value=store), \
             patch("hub.harvesters.git_harvester.GitHarvester",
                   return_value=harvester):
            lc.return_value = MagicMock(repos=[_repo("a")])
            cmd_repo_sync(self._args(all_history=True, no_github=False))
        out = capsys.readouterr().out
        assert "No GitHub token" in out
        assert "skipped" in out


class TestHistoryCoverage:
    def _fixture(self, tmp_path, *, complete: bool, commits: list[str]):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        key = resolve_dir(str(repo)).key
        wdb = tmp_path / "workspace.db"
        s = WorkspaceStore(wdb)
        wid = s.upsert_workspace(resolve_dir(str(repo)))
        with s._lock:
            s._conn.execute(
                """INSERT OR REPLACE INTO workspace_classification
                   (workspace_id, category, subtype, role, project_key,
                    project_label, resolved_via, classified_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (wid, "A", "project", "project", key, "acme/R", "self", "x"))
            s._conn.commit()
        s.close()
        gh = tmp_path / "github.db"
        c = sqlite3.connect(gh)
        c.execute("""CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT,
                     remote_url TEXT, owner TEXT, repo_name TEXT,
                     history_complete INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE git_commits (id INTEGER PRIMARY KEY,
                     repo_id INTEGER, sha TEXT, timestamp TEXT)""")
        c.execute("INSERT INTO repos VALUES (1, ?, 'github.com/acme/R', 'acme',"
                  " 'R', ?)", (str(repo), 1 if complete else 0))
        for i, ts in enumerate(commits):
            c.execute("INSERT INTO git_commits (repo_id, sha, timestamp)"
                      " VALUES (1, ?, ?)", (f"{i}" * 40, ts))
        c.commit()
        c.close()
        return str(wdb), str(gh), key

    def test_partial_history_reports_since(self, tmp_path):
        wdb, gh, key = self._fixture(
            tmp_path, complete=False,
            commits=["2026-06-22T10:00:00", "2026-09-01T10:00:00"])
        cov = WorkspaceStore.read_history_coverage(wdb, gh)
        assert cov[key] == {"complete": False, "since": "2026-06-22"}

    def test_complete_history(self, tmp_path):
        wdb, gh, key = self._fixture(
            tmp_path, complete=True, commits=["2026-01-05T10:00:00"])
        assert WorkspaceStore.read_history_coverage(wdb, gh)[key] == {
            "complete": True, "since": "2026-01-05"}

    def test_production_payload_carries_coverage(self, tmp_path):
        """End-to-end: the production row carries outcome_complete/since so the
        UI can label 'desde <fecha>'."""
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        key = resolve_dir(str(repo)).key
        wdb = tmp_path / "workspace.db"
        s = WorkspaceStore(wdb)
        try:
            wid = s.upsert_workspace(resolve_dir(str(repo)))
            with s._lock:
                s._conn.execute(
                    """INSERT OR REPLACE INTO workspace_classification
                       (workspace_id, category, subtype, role, project_key,
                        project_label, resolved_via, classified_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (wid, "A", "project", "project", key, "acme/R", "self", "x"))
                s._conn.execute(
                    """INSERT INTO path_attributions
                       (session_id, provider, file_path, workspace_id,
                        resolved_via, first_seen, via, event_ts, activity_ts)
                       VALUES ('s1','claude',? ,?,'file','x','file',
                               '2026-09-20T10:00:00','2026-09-20T10:00:00')""",
                    (str(repo / "a.py"), wid))
                s._conn.commit()
        finally:
            s.close()
        gh = tmp_path / "github.db"
        c = sqlite3.connect(gh)
        c.execute("""CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT,
                     remote_url TEXT, owner TEXT, repo_name TEXT,
                     history_complete INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE git_commits (id INTEGER PRIMARY KEY,
                     repo_id INTEGER, sha TEXT, timestamp TEXT)""")
        c.execute("""CREATE TABLE github_issues (id INTEGER PRIMARY KEY,
                     repo_id INTEGER, state TEXT, closed_at TEXT,
                     is_pull_request INTEGER DEFAULT 0, pr_merged_at TEXT)""")
        c.execute("INSERT INTO repos VALUES (1, ?, 'github.com/acme/R', 'acme',"
                  " 'R', 0)", (str(repo),))
        c.execute("INSERT INTO git_commits (repo_id, sha, timestamp) VALUES"
                  " (1, ?, '2026-07-01T00:00:00')", ("a" * 40,))
        c.commit()
        c.close()
        out = _portfolio_production(
            str(tmp_path / "absent-events.db"), str(wdb), 30,
            today="2026-09-26", github_db=str(gh))
        row = out["projects"][0]
        assert row["outcome_complete"] is False
        assert row["outcome_since"] == "2026-07-01"
