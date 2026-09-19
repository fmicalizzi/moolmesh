"""Tests for WorkspaceStore + the read-only backfill pass (issue #20)."""

import sqlite3
import time
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
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


def _make_github_db_issues(path: Path, repos: list[tuple[int, str]],
                           issues: list[dict]) -> Path:
    """repos: (repo_id, path). issues: dicts with the outcome-relevant cols."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE repos (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, remote_url TEXT)""")
    c.execute("""CREATE TABLE github_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        number INTEGER, title TEXT, state TEXT, author TEXT,
        closed_at TEXT, is_pull_request INTEGER DEFAULT 0, pr_merged_at TEXT)""")
    for rid, rpath in repos:
        c.execute("INSERT INTO repos (id, path) VALUES (?, ?)", (rid, rpath))
    for n, i in enumerate(issues):
        c.execute(
            "INSERT INTO github_issues (repo_id, number, title, state, author,"
            " closed_at, is_pull_request, pr_merged_at) VALUES (?,?,?,?,?,?,?,?)",
            (i["repo_id"], n, "t", i["state"], i.get("author"),
             i.get("closed_at"), i.get("is_pull_request", 0), i.get("pr_merged_at")),
        )
    c.commit()
    c.close()
    return path


class TestGithubOutcome:
    """read_github_outcome (#27): merged-PR/closed-issue/open-issue per canonical
    project, contributor-agnostic, read-only over github.db."""

    def _classify(self, s, key, project_key, project_label):
        """Insert a minimal classification row mapping a workspace to a project."""
        with s._lock:
            wid = s._conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key=?", (key,)
            ).fetchone()[0]
            s._conn.execute(
                """INSERT OR REPLACE INTO workspace_classification
                   (workspace_id, category, subtype, role, project_key,
                    project_label, resolved_via, classified_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (wid, "A", "project", "project", project_key, project_label,
                 "self", "2026-01-01T00:00:00"),
            )
            s._conn.commit()

    def test_outcome_folds_two_repos_onto_one_canonical_project(self, tmp_path):
        """Two repos collapsing to the same project_key sum their outcome; all
        authors are counted (contributor-agnostic), none surfaced."""
        from hub.correlation.workspace_resolver import resolve_dir
        r1 = _mkrepo(tmp_path, "R1", "git@github.com:acme/R1.git")
        r2 = _mkrepo(tmp_path, "R2", "git@github.com:acme/R2.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        now = "2026-01-01T00:00:00"
        with s._lock:
            for r in (r1, r2):
                s._upsert_workspace_locked(resolve_dir(str(r)), now)
            s._conn.commit()
        # Both repos map to the SAME canonical project group.
        self._classify(s, resolve_dir(str(r1)).key, "proj:acme", "acme")
        self._classify(s, resolve_dir(str(r2)).key, "proj:acme", "acme")
        gh = _make_github_db_issues(
            tmp_path / "github.db", [(1, str(r1)), (2, str(r2))],
            [
                # r1: 2 merged PRs (owner + collaborator), 1 unmerged PR (ignored)
                {"repo_id": 1, "state": "closed", "is_pull_request": 1,
                 "pr_merged_at": now, "author": "owner"},
                {"repo_id": 1, "state": "closed", "is_pull_request": 1,
                 "pr_merged_at": now, "author": "avillegas"},
                {"repo_id": 1, "state": "closed", "is_pull_request": 1,
                 "pr_merged_at": None, "author": "owner"},
                # r1: 1 closed issue, 1 open issue
                {"repo_id": 1, "state": "closed", "closed_at": now},
                {"repo_id": 1, "state": "open"},
                # r2: 1 merged PR, 2 open issues
                {"repo_id": 2, "state": "closed", "is_pull_request": 1,
                 "pr_merged_at": now, "author": "almacreativa"},
                {"repo_id": 2, "state": "open"},
                {"repo_id": 2, "state": "open"},
            ],
        )
        out = WorkspaceStore.read_github_outcome(str(s.db_path), str(gh))
        s.close()
        assert out == {"proj:acme": {
            "merged_prs": 3,       # 2 (r1) + 1 (r2); unmerged PR not counted
            "closed_issues": 1,    # only non-PR closed
            "open_issues": 3,      # 1 (r1) + 2 (r2)
        }}

    def test_outcome_empty_when_github_absent(self, tmp_path):
        s = WorkspaceStore(tmp_path / "workspace.db")
        out = WorkspaceStore.read_github_outcome(
            str(s.db_path), str(tmp_path / "nope.db"))
        s.close()
        assert out == {}

    def test_outcome_skips_unclassified_repo(self, tmp_path):
        """A repo whose workspace has no project_key contributes nothing —
        never guessed onto some other project."""
        from hub.correlation.workspace_resolver import resolve_dir
        r = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        with s._lock:
            s._upsert_workspace_locked(resolve_dir(str(r)), "2026-01-01T00:00:00")
            s._conn.commit()
        # No classification row inserted → no canonical project.
        gh = _make_github_db_issues(
            tmp_path / "github.db", [(1, str(r))],
            [{"repo_id": 1, "state": "closed", "is_pull_request": 1,
              "pr_merged_at": "2026-01-01T00:00:00"}],
        )
        out = WorkspaceStore.read_github_outcome(str(s.db_path), str(gh))
        s.close()
        assert out == {}

    def test_production_view_carries_outcome(self, tmp_path):
        """End-to-end: the augmented production view exposes outcome columns on
        the canonical project row, next to the effort columns."""
        from hub.correlation.workspace_resolver import resolve_dir, resolve_path
        from hub.mcp_server import _portfolio_production
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        f = str(repo / "src" / "x.py")
        s.record_attribution("sess1", "claude", f, resolve_path(f))
        self._classify(s, resolve_dir(str(repo)).key, "proj:acme", "acme")
        s.close()
        ev = _make_events_db(tmp_path / "events.db",
                             [("sess1", "claude", f, str(repo))])
        gh = _make_github_db_issues(
            tmp_path / "github.db", [(1, str(repo))],
            [{"repo_id": 1, "state": "closed", "is_pull_request": 1,
              "pr_merged_at": "2026-01-01T00:00:00"},
             {"repo_id": 1, "state": "open"}],
        )
        # _make_events_db dates rows at time.time() (real now); leave `today`
        # at its default so the session falls inside the 90-day window.
        data = _portfolio_production(str(ev), str(tmp_path / "workspace.db"),
                                     days=90, github_db=str(gh))
        rows = [p for p in data["projects"] if p["project_key"] == "proj:acme"]
        assert len(rows) == 1
        assert rows[0]["merged_prs"] == 1
        assert rows[0]["open_issues"] == 1
        assert rows[0]["closed_issues"] == 0
        assert rows[0]["sessions"] == 1  # effort column still present


def _utc_iso(dt) -> str:
    """Explicit-UTC ISO string — _parse_ts reads naive strings as SYSTEM-LOCAL,
    so every GitHub timestamp in a fixture MUST carry an offset or the test is
    timezone-dependent (passes in UTC-3, fails on a UTC CI runner)."""
    return dt.astimezone(_tz.utc).isoformat()


class TestProjectStates:
    """derive_project_states (#28): one honest state per canonical project,
    fusing local activity (session ingest / fs / git) with GitHub outcome over
    real clocks — activo / enfriandose / entregado / estancado / pausado."""

    def _classify(self, s, key, project_key, project_label):
        with s._lock:
            wid = s._conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key=?", (key,)
            ).fetchone()[0]
            s._conn.execute(
                """INSERT OR REPLACE INTO workspace_classification
                   (workspace_id, category, subtype, role, project_key,
                    project_label, resolved_via, classified_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (wid, "A", "project", "project", project_key, project_label,
                 "self", "2026-01-01T00:00:00"),
            )
            s._conn.commit()
        return wid

    def _git_project(self, tmp_path):
        """A git-backed workspace classified to proj:acme, with one session
        attributed to it (ingest ≈ real now)."""
        from hub.correlation.workspace_resolver import resolve_dir, resolve_path
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        f = str(repo / "src" / "x.py")
        s.record_attribution("sess1", "claude", f, resolve_path(f))
        self._classify(s, resolve_dir(str(repo)).key, "proj:acme", "acme")
        ev = _make_events_db(tmp_path / "events.db",
                             [("sess1", "claude", f, str(repo))])
        return s, repo, str(ev)

    def test_activo_recent_activity(self, tmp_path):
        """A session ingested ~now → activo, regardless of outcome."""
        s, repo, ev = self._git_project(tmp_path)
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        st = s.derive_project_states(ev, str(gh))
        s.close()
        assert st["proj:acme"]["state"] == "activo"
        assert st["proj:acme"]["basis"] == "recent_activity"
        assert st["proj:acme"]["outcome_measurable"] is True

    def test_enfriandose_between_windows(self, tmp_path):
        """Age past ACTIVE but under COOLING → enfriándose (activity-only read)."""
        s, repo, ev = self._git_project(tmp_path)
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        now = _dt.now(_tz.utc) + _td(days=7)
        st = s.derive_project_states(ev, str(gh), now=now)
        s.close()
        assert st["proj:acme"]["state"] == "enfriandose"

    def test_entregado_merged_pr_closes_burst(self, tmp_path):
        """Quiet + a merged PR within slack of the last activity → entregado
        (a git FACT)."""
        s, repo, ev = self._git_project(tmp_path)
        merge_ts = _utc_iso(_dt.now(_tz.utc))  # ≈ the session ingest clock
        gh = _make_github_db_issues(
            tmp_path / "github.db", [(1, str(repo))],
            [{"repo_id": 1, "state": "closed", "is_pull_request": 1,
              "pr_merged_at": merge_ts}],
        )
        now = _dt.now(_tz.utc) + _td(days=30)  # quiet
        st = s.derive_project_states(ev, str(gh), now=now)
        s.close()
        assert st["proj:acme"]["state"] == "entregado"
        assert st["proj:acme"]["basis"] == "merged_pr"
        assert st["proj:acme"]["merged_prs"] == 1

    def test_estancado_repo_with_open_issues(self, tmp_path):
        """Quiet + a repo with open issues and NO recent merge → estancado."""
        s, repo, ev = self._git_project(tmp_path)
        gh = _make_github_db_issues(
            tmp_path / "github.db", [(1, str(repo))],
            [{"repo_id": 1, "state": "open"},
             {"repo_id": 1, "state": "open"}],
        )
        now = _dt.now(_tz.utc) + _td(days=30)
        st = s.derive_project_states(ev, str(gh), now=now)
        s.close()
        assert st["proj:acme"]["state"] == "estancado"
        assert st["proj:acme"]["basis"] == "open_issues"
        assert st["proj:acme"]["open_issues"] == 2

    def test_repo_zero_prs_is_measurable_not_gitless(self, tmp_path):
        """outcome_measurable comes from the repos side: a repo with 0 PRs/issues
        is still measurable — quiet with nothing open → pausado, never mistaken
        for a gitless project (the #28 comment)."""
        s, repo, ev = self._git_project(tmp_path)
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        now = _dt.now(_tz.utc) + _td(days=30)
        st = s.derive_project_states(ev, str(gh), now=now)
        s.close()
        assert st["proj:acme"]["outcome_measurable"] is True
        assert st["proj:acme"]["state"] == "pausado"

    def test_gitless_delivery_candidate_is_entregado(self, tmp_path):
        """A project with no repo (outcome not measurable) but a delivery_candidate
        row → entregado via the local heuristic; never estancado for missing PRs."""
        from hub.correlation.workspace_resolver import resolve_path
        s = WorkspaceStore(tmp_path / "workspace.db")
        f = str(tmp_path / "materials" / "brief.pdf")
        wid = s.record_attribution("sess1", "claude", f, resolve_path(f))
        key = s._conn.execute(
            "SELECT workspace_key FROM workspaces WHERE id=?", (wid,)
        ).fetchone()[0]
        self._classify(s, key, "proj:gitless", "materials")
        with s._lock:
            s._conn.execute(
                """INSERT INTO delivery_candidates
                   (workspace_id, signal, signal_detail, quiescent_since,
                    confidence, detected_at)
                   VALUES (?,?,?,?,?,?)""",
                (wid, "root_artifact", f, "2026-01-01T00:00:00", 0.5,
                 "2026-01-01T00:00:00"),
            )
            s._conn.commit()
        ev = _make_events_db(tmp_path / "events.db",
                             [("sess1", "claude", f, str(tmp_path / "materials"))])
        now = _dt.now(_tz.utc) + _td(days=30)
        st = s.derive_project_states(ev, str(tmp_path / "nope.db"), now=now)
        s.close()
        assert st["proj:gitless"]["state"] == "entregado"
        assert st["proj:gitless"]["basis"] == "delivery_candidate"
        assert st["proj:gitless"]["outcome_measurable"] is False

    def test_gitless_no_candidate_is_pausado(self, tmp_path):
        """Gitless + quiet + no candidate → pausado (outcome not measurable);
        never estancado (it has no PRs it could ever have)."""
        from hub.correlation.workspace_resolver import resolve_path
        s = WorkspaceStore(tmp_path / "workspace.db")
        f = str(tmp_path / "notes" / "todo.md")
        wid = s.record_attribution("sess1", "claude", f, resolve_path(f))
        key = s._conn.execute(
            "SELECT workspace_key FROM workspaces WHERE id=?", (wid,)
        ).fetchone()[0]
        self._classify(s, key, "proj:notes", "notes")
        ev = _make_events_db(tmp_path / "events.db",
                             [("sess1", "claude", f, str(tmp_path / "notes"))])
        now = _dt.now(_tz.utc) + _td(days=30)
        st = s.derive_project_states(ev, str(tmp_path / "nope.db"), now=now)
        s.close()
        assert st["proj:notes"]["state"] == "pausado"
        assert st["proj:notes"]["outcome_measurable"] is False

    def test_no_activity_no_state(self, tmp_path):
        """A classified project with zero real activity yields NO state —
        absence is never fabricated into pausado."""
        from hub.correlation.workspace_resolver import resolve_dir
        repo = _mkrepo(tmp_path, "R", "git@github.com:acme/R.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        with s._lock:
            s._upsert_workspace_locked(resolve_dir(str(repo)), "2026-01-01T00:00:00")
            s._conn.commit()
        self._classify(s, resolve_dir(str(repo)).key, "proj:acme", "acme")
        st = s.derive_project_states(
            str(tmp_path / "no_ev.db"), str(tmp_path / "no_gh.db"))
        s.close()
        assert st == {}

    def test_last_activity_age_surfaced(self, tmp_path):
        """The chip's basis carries its ground: last_activity + age_days for the
        cold sub-cases (#25 absorbed)."""
        s, repo, ev = self._git_project(tmp_path)
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        now = _dt.now(_tz.utc) + _td(days=20)
        st = s.derive_project_states(ev, str(gh), now=now)
        s.close()
        cell = st["proj:acme"]
        assert cell["last_activity"] is not None
        assert 19 <= cell["age_days"] <= 21

    def test_grouped_view_carries_state(self, tmp_path, monkeypatch):
        """End-to-end: the grouped portfolio attaches a masked-safe state chip
        (no labels) to each project row, read-on-load."""
        import hub.config as cfgmod
        from hub.config import HubConfig
        # Neutralize the dev machine's real ~/.moolmesh/config.toml: with no
        # owner identity the #29 client tier is a flat no-op, so the project
        # stays in `projects` deterministically (not machine-dependent).
        monkeypatch.setattr(cfgmod, "load_config", lambda: HubConfig())
        from hub.mcp_server import _get_portfolio_grouped
        from hub.correlation.workspace_resolver import resolve_dir
        s, repo, ev = self._git_project(tmp_path)
        # A rollup row so the grouped view surfaces the project.
        with s._lock:
            wid = s._conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key=?",
                (resolve_dir(str(repo)).key,),
            ).fetchone()[0]
            s._conn.execute(
                """INSERT INTO workspace_rollup
                   (workspace_id, day, session_touches, fs_touches, git_touches,
                    last_activity, built_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (wid, "2026-01-01", 1, 0, 0, "2026-01-01T00:00:00",
                 "2026-01-01T00:00:00"),
            )
            s._conn.commit()
        s.close()
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        data = _get_portfolio_grouped(
            str(tmp_path / "workspace.db"), events_db=ev, github_db=str(gh))
        rows = [p for p in data["projects"] if p["project_key"] == "proj:acme"]
        assert len(rows) == 1
        assert rows[0]["state"]["state"] == "activo"
        # Flat no-op: no client tier without an owner identity.
        assert data["clients"] == []
        # Internal join-only fields never ship.
        assert "_remote_url" not in rows[0]

    def test_grouped_view_client_hierarchy_active(self, tmp_path, monkeypatch):
        """With an owner identity + a client org configured, the git project
        (remote github.com/acme/r) groups under the acme client node instead of
        staying loose — the #29 hierarchy end-to-end, effort rolled up."""
        import hub.config as cfgmod
        from hub.config import HubConfig
        monkeypatch.setattr(
            cfgmod, "load_config",
            lambda: HubConfig(personal_orgs=["me"], client_orgs=["acme"]))
        from hub.mcp_server import _get_portfolio_grouped
        from hub.correlation.workspace_resolver import resolve_dir
        s, repo, ev = self._git_project(tmp_path)
        with s._lock:
            wid = s._conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key=?",
                (resolve_dir(str(repo)).key,),
            ).fetchone()[0]
            s._conn.execute(
                """INSERT INTO workspace_rollup
                   (workspace_id, day, session_touches, fs_touches, git_touches,
                    last_activity, built_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (wid, "2026-01-01", 2, 0, 0, "2026-01-01T00:00:00",
                 "2026-01-01T00:00:00"),
            )
            s._conn.commit()
        s.close()
        gh = _make_github_db_issues(tmp_path / "github.db", [(1, str(repo))], [])
        data = _get_portfolio_grouped(
            str(tmp_path / "workspace.db"), events_db=ev, github_db=str(gh))
        # The project is NOT loose — it lives under the acme client node.
        assert all(p["project_key"] != "proj:acme" for p in data["projects"])
        clients = {c["client_key"]: c for c in data["clients"]}
        assert "client:acme" in clients
        acme = clients["client:acme"]
        assert [p["project_key"] for p in acme["projects"]] == ["proj:acme"]
        assert acme["session_touches"] == 2          # effort rolled up
        assert acme["state"]["state"] == "activo"    # hottest inherited
        assert data["summary"]["clients"] == 1
