"""Team-activity mark (issue #36, decisión 1A) + ``[user] emails``.

A project whose hot state is sustained ONLY by commits authored by other people
carries ``team_only``; any own commit / session / fs touch in the state's band
clears it. No emails configured → nothing is ever marked (previous behavior).
NOTHING per-person is exposed: only the boolean travels.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from hub.mcp_server import _user_emails
from hub.config import HubConfig, load_config, save_config
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc
OWN = "me@example.com"


def _make_github_db_with_authors(
    path: Path, repo_path: str, commits: list[tuple[str, str, str]],
) -> Path:
    """One repo; commits: (sha, timestamp, author_email)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE repos (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, remote_url TEXT)""")
    c.execute("""CREATE TABLE git_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        sha TEXT NOT NULL, timestamp TEXT NOT NULL,
        author_email TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE github_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        number INTEGER, title TEXT, state TEXT, author TEXT,
        closed_at TEXT, is_pull_request INTEGER DEFAULT 0, pr_merged_at TEXT)""")
    c.execute("INSERT INTO repos (id, path) VALUES (1, ?)", (repo_path,))
    for sha, ts, email in commits:
        c.execute(
            "INSERT INTO git_commits (repo_id, sha, timestamp, author_email)"
            " VALUES (1, ?, ?, ?)", (sha, ts, email),
        )
    c.commit()
    c.close()
    return path


def _classify(s: WorkspaceStore, wid: int, pk: str) -> None:
    with s._lock:
        s._conn.execute(
            """INSERT OR REPLACE INTO workspace_classification
               (workspace_id, category, subtype, role, project_key,
                project_label, resolved_via, classified_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, "A", "project", "project", pk, pk, "self",
             "2026-01-01T00:00:00"),
        )
        s._conn.commit()


def _setup(tmp_path, *, commits):
    repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
    key = resolve_dir(str(repo)).key
    store = WorkspaceStore(tmp_path / "workspace.db")
    wid = store.upsert_workspace(resolve_dir(str(repo)))
    _classify(store, wid, key)
    gh = _make_github_db_with_authors(
        tmp_path / "github.db", str(repo), commits)
    return store, key, str(gh)


def _now_naive() -> str:
    # git_commits stores naive-local (per _parse_ts) — keep the fixture honest.
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class TestTeamOnly:
    def test_other_commits_only_marks_team(self, tmp_path):
        store, key, gh = _setup(
            tmp_path, commits=[("a" * 40, _now_naive(), "team@example.com")])
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=[OWN])
        finally:
            store.close()
        assert st[key]["state"] == "activo"
        assert st[key]["team_only"] is True

    def test_own_commit_in_window_clears_mark(self, tmp_path):
        store, key, gh = _setup(tmp_path, commits=[
            ("a" * 40, _now_naive(), "team@example.com"),
            ("b" * 40, _now_naive(), OWN.upper()),  # case-insensitive match
        ])
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=[OWN])
        finally:
            store.close()
        assert st[key]["state"] == "activo"
        assert st[key]["team_only"] is False

    def test_local_session_clears_mark(self, tmp_path):
        store, key, gh = _setup(
            tmp_path, commits=[("a" * 40, _now_naive(), "team@example.com")])
        wid = store._conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key = ?", (key,)
        ).fetchone()[0]
        with store._lock:
            store._conn.execute(
                """INSERT INTO path_attributions
                   (session_id, provider, file_path, workspace_id, resolved_via,
                    first_seen, via, event_ts, activity_ts)
                   VALUES ('s1','claude','/x/R/a.py',?,'file',?, 'file', ?, ?)""",
                (wid, _now_naive(), _now_naive(), _now_naive()),
            )
            store._conn.commit()
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=[OWN])
        finally:
            store.close()
        assert st[key]["team_only"] is False

    def test_no_emails_configured_no_mark(self, tmp_path):
        store, key, gh = _setup(
            tmp_path, commits=[("a" * 40, _now_naive(), "team@example.com")])
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=None)
        finally:
            store.close()
        assert st[key]["state"] == "activo"
        assert st[key]["team_only"] is False

    def test_own_activity_outside_band_marks_team(self, tmp_path):
        """An own commit 5d ago with a team commit today: the state is activo
        (team's 3d band) → team_only, even though own is inside the cooling
        band."""
        old = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        store, key, gh = _setup(tmp_path, commits=[
            ("a" * 40, _now_naive(), "team@example.com"),
            ("b" * 40, old, OWN),
        ])
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=[OWN])
        finally:
            store.close()
        assert st[key]["state"] == "activo"
        assert st[key]["team_only"] is True

    def test_no_authors_nor_emails_exposed(self, tmp_path):
        store, key, gh = _setup(
            tmp_path, commits=[("a" * 40, _now_naive(), "team@example.com")])
        try:
            st = store.derive_project_states(
                str(tmp_path / "absent-events.db"), gh,
                now=datetime.now(UTC), own_emails=[OWN])
        finally:
            store.close()
        blob = repr(st)
        assert "team@example.com" not in blob
        assert OWN not in blob
        assert "author" not in blob


class TestUserEmailsConfig:
    @pytest.fixture
    def temp_config_path(self, tmp_path, monkeypatch):
        import hub.config as config_module
        config_dir = tmp_path / ".moolmesh"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.toml"
        original_path = config_module.CONFIG_PATH
        original_dir = config_module.CONFIG_DIR
        config_module.CONFIG_PATH = config_path
        config_module.CONFIG_DIR = config_dir
        yield config_path
        config_module.CONFIG_PATH = original_path
        config_module.CONFIG_DIR = original_dir

    def test_roundtrip(self, temp_config_path):
        save_config(HubConfig(
            github_handle="user",
            user_emails=["Me@Example.com", "second@example.com"],
        ))
        loaded = load_config()
        assert loaded.user_emails == ["Me@Example.com", "second@example.com"]

    def test_absent_is_empty(self, temp_config_path):
        temp_config_path.write_text("[user]\ngithub_handle = \"u\"\n")
        assert load_config().user_emails == []

    def test_empty_not_written(self, temp_config_path):
        save_config(HubConfig(github_handle="u"))
        assert "emails" not in temp_config_path.read_text()

    def test_mcp_user_emails_reads_config(self, temp_config_path, monkeypatch):
        save_config(HubConfig(github_handle="u", user_emails=[OWN]))
        assert _user_emails() == [OWN]
