"""Tests for WorkspaceStore + the read-only backfill pass (issue #20)."""

import sqlite3
import time
from pathlib import Path

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import WorkspaceIdentity
from hub.mcp_server import (
    _get_portfolio,
    _get_session_workspaces,
    _get_workspace_activity,
    _get_workspace_sessions,
    _list_workspaces,
)


def _mkrepo(root: Path, name: str, remote: str | None = None) -> Path:
    d = root / name
    (d / ".git").mkdir(parents=True)
    cfg = "[core]\n\tbare = false\n"
    if remote is not None:
        cfg += f'[remote "origin"]\n\turl = {remote}\n'
    (d / ".git" / "config").write_text(cfg)
    return d


def _make_events_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (session_id, provider, file_path, cwd)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
        event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
        tokens_json TEXT, tool_name TEXT, file_path TEXT, model TEXT, cwd TEXT,
        fingerprint TEXT, created_at REAL NOT NULL)""")
    for sess, prov, fp, cwd in rows:
        c.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " session_id, tool_name, file_path, cwd, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (prov, "p", "tool_use", "2026-01-01T00:00:00", "s", sess, "Edit", fp, cwd, time.time()),
        )
    c.commit()
    c.close()
    return path


@pytest.fixture
def store(tmp_path) -> WorkspaceStore:
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s
    s.close()


class TestSchema:
    def test_tables_created(self, store):
        with store._lock:
            tables = {
                r[0] for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"workspaces", "path_attributions", "schema_migrations"}.issubset(tables)


class TestUpsertAndSelfHeal:
    def test_upsert_is_stable(self, store):
        ident = WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote", remote_url="h/o/r")
        a = store.upsert_workspace(ident)
        b = store.upsert_workspace(ident)
        assert a == b

    def test_rerun_repoints_attribution_when_repo_gains_remote(self, tmp_path, store):
        """The real self-heal path: a checkout with no remote resolves to a
        git_root workspace; after it gains a remote, a re-run repoints the *same*
        (session, provider, file_path) attribution to the git_remote workspace.

        (A directory gaining a remote produces a *different* workspace_key, so
        the healing happens on ``path_attributions.workspace_id`` via ON CONFLICT
        DO UPDATE — not by mutating a workspace row in place.)
        """
        r = _mkrepo(tmp_path, "proj", remote=None)
        fp = str(r / "x.py")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", fp, None)])

        store.backfill_from_events(ev)
        ws = store.get_session_workspaces("s1")
        assert len(ws) == 1
        assert ws[0]["kind"] == "git_root"

        # The checkout gains an origin remote.
        (r / ".git" / "config").write_text(
            '[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:acme/proj.git\n'
        )
        store.backfill_from_events(ev)

        ws2 = store.get_session_workspaces("s1")
        assert len(ws2) == 1  # attribution repointed, not duplicated
        assert ws2[0]["kind"] == "git_remote"
        assert ws2[0]["workspace_key"] == "git_remote:github.com/acme/proj"
        with store._lock:
            n = store._conn.execute(
                "SELECT COUNT(*) FROM path_attributions WHERE session_id='s1'"
            ).fetchone()[0]
        assert n == 1  # one edge, resolved_via updated in place

    def test_first_seen_pinned_across_upserts(self, store):
        ident = WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote", remote_url="h/o/r")
        wid = store.upsert_workspace(ident)
        with store._lock:
            first = store._conn.execute(
                "SELECT first_seen FROM workspaces WHERE id=?", (wid,)
            ).fetchone()[0]
        store.upsert_workspace(
            WorkspaceIdentity(key="git_remote:h/o/r", kind="git_remote",
                              remote_url="h/o/r", root_path="/new/path")
        )
        with store._lock:
            row = store._conn.execute(
                "SELECT first_seen, root_path FROM workspaces WHERE id=?", (wid,)
            ).fetchone()
        assert row[0] == first          # first_seen pinned
        assert row[1] == "/new/path"    # evidence refreshed


class TestBackfillMxN:
    def test_one_session_many_workspaces(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        B = _mkrepo(tmp_path, "B", "https://github.com/acme/B")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
            ("s1", "claude", str(B / "y.py"), None),
        ])
        result = store.backfill_from_events(ev)
        assert result["attributed"] == 2
        ws = store.get_session_workspaces("s1")
        keys = {w["workspace_key"] for w in ws}
        assert keys == {"git_remote:github.com/acme/a", "git_remote:github.com/acme/b"}

    def test_one_workspace_many_sessions(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
            ("s2", "codex", str(A / "z.py"), None),
        ])
        store.backfill_from_events(ev)
        sessions = store.get_workspace_sessions("git_remote:github.com/acme/a")
        pairs = {(s["session_id"], s["provider"]) for s in sessions}
        assert pairs == {("s1", "claude"), ("s2", "codex")}

    def test_three_ladder_rungs_and_skips(self, tmp_path, store):
        remote = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        rootonly = _mkrepo(tmp_path, "L", remote=None)
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(remote / "a.py"), None),      # git_remote
            ("s1", "claude", str(rootonly / "b.py"), None),    # git_root
            ("s1", "claude", "/gone/dir/c.py", None),          # path_hash (nonexistent)
            ("s1", "claude", "gh issue list --repo x", None),  # skipped (non-absolute)
        ])
        result = store.backfill_from_events(ev)
        assert result["attributed"] == 3
        assert result["skipped_non_absolute"] == 1
        kinds = {w["kind"] for w in store.get_session_workspaces("s1")}
        assert kinds == {"git_remote", "git_root", "path_hash"}

    def test_provider_disambiguates_same_session_id(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("shared", "claude", str(A / "x.py"), None),
            ("shared", "codex", str(A / "x.py"), None),
        ])
        store.backfill_from_events(ev)
        # same session_id + file, two providers → two distinct attributions
        sessions = store.get_workspace_sessions("git_remote:github.com/acme/a")
        assert len(sessions) == 2

    def test_backfill_idempotent(self, tmp_path, store):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [
            ("s1", "claude", str(A / "x.py"), None),
        ])
        r1 = store.backfill_from_events(ev)
        r2 = store.backfill_from_events(ev)
        assert r1["workspaces"] == r2["workspaces"] == 1
        with store._lock:
            n = store._conn.execute("SELECT COUNT(*) FROM path_attributions").fetchone()[0]
        assert n == 1

    def test_events_db_never_written(self, tmp_path, store):
        """The backfill opens events.db read-only; it must be byte-identical after."""
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", str(A / "x.py"), None)])
        before = ev.read_bytes()
        store.backfill_from_events(ev)
        assert ev.read_bytes() == before


class TestMcpReadSurface:
    def test_helpers_return_empty_when_db_absent(self, tmp_path):
        """A fresh install has no workspace.db — the MCP tools must not explode."""
        missing = str(tmp_path / "nope.db")
        assert _get_session_workspaces(missing, "s1") == []
        assert _get_workspace_sessions(missing, "git_remote:h/o/r") == []
        assert _list_workspaces(missing) == []

    def test_helpers_read_backfilled_db(self, tmp_path):
        A = _mkrepo(tmp_path, "A", "git@github.com:acme/A.git")
        ev = _make_events_db(tmp_path / "events.db", [("s1", "claude", str(A / "x.py"), None)])
        db = tmp_path / "workspace.db"
        s = WorkspaceStore(db)
        s.backfill_from_events(ev)
        s.close()

        ws = _get_session_workspaces(str(db), "s1")
        assert ws and ws[0]["workspace_key"] == "git_remote:github.com/acme/a"
        assert _get_workspace_sessions(str(db), "git_remote:github.com/acme/a")
        assert _list_workspaces(str(db))


def _make_github_db(path: Path, repos: list[tuple[int, str]],
                    commits: list[tuple[int, str, str]]) -> Path:
    """repos: (repo_id, path). commits: (repo_id, sha, timestamp)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE repos (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, remote_url TEXT)""")
    c.execute("""CREATE TABLE git_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        sha TEXT NOT NULL, timestamp TEXT NOT NULL)""")
    for rid, rpath in repos:
        c.execute("INSERT INTO repos (id, path) VALUES (?, ?)", (rid, rpath))
    for rid, sha, ts in commits:
        c.execute("INSERT INTO git_commits (repo_id, sha, timestamp) VALUES (?,?,?)",
                  (rid, sha, ts))
    c.commit()
    c.close()
    return path


class TestPortfolioRollup:
    def test_schema_table_created(self, store):
        with store._lock:
            tables = {r[0] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "workspace_rollup" in tables

    def test_signal_agnostic_union(self, tmp_path):
        """One workspace lights up from session + filesystem + git — a single
        node, three signals — because all three resolve on the same ladder key."""
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        key = "git_remote:github.com/acme/r"
        s = WorkspaceStore(tmp_path / "workspace.db")
        # session edge on a deep file inside the repo
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("sess1", "claude", str(repo / "src" / "x.py"),
                             resolve_path(str(repo / "src" / "x.py")))
        # filesystem touch on another deep file
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(repo / "y.py"), 1_700_000_000.0,
                       resolve_dir(str(repo)))
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "abc", "2026-02-01T10:00:00")])
        r = s.build_rollup(gh)
        assert r["multi_source_nodes"] == 1
        assert r["git_repos_matched"] == 1 and r["git_repos_new"] == 0

        pf = s.get_portfolio()
        node = [p for p in pf if p["workspace_key"] == key]
        assert len(node) == 1
        assert set(node[0]["sources"]) == {"session", "filesystem", "git"}
        assert node[0]["session_touches"] == 1
        assert node[0]["fs_touches"] == 1
        assert node[0]["git_touches"] == 1
        s.close()

    def test_iso_epoch_trap_fs_grouped_by_iso_day(self, tmp_path):
        """path_touches.mtime is REAL epoch but last_seen is ISO — the rollup
        must group on the ISO day and never yield an empty date string."""
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        s.build_rollup(tmp_path / "absent-github.db")
        with s._lock:
            days = [r[0] for r in s._conn.execute(
                "SELECT day FROM workspace_rollup").fetchall()]
        assert days and all(len(day) == 10 and day != "" for day in days)
        # ISO date, not an epoch-derived or blank value
        assert all(day[4] == "-" and day[7] == "-" for day in days)
        s.close()

    def test_insert_or_replace_preserves_prior_days(self, tmp_path):
        """A re-touch re-dates path_touches.last_seen to today, but the rollup
        keyed INSERT OR REPLACE must keep the earlier day's row — the rollup is
        the only durable per-day fs record (never wipe-and-rebuild)."""
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        ident = resolve_dir(str(d))
        wid = s.record_touch(str(d / "a.bin"), 1_700_000_000.0, ident)
        # Simulate an earlier rollup day already materialized.
        with s._lock:
            s._conn.execute(
                """INSERT INTO workspace_rollup
                   (workspace_id, day, session_touches, fs_touches, git_touches,
                    last_activity, built_at)
                   VALUES (?, '2020-01-01', 0, 3, 0, '2020-01-01T00:00:00', 'x')""",
                (wid,))
            s._conn.commit()
        s.build_rollup(tmp_path / "absent-github.db")
        with s._lock:
            old = s._conn.execute(
                "SELECT fs_touches FROM workspace_rollup WHERE day='2020-01-01'"
            ).fetchone()
        assert old is not None and old[0] == 3  # earlier day survived
        s.close()

    def test_since_filter_string_compare(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        wid = s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        with s._lock:
            for day in ("2020-01-01", "2030-12-31"):
                s._conn.execute(
                    """INSERT OR REPLACE INTO workspace_rollup
                       (workspace_id, day, session_touches, fs_touches,
                        git_touches, last_activity, built_at)
                       VALUES (?, ?, 0, 1, 0, ?, 'x')""",
                    (wid, day, day + "T00:00:00"))
            s._conn.commit()
        key = s.list_workspaces()[0]["workspace_key"]
        recent = s.get_workspace_activity(key, since="2025-01-01")
        assert [r["day"] for r in recent] == ["2030-12-31"]
        s.close()

    def test_absent_github_db_git_contributes_nothing(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(d / "a.bin"), 1_700_000_000.0, resolve_dir(str(d)))
        r = s.build_rollup(tmp_path / "does-not-exist.db")
        assert r["git_repos_matched"] == 0 and r["git_repos_new"] == 0
        assert all(p["git_touches"] == 0 for p in s.get_portfolio())
        s.close()

    @pytest.mark.skipif(not hasattr(time, "tzset"),
                        reason="tzset (POSIX TZ) unavailable on this platform")
    def test_git_and_fs_same_instant_bucket_same_utc_day(self, tmp_path, monkeypatch):
        """A near-midnight git commit and a filesystem touch at the SAME instant
        must land on ONE (workspace, day) UTC row — not ±1 day apart (#23).

        git_commits.timestamp is naive-LOCAL; the old rollup did
        substr(timestamp,1,10), dating the commit by its local calendar day, so a
        20:00 commit in a UTC-6 zone (02:00 UTC next day) fell on the *previous*
        day from the fs touch's UTC day. We pin a FIXED-offset POSIX TZ ("UTC+06"
        — POSIX sign is inverted, so this is UTC-6, and needs no tzdata) via tzset
        so _parse_ts's astimezone() reads a non-UTC zone — the test is real even
        on a UTC CI runner, and FAILS on the raw-substr code.
        """
        import os as _os
        import time as _time
        from datetime import timezone
        from hub.cache.workspace_store import _parse_ts
        from hub.correlation.workspace_resolver import resolve_dir

        prior_tz = _os.environ.get("TZ")
        monkeypatch.setenv("TZ", "UTC+06")  # POSIX inverted sign → UTC-6
        _time.tzset()
        try:
            git_naive = "2026-01-01T20:00:00"          # naive LOCAL (UTC-6)
            inst = _parse_ts(git_naive)                # → 2026-01-02T02:00:00+00:00
            utc_day = inst.date().isoformat()          # 2026-01-02
            local_day = git_naive[:10]                 # 2026-01-01
            # Precondition: this instant genuinely straddles the UTC midnight —
            # otherwise the test would be tautological (e.g. on a UTC machine).
            assert utc_day != local_day

            repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
            key = "git_remote:github.com/acme/r"
            s = WorkspaceStore(tmp_path / "workspace.db")
            # fs touch inside the repo, at the SAME instant (UTC ISO last_seen).
            wid = s.record_touch(str(repo / "y.py"), 1_700_000_000.0,
                                 resolve_dir(str(repo)))
            with s._lock:
                s._conn.execute(
                    "UPDATE path_touches SET last_seen = ? WHERE workspace_id = ?",
                    (inst.astimezone(timezone.utc).isoformat(), wid))
                s._conn.commit()
            gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                                 [(1, "abc", git_naive)])
            s.build_rollup(gh)

            with s._lock:
                rows = {r[0]: (r[1], r[2]) for r in s._conn.execute(
                    """SELECT day, git_touches, fs_touches FROM workspace_rollup
                       WHERE workspace_id = ?""", (wid,)).fetchall()}
            # Both signals collapse onto the single UTC day...
            assert rows.get(utc_day) == (1, 1)
            # ...and nothing was mis-dated onto the local calendar day.
            assert local_day not in rows
            assert set(s.get_portfolio()[0]["sources"]) >= {"filesystem", "git"}
            s.close()
        finally:
            # Restore the EXACT prior TZ and resync libc's cached zone before
            # monkeypatch's own teardown runs, so no stale zone leaks to siblings.
            if prior_tz is None:
                _os.environ.pop("TZ", None)
            else:
                _os.environ["TZ"] = prior_tz
            _time.tzset()

    def test_mcp_portfolio_masking(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        db = tmp_path / "workspace.db"
        s = WorkspaceStore(db)
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("sess1", "claude", str(repo / "x.py"),
                             resolve_path(str(repo / "x.py")))
        s.build_rollup(tmp_path / "absent.db")
        s.close()

        import hub.mcp_server as mcp
        # unmasked
        rows = _get_portfolio(str(db))
        assert rows and rows[0]["remote_url"] == "github.com/acme/r"
        # masked
        orig = mcp._hide_project_names
        mcp._hide_project_names = lambda: True
        try:
            masked = _get_portfolio(str(db))
        finally:
            mcp._hide_project_names = orig
        assert masked[0]["remote_url"] is None
        assert masked[0]["label"] and masked[0]["workspace_key"]  # key preserved
        # per-day view
        act = _get_workspace_activity(str(db), rows[0]["workspace_key"])
        assert act and act[0]["sources"] == ["session"]

    def test_mcp_portfolio_empty_when_db_absent(self, tmp_path):
        assert _get_portfolio(str(tmp_path / "nope.db")) == []
        assert _get_workspace_activity(str(tmp_path / "nope.db"), "k") == []


def _make_sessions_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (id, provider, last_event_at, ended_at)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE sessions (
        id TEXT, provider TEXT, last_event_at TEXT, ended_at TEXT)""")
    for sid, prov, last_ev, ended in rows:
        c.execute("INSERT INTO sessions (id, provider, last_event_at, ended_at)"
                  " VALUES (?,?,?,?)", (sid, prov, last_ev, ended))
    c.commit()
    c.close()
    return path


def _future(days: int = 30):
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone.utc) + timedelta(days=days)


class TestParseTs:
    def test_three_clock_formats_to_aware_utc(self):
        from datetime import datetime, timezone
        from hub.cache.workspace_store import _parse_ts
        # UTC offset form
        assert _parse_ts("2026-09-17T08:17:16+00:00").utcoffset().total_seconds() == 0
        # Z form is UTC
        assert _parse_ts("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=timezone.utc)
        # falsy / garbage drop out
        assert _parse_ts("") is None and _parse_ts(None) is None
        assert _parse_ts("not-a-date") is None

    def test_naive_is_interpreted_local_not_utc(self):
        """The git_commits case: a naive ts must be read as LOCAL, then shifted
        to UTC. Pin the invariant explicitly (not via astimezone round-trip, and
        not tautologically on a UTC machine) using the system's gmt offset."""
        from datetime import datetime, timezone, timedelta
        import time
        from hub.cache.workspace_store import _parse_ts
        naive = _parse_ts("2026-01-01T00:00:00")
        gmtoff = time.localtime(
            datetime(2026, 1, 1).timestamp()).tm_gmtoff  # seconds east of UTC
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=gmtoff)
        assert naive == expected
        # On a non-UTC machine, naive and the Z form are DIFFERENT instants —
        # the exact bug (treating naive as UTC) this guards against.
        if gmtoff != 0:
            assert _parse_ts("2026-01-01T00:00:00") != _parse_ts("2026-01-01T00:00:00Z")


class TestDeliveryCandidate:
    def test_schema_table_created(self, store):
        with store._lock:
            tables = {r[0] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "delivery_candidates" in tables

    def test_git_commit_signal_fires(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        key = "git_remote:github.com/acme/r"
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("s1", "claude", str(repo / "x.py"),
                             resolve_path(str(repo / "x.py")))
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "deadbeef", "2026-01-01T10:00:00")])
        r = s.detect_delivery_candidates(
            events_db_path=tmp_path / "absent.db", github_db_path=gh,
            now=_future())
        assert r["candidates"] == 1 and r["by_signal"]["git_commit"] == 1
        cands = s.get_delivery_candidates()
        assert cands[0]["workspace_key"] == key
        assert cands[0]["signal"] == "git_commit"
        assert cands[0]["signal_detail"] == "deadbeef"
        assert 0 < cands[0]["confidence"] <= 0.9
        s.close()

    def test_session_close_signal_uses_ended_at(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_path
        s.record_attribution("sess-9", "claude", str(repo / "x.py"),
                             resolve_path(str(repo / "x.py")))
        ev = _make_sessions_db(tmp_path / "events.db",
                               [("sess-9", "claude", "2026-01-01T09:00:00Z",
                                 "2026-01-01T10:00:00Z")])
        r = s.detect_delivery_candidates(
            events_db_path=ev, github_db_path=tmp_path / "absent.db", now=_future())
        assert r["by_signal"]["session_close"] == 1
        c = s.get_delivery_candidates()[0]
        assert c["signal"] == "session_close" and c["signal_detail"] == "sess-9"
        s.close()

    def test_quiescence_alone_never_emits(self, tmp_path):
        """An old fs touch that is NOT a root artifact, no git, no session close
        → quiescent but no admissible second signal → zero candidates."""
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        # deep file (below root) with a normal extension — no artifact, no signal
        s.record_touch(str(repo / "src" / "a.py"), 1_700_000_000.0,
                       resolve_dir(str(repo / "src")))
        r = s.detect_delivery_candidates(
            events_db_path=tmp_path / "absent.db",
            github_db_path=tmp_path / "absent.db", now=_future())
        assert r["quiescent_workspaces"] >= 1  # it IS quiescent
        assert r["candidates"] == 0            # but nothing emitted
        assert s.get_delivery_candidates() == []
        s.close()

    def test_root_artifact_signal(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        # working set: .py below the root
        s.record_touch(str(repo / "src" / "a.py"), 1_700_000_000.0,
                       resolve_dir(str(repo / "src")))
        # a new artifact AT the root, extension outside the working set
        s.record_touch(str(repo / "release.zip"), 1_700_000_100.0,
                       resolve_dir(str(repo)))
        r = s.detect_delivery_candidates(
            events_db_path=tmp_path / "absent.db",
            github_db_path=tmp_path / "absent.db", now=_future())
        assert r["by_signal"]["root_artifact"] == 1
        c = [x for x in s.get_delivery_candidates() if x["signal"] == "root_artifact"][0]
        assert c["signal_detail"].endswith("release.zip")
        s.close()

    def test_path_hash_folder_never_fires_root_artifact(self, tmp_path):
        """A non-git materials folder (path_hash) with two odd singleton
        extensions must NOT fabricate a root_artifact — every dir is its own
        workspace so there is no meaningful 'root' with a tree below it."""
        d = tmp_path / "materials"
        d.mkdir()
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(d / "brief.pdf"), 1_700_000_000.0, resolve_dir(str(d)))
        s.record_touch(str(d / "logo.svg"), 1_700_000_100.0, resolve_dir(str(d)))
        r = s.detect_delivery_candidates(
            events_db_path=tmp_path / "absent.db",
            github_db_path=tmp_path / "absent.db", now=_future())
        assert r["quiescent_workspaces"] >= 1
        assert r["by_signal"]["root_artifact"] == 0
        assert s.get_delivery_candidates() == []
        s.close()

    def test_eviction_when_workspace_active_again(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        from hub.correlation.workspace_resolver import resolve_path, resolve_dir
        s.record_attribution("s1", "claude", str(repo / "x.py"),
                             resolve_path(str(repo / "x.py")))
        gh = _make_github_db(tmp_path / "github.db", [(1, str(repo))],
                             [(1, "abc", "2026-01-01T10:00:00")])
        # far-future now → quiescent → candidate exists
        s.detect_delivery_candidates(events_db_path=tmp_path / "absent.db",
                                     github_db_path=gh, now=_future())
        assert s.get_delivery_candidates()
        # now near the commit → NOT quiescent → candidate evicted
        from hub.cache.workspace_store import _parse_ts
        from datetime import timedelta
        near = _parse_ts("2026-01-01T10:00:00") + timedelta(minutes=1)
        s.detect_delivery_candidates(events_db_path=tmp_path / "absent.db",
                                     github_db_path=gh, now=near)
        assert s.get_delivery_candidates() == []
        s.close()

    def test_mcp_delivery_masking(self, tmp_path):
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        db = tmp_path / "workspace.db"
        s = WorkspaceStore(db)
        from hub.correlation.workspace_resolver import resolve_dir
        s.record_touch(str(repo / "src" / "a.py"), 1_700_000_000.0,
                       resolve_dir(str(repo / "src")))
        s.record_touch(str(repo / "release.zip"), 1_700_000_100.0,
                       resolve_dir(str(repo)))
        s.detect_delivery_candidates(events_db_path=tmp_path / "absent.db",
                                     github_db_path=tmp_path / "absent.db",
                                     now=_future())
        s.close()

        import hub.mcp_server as mcp
        from hub.mcp_server import _get_delivery_candidates
        rows = _get_delivery_candidates(str(db))
        art = [r for r in rows if r["signal"] == "root_artifact"][0]
        assert art["signal_detail"].endswith("release.zip")  # unmasked path
        orig = mcp._hide_project_names
        mcp._hide_project_names = lambda: True
        try:
            masked = _get_delivery_candidates(str(db))
        finally:
            mcp._hide_project_names = orig
        m_art = [r for r in masked if r["signal"] == "root_artifact"][0]
        assert "release.zip" not in str(m_art["signal_detail"])  # path masked
        assert m_art["remote_url"] is None and m_art["workspace_key"]

    def test_mcp_delivery_empty_when_absent(self, tmp_path):
        from hub.mcp_server import _get_delivery_candidates
        assert _get_delivery_candidates(str(tmp_path / "nope.db")) == []
