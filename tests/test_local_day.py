"""One definition of "local day" (issue #81): config, helper, digests, rollup,
portfolio window. Fixtures are synthetic (acme); every GitHub timestamp in a
fixture carries an explicit offset unless the test pins the system zone."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hub import localtime
from hub.localtime import local_day

zoneinfo = pytest.importorskip("zoneinfo")
try:
    CR = zoneinfo.ZoneInfo("America/Costa_Rica")    # UTC-6, no DST
    NY = zoneinfo.ZoneInfo("America/New_York")      # DST: 2nd Sun Mar, 1st Sun Nov
except zoneinfo.ZoneInfoNotFoundError:              # Windows without tzdata
    pytest.skip("no tz database on this machine", allow_module_level=True)


@pytest.fixture
def system_tz(monkeypatch):
    """Pin the SYSTEM zone (POSIX TZ + tzset) and restore it afterwards."""
    prior = os.environ.get("TZ")

    def _set(posix: str) -> None:
        monkeypatch.setenv("TZ", posix)
        time.tzset()

    yield _set
    if prior is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prior
    time.tzset()


@pytest.fixture
def config_tz(tmp_path, monkeypatch):
    """Write ``[user] timezone`` to an isolated config.toml."""
    import hub.config as config_mod
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)

    def _set(name: str | None) -> None:
        if name is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f'[user]\ngithub_handle = ""\ntimezone = "{name}"\n',
                            encoding="utf-8")
    return _set


# ── config ──────────────────────────────────────────────────────────


class TestConfigTimezone:
    def test_parsed_from_user_section(self, config_tz):
        from hub.config import load_config
        config_tz("America/Costa_Rica")
        assert load_config().timezone == "America/Costa_Rica"

    def test_default_is_system_zone(self, config_tz):
        from hub.config import load_config
        config_tz(None)
        assert load_config().timezone == ""
        assert localtime.user_timezone() is None   # None = system zone

    def test_round_trip_and_omitted_when_unset(self, tmp_path, monkeypatch):
        import hub.config as config_mod
        from hub.config import HubConfig, _serialize_toml, load_config, save_config
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
        assert "timezone" not in _serialize_toml(HubConfig())
        save_config(HubConfig(timezone="America/New_York"))
        assert load_config().timezone == "America/New_York"

    def test_non_string_value_falls_back_with_warning(self, tmp_path, monkeypatch, caplog):
        import hub.config as config_mod
        from hub.config import load_config
        path = tmp_path / "config.toml"
        path.write_text("[user]\ntimezone = 5\n", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        with caplog.at_level(logging.WARNING, logger="hub"):
            assert load_config().timezone == ""
        assert any("timezone" in r.getMessage() for r in caplog.records)

    def test_unknown_name_falls_back_to_system_with_warning(self, config_tz, caplog):
        config_tz("Mars/Olympus_Mons")
        localtime.resolve_timezone.cache_clear()
        with caplog.at_level(logging.WARNING, logger="hub"):
            assert localtime.user_timezone() is None
        assert any("Mars/Olympus_Mons" in r.getMessage() for r in caplog.records)

    def test_no_tz_database_falls_back_to_system(self, monkeypatch, caplog):
        """Windows without tzdata: ZoneInfo raises for every key → system zone;
        "UTC" still resolves (it never needs the database)."""
        def _missing(key):
            raise zoneinfo.ZoneInfoNotFoundError(key)
        monkeypatch.setattr(zoneinfo, "ZoneInfo", _missing)
        localtime.resolve_timezone.cache_clear()
        try:
            with caplog.at_level(logging.WARNING, logger="hub"):
                assert localtime.resolve_timezone("America/New_York") is None
            assert caplog.records
            assert localtime.resolve_timezone("UTC") is timezone.utc
            # Bucketing still works on the system zone.
            assert local_day("2026-05-05T12:00:00+00:00", None)
        finally:
            localtime.resolve_timezone.cache_clear()


# ── the helper ──────────────────────────────────────────────────────


class TestLocalDay:
    def test_merged_0100_utc_is_previous_day_at_utc_minus_6(self):
        assert local_day("2026-05-05T01:00:00Z", CR) == "2026-05-04"
        assert local_day("2026-05-05T01:00:00+00:00", CR) == "2026-05-04"
        assert local_day("2026-05-05T01:00:00Z", timezone.utc) == "2026-05-05"

    def test_accepts_every_stored_clock(self, system_tz):
        system_tz("UTC")
        inst = datetime(2026, 5, 5, 1, 0, tzinfo=timezone.utc)
        ms = int(inst.timestamp() * 1000)
        for value in (inst, inst.timestamp(), ms, str(ms),
                      "2026-05-05T01:00:00",            # naive = system (UTC)
                      "2026-05-04T19:00:00-06:00"):
            assert local_day(value, CR) == "2026-05-04", value
        for bad in (None, "", "garbage", True):
            assert local_day(bad, CR) is None

    def test_naive_values_are_system_local(self, system_tz):
        """github.db stores system-local naive: at a UTC-6 system, a naive
        20:00 is 02:00 UTC the next day."""
        system_tz("UTC+06")   # POSIX sign inverted → UTC-6
        assert local_day("2026-01-01T20:00:00", timezone.utc) == "2026-01-02"
        assert local_day("2026-01-01T20:00:00", None) == "2026-01-01"

    def test_dst_spring_forward_new_york(self):
        # 2026-03-08: 02:00 EST → 03:00 EDT (07:00 UTC). A 23-hour day.
        assert local_day("2026-03-08T04:30:00Z", NY) == "2026-03-07"   # 23:30 EST
        assert local_day("2026-03-08T05:30:00Z", NY) == "2026-03-08"   # 00:30 EST
        assert local_day("2026-03-09T03:30:00Z", NY) == "2026-03-08"   # 23:30 EDT
        assert local_day("2026-03-09T04:30:00Z", NY) == "2026-03-09"   # 00:30 EDT
        start, end = localtime.day_bounds("2026-03-08", tz=NY)
        assert end - start == timedelta(hours=23)
        assert start == datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)

    def test_dst_fall_back_new_york(self):
        # 2026-11-01: 02:00 EDT → 01:00 EST (06:00 UTC). A 25-hour day.
        assert local_day("2026-11-01T03:30:00Z", NY) == "2026-10-31"   # 23:30 EDT
        assert local_day("2026-11-01T04:30:00Z", NY) == "2026-11-01"   # 00:30 EDT
        assert local_day("2026-11-02T04:30:00Z", NY) == "2026-11-01"   # 23:30 EST
        assert local_day("2026-11-02T05:30:00Z", NY) == "2026-11-02"   # 00:30 EST
        start, end = localtime.day_bounds("2026-11-01", tz=NY)
        assert end - start == timedelta(hours=25)

    def test_system_naive_range_matches_legacy_strings_without_zone(self, system_tz):
        system_tz("UTC+06")
        assert localtime.system_naive_range("2026-04-16", tz=None) == (
            "2026-04-16T00:00:00", "2026-04-16T23:59:59")

    def test_system_naive_range_converts_configured_day_to_system_clock(self, system_tz):
        system_tz("UTC")
        # CR day 2026-05-04 = 06:00Z on the 4th .. 05:59:59Z on the 5th.
        assert localtime.system_naive_range("2026-05-04", tz=CR) == (
            "2026-05-04T06:00:00", "2026-05-05T05:59:59")

    def test_window_and_today(self):
        assert localtime.window_start("2026-05-07", 7) == "2026-05-01"
        assert localtime.window_start("2026-05-07", 1) == "2026-05-07"
        now = datetime(2026, 5, 5, 1, 0, tzinfo=timezone.utc)
        assert localtime.today(CR, now=now) == "2026-05-04"

    def test_zone_key(self):
        assert localtime.zone_key(None) == ""
        assert localtime.zone_key(timezone.utc) == "UTC"
        assert localtime.zone_key(NY) == "America/New_York"


# ── digests ─────────────────────────────────────────────────────────


def _repo_config():
    from hub.config import RepoConfig
    return RepoConfig(path="/home/u/work/acme-api", remote_url="github.com/acme/api",
                      owner="acme", repo="api", added_at="2026-01-01T00:00:00")


def _issue(number, *, created, pr=False, merged=None, closed=None, state=None):
    return {"number": number, "title": f"t{number}",
            "state": state or ("closed" if (merged or closed) else "open"),
            "author": "ownerhandle", "created_at": created, "updated_at": created,
            "closed_at": closed, "is_pull_request": 1 if pr else 0,
            "pr_merged_at": merged}


class TestDigestLocalDay:
    def _store(self, tmp_path):
        from hub.cache.git_store import GitStore
        store = GitStore(tmp_path / "github.db")
        return store, store.register_repo(_repo_config())

    def test_pr_merged_0100_utc_lands_on_previous_local_day(self, tmp_path, system_tz,
                                                            config_tz):
        """Acceptance (#81): at UTC-6, a PR merged at 01:00 UTC is in the
        previous day's digest — via the configured zone, on a UTC machine."""
        from hub.digests.stats import compute_daily_stats
        system_tz("UTC")
        config_tz("America/Costa_Rica")
        store, rid = self._store(tmp_path)
        store.upsert_issues(rid, [
            _issue(7, created="2026-05-01T15:00:00Z", pr=True,
                   merged="2026-05-05T01:00:00Z"),
        ])
        store.store_commits(rid, [
            {"sha": "c1", "author_name": "A", "author_email": "",
             "timestamp": "2026-05-05T01:00:00+00:00", "message": "late"},
        ])
        prev = compute_daily_stats(store, rid, "2026-05-04")
        same = compute_daily_stats(store, rid, "2026-05-05")
        assert [p["number"] for p in prev["prs_merged"]] == [7]
        assert prev["commits"] == 1
        assert same["prs_merged"] == [] and same["commits"] == 0
        # Without the zone (system = UTC) it is the UTC day, as before.
        config_tz(None)
        assert [p["number"] for p in
                compute_daily_stats(store, rid, "2026-05-05")["prs_merged"]] == [7]
        store.close()

    def test_dst_days_new_york(self, tmp_path, system_tz):
        """Spring-forward and fall-back days hold exactly their own instants."""
        from hub.digests.stats import compute_daily_stats
        system_tz("UTC")
        store, rid = self._store(tmp_path)
        store.upsert_issues(rid, [
            _issue(1, created="2026-03-08T04:30:00Z"),   # Mar 7 23:30 EST
            _issue(2, created="2026-03-09T03:30:00Z"),   # Mar 8 23:30 EDT
            _issue(3, created="2026-11-01T04:30:00Z"),   # Nov 1 00:30 EDT
            _issue(4, created="2026-11-02T04:30:00Z"),   # Nov 1 23:30 EST
            _issue(5, created="2026-11-02T05:30:00Z"),   # Nov 2 00:30 EST
        ])

        def opened(day):
            return sorted(i["number"] for i in
                          compute_daily_stats(store, rid, day, tz=NY)["issues_opened"])

        assert opened("2026-03-07") == [1]
        assert opened("2026-03-08") == [2]
        assert opened("2026-11-01") == [3, 4]
        assert opened("2026-11-02") == [5]
        store.close()

    def test_weekly_uses_local_days(self, tmp_path, system_tz):
        from hub.digests.stats import compute_weekly_stats
        system_tz("UTC")
        store, rid = self._store(tmp_path)
        store.upsert_issues(rid, [
            # Sunday 2026-05-10 22:00 CR = Monday 04:00Z: belongs to the week
            # of Mon 2026-05-04 locally.
            _issue(9, created="2026-05-01T00:00:00Z", pr=True,
                   merged="2026-05-11T04:00:00Z"),
        ])
        wk = compute_weekly_stats(store, rid, "2026-05-04", tz=CR)
        nxt = compute_weekly_stats(store, rid, "2026-05-11", tz=CR)
        assert [p["number"] for p in wk["prs_merged"]] == [9]
        assert "pr_merged_at" not in wk["prs_merged"][0]
        assert nxt["prs_merged"] == []
        store.close()

    def test_continuity_context_uses_previous_local_day(self, tmp_path, system_tz,
                                                        config_tz):
        from hub.digests.engine import DigestEngine
        system_tz("UTC")
        config_tz("America/Costa_Rica")
        store, rid = self._store(tmp_path)
        store.upsert_issues(rid, [
            _issue(11, created="2026-05-01T00:00:00Z", pr=True,
                   merged="2026-05-05T01:00:00Z"),   # CR: 2026-05-04
        ])
        ctx = DigestEngine(store)._build_continuity_context(rid, "2026-05-05", "acme/api")
        assert ctx["yesterday_date"] == "2026-05-04"
        assert "#11" in ctx["yesterday_summary"]
        store.close()


# ── portfolio window ────────────────────────────────────────────────


def _mkrepo(base: Path, name: str, remote: str) -> Path:
    repo = base / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8")
    return repo


def _github_db(path: Path, repos, issues) -> Path:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,"
              " remote_url TEXT)")
    c.execute("""CREATE TABLE github_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        number INTEGER, title TEXT, state TEXT, author TEXT,
        closed_at TEXT, is_pull_request INTEGER DEFAULT 0, pr_merged_at TEXT)""")
    c.execute("""CREATE TABLE git_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INTEGER NOT NULL,
        sha TEXT NOT NULL, timestamp TEXT NOT NULL)""")
    for rid, rpath in repos:
        c.execute("INSERT INTO repos (id, path) VALUES (?, ?)", (rid, rpath))
    for n, i in enumerate(issues):
        c.execute(
            "INSERT INTO github_issues (repo_id, number, title, state, closed_at,"
            " is_pull_request, pr_merged_at) VALUES (?,?,?,?,?,?,?)",
            (i.get("repo_id", 1), n, "t", i["state"], i.get("closed_at"),
             i.get("is_pull_request", 0), i.get("pr_merged_at")))
    c.commit()
    c.close()
    return path


def _classify(store, key: str, project_key: str) -> None:
    with store._lock:
        wid = store._conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key=?", (key,)).fetchone()[0]
        store._conn.execute(
            """INSERT OR REPLACE INTO workspace_classification
               (workspace_id, category, subtype, role, project_key,
                project_label, resolved_via, classified_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wid, "A", "project", "project", project_key, "acme", "self",
             "2026-01-01T00:00:00"))
        store._conn.commit()


# Window 2026-05-01..2026-05-07 in CR (UTC-6) = 05-01T06:00Z .. 05-08T05:59:59Z.
WINDOW_FIXTURE = [
    # merged PRs
    {"state": "closed", "is_pull_request": 1, "pr_merged_at": "2026-05-01T06:00:00+00:00"},  # in (first second)
    {"state": "closed", "is_pull_request": 1, "pr_merged_at": "2026-05-01T05:59:59+00:00"},  # out (Apr 30 CR)
    {"state": "closed", "is_pull_request": 1, "pr_merged_at": "2026-05-08T01:00:00+00:00"},  # in (May 7 19:00 CR)
    {"state": "closed", "is_pull_request": 1, "pr_merged_at": "2026-05-08T06:00:00+00:00"},  # out (May 8 CR)
    {"state": "closed", "is_pull_request": 1, "pr_merged_at": None},                        # unmerged
    # closed issues
    {"state": "closed", "closed_at": "2026-05-03T12:00:00+00:00"},   # in
    {"state": "closed", "closed_at": "2026-04-20T12:00:00+00:00"},   # out
    {"state": "closed", "closed_at": None},                          # no closedAt: total only
    {"state": "open"},
]


class TestPortfolioWindow:
    def _setup(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        from hub.correlation.workspace_resolver import resolve_dir
        repo = _mkrepo(tmp_path, "acme-api", "git@github.com:acme/api.git")
        s = WorkspaceStore(tmp_path / "workspace.db")
        with s._lock:
            s._upsert_workspace_locked(resolve_dir(str(repo)), "2026-01-01T00:00:00")
            s._conn.commit()
        _classify(s, resolve_dir(str(repo)).key, "proj:acme")
        return s, repo

    def test_window_equals_manual_count(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        s, repo = self._setup(tmp_path)
        gh = _github_db(tmp_path / "github.db", [(1, str(repo))], WINDOW_FIXTURE)
        out = WorkspaceStore.read_github_outcome(
            str(s.db_path), str(gh), window=("2026-05-01", "2026-05-07"), tz=CR)
        s.close()
        # Manual count over WINDOW_FIXTURE: 2 merged in, 1 closed in.
        assert out == {"proj:acme": {
            "merged_prs": 4, "closed_issues": 3, "open_issues": 1,
            "merged_prs_window": 2, "closed_issues_window": 1,
        }}

    def test_closed_issue_without_closed_at_never_in_window(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        s, repo = self._setup(tmp_path)
        gh = _github_db(tmp_path / "github.db", [(1, str(repo))],
                        [{"state": "closed", "closed_at": None}])
        out = WorkspaceStore.read_github_outcome(
            str(s.db_path), str(gh), window=("2000-01-01", "2100-12-31"), tz=CR)
        s.close()
        assert out["proj:acme"]["closed_issues"] == 1
        assert out["proj:acme"]["closed_issues_window"] == 0

    def test_no_window_keeps_previous_shape(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        s, repo = self._setup(tmp_path)
        gh = _github_db(tmp_path / "github.db", [(1, str(repo))], WINDOW_FIXTURE)
        out = WorkspaceStore.read_github_outcome(str(s.db_path), str(gh))
        s.close()
        assert set(out["proj:acme"]) == {"merged_prs", "closed_issues", "open_issues"}

    def test_production_view_exposes_window_fields(self, tmp_path):
        """End-to-end through _portfolio_production: the session row is dated
        now, so the window is the last 30 local days ending today."""
        from hub.correlation.workspace_resolver import resolve_path
        from hub.mcp_server import _portfolio_production
        s, repo = self._setup(tmp_path)
        f = str(repo / "src" / "x.py")
        s.record_attribution("sess1", "claude", f, resolve_path(f))
        now = datetime.now(timezone.utc)
        with s._lock:
            s._conn.execute("UPDATE path_attributions SET activity_ts = ?",
                            (now.isoformat(),))
            s._conn.commit()
        s.close()
        gh = _github_db(tmp_path / "github.db", [(1, str(repo))], [
            {"state": "closed", "is_pull_request": 1,
             "pr_merged_at": (now - timedelta(days=2)).isoformat()},
            {"state": "closed", "is_pull_request": 1,
             "pr_merged_at": (now - timedelta(days=100)).isoformat()},
            {"state": "closed", "closed_at": (now - timedelta(days=3)).isoformat()},
            {"state": "closed", "closed_at": None},
        ])
        ev = tmp_path / "events.db"
        sqlite3.connect(ev).close()
        data = _portfolio_production(str(ev), str(tmp_path / "workspace.db"),
                                     days=30, github_db=str(gh), hide=False, tz=CR)
        row = [p for p in data["projects"] if p["project_key"] == "proj:acme"][0]
        assert (row["merged_prs"], row["merged_prs_window"]) == (2, 1)
        assert (row["closed_issues"], row["closed_issues_window"]) == (2, 1)

    def test_production_strip_uses_configured_zone(self, tmp_path):
        """The production strip's day is the same local day as everywhere else."""
        from hub.correlation.workspace_resolver import resolve_path
        from hub.mcp_server import _portfolio_production
        s, repo = self._setup(tmp_path)
        f = str(repo / "src" / "x.py")
        s.record_attribution("sess1", "claude", f, resolve_path(f))
        with s._lock:
            s._conn.execute("UPDATE path_attributions SET activity_ts = ?",
                            ("2026-05-05T01:00:00+00:00",))
            s._conn.commit()
        s.close()
        ev = tmp_path / "events.db"
        sqlite3.connect(ev).close()
        data = _portfolio_production(str(ev), str(tmp_path / "workspace.db"),
                                     days=7, today="2026-05-07", hide=False,
                                     github_db=str(tmp_path / "none.db"), tz=CR)
        row = [p for p in data["projects"] if p["project_key"] == "proj:acme"][0]
        assert list(row["days"]) == ["2026-05-04"]


# ── workspace rollup ────────────────────────────────────────────────


class TestRollupLocalDay:
    def _rollup(self, s):
        with s._lock:
            return {(r[0], r[1]): (r[2], r[3], r[4]) for r in s._conn.execute(
                """SELECT w.workspace_key, r.day, r.session_touches, r.fs_touches,
                          r.git_touches
                   FROM workspace_rollup r JOIN workspaces w ON w.id = r.workspace_id"""
            ).fetchall()}

    def _zone(self, s):
        with s._lock:
            row = s._conn.execute(
                "SELECT value FROM rollup_meta WHERE key = 'day_zone'").fetchone()
        return row[0] if row else None

    def _fixture(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        from hub.correlation.workspace_resolver import resolve_dir, resolve_path
        repo = _mkrepo(tmp_path, "acme-api", "git@github.com:acme/api.git")
        key = resolve_dir(str(repo)).key
        s = WorkspaceStore(tmp_path / "workspace.db")
        f = str(repo / "src" / "x.py")
        s.record_attribution("sess1", "claude", f, resolve_path(f))
        wid = s.record_touch(str(repo / "y.py"), 1_700_000_000.0, resolve_dir(str(repo)))
        inst = "2026-05-05T01:00:00+00:00"          # CR: 2026-05-04 19:00
        with s._lock:
            s._conn.execute("UPDATE path_attributions SET event_ts = ?", (inst,))
            s._conn.execute("UPDATE path_touches SET last_seen = ?", (inst,))
            s._conn.commit()
        gh = tmp_path / "github.db"
        c = sqlite3.connect(gh)
        c.execute("CREATE TABLE repos (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,"
                  " remote_url TEXT)")
        c.execute("CREATE TABLE git_commits (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  " repo_id INTEGER NOT NULL, sha TEXT NOT NULL, timestamp TEXT NOT NULL)")
        c.execute("INSERT INTO repos (id, path) VALUES (1, ?)", (str(repo),))
        # naive = system local; the tests pin the system zone to UTC
        c.execute("INSERT INTO git_commits (repo_id, sha, timestamp)"
                  " VALUES (1, 'c1', '2026-05-05T01:00:00')")
        c.commit()
        c.close()
        return s, key, wid, gh

    def test_all_signals_on_configured_local_day(self, tmp_path, system_tz, config_tz):
        system_tz("UTC")
        config_tz("America/Costa_Rica")
        s, key, _wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)
        assert self._rollup(s) == {(key, "2026-05-04"): (1, 1, 1)}
        assert self._zone(s) == "America/Costa_Rica"
        s.close()

    def test_zone_change_moves_rows_once_without_double_count(self, tmp_path, system_tz,
                                                              config_tz):
        """A rollup built on UTC days (pre-#81) moves to local days on the next
        build: git and current fs rows move, older fs history is kept, nothing
        is counted twice, and a further build is a no-op."""
        system_tz("UTC")
        config_tz("UTC")
        s, key, wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)
        assert self._rollup(s) == {(key, "2026-05-05"): (1, 1, 1)}
        assert self._zone(s) == "UTC"
        # fs history only the rollup holds (a file since re-touched elsewhere).
        with s._lock:
            s._conn.execute(
                """INSERT INTO workspace_rollup (workspace_id, day, fs_touches, built_at)
                   VALUES (?, '2026-04-01', 5, '2026-04-01T00:00:00')""", (wid,))
            s._conn.commit()

        config_tz("America/Costa_Rica")
        s.build_rollup(gh)
        after = self._rollup(s)
        assert after == {(key, "2026-05-04"): (1, 1, 1),
                         (key, "2026-04-01"): (0, 5, 0)}
        assert self._zone(s) == "America/Costa_Rica"
        s.build_rollup(gh)
        assert self._rollup(s) == after
        s.close()

    def test_migration_marks_existing_rollup_as_utc(self, tmp_path, system_tz, config_tz):
        from hub.cache.workspace_store import WorkspaceStore
        system_tz("UTC")
        config_tz("UTC")
        s, key, _wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)
        # Simulate a pre-#81 database: no marker, migration 4 not applied.
        with s._lock:
            s._conn.execute("DROP TABLE rollup_meta")
            s._conn.execute("DELETE FROM schema_migrations WHERE version = 4")
            s._conn.commit()
        s.close()
        s = WorkspaceStore(tmp_path / "workspace.db")
        assert self._zone(s) == "UTC"
        s.close()

    def test_fresh_rollup_gets_no_utc_marker(self, tmp_path):
        from hub.cache.workspace_store import WorkspaceStore
        s = WorkspaceStore(tmp_path / "workspace.db")
        assert self._zone(s) is None
        s.close()

    # ── review fix: never rewrite the rollup without a backup ──

    def _backups(self, tmp_path):
        d = tmp_path / "backups"
        return sorted(d.glob("workspace-*.db")) if d.exists() else []

    def test_transition_takes_backup_with_previous_values(self, tmp_path, system_tz,
                                                          config_tz):
        system_tz("UTC")
        config_tz("UTC")
        s, key, _wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)
        before = self._rollup(s)
        assert self._backups(tmp_path) == []

        config_tz("America/Costa_Rica")
        s.build_rollup(gh)
        backups = self._backups(tmp_path)
        assert len(backups) == 1
        c = sqlite3.connect(backups[0])
        try:
            copied = {(r[0], r[1]): (r[2], r[3], r[4]) for r in c.execute(
                """SELECT w.workspace_key, r.day, r.session_touches, r.fs_touches,
                          r.git_touches
                   FROM workspace_rollup r JOIN workspaces w ON w.id = r.workspace_id""")}
            zone = c.execute(
                "SELECT value FROM rollup_meta WHERE key = 'day_zone'").fetchone()[0]
        finally:
            c.close()
        assert copied == before and zone == "UTC"
        assert self._rollup(s) == {(key, "2026-05-04"): (1, 1, 1)}
        s.build_rollup(gh)                      # moved: no second backup
        assert len(self._backups(tmp_path)) == 1
        s.close()

    def test_backup_failure_leaves_rollup_and_zone_unchanged(self, tmp_path, system_tz,
                                                             config_tz, monkeypatch):
        import hub.cache.workspace_store as ws
        system_tz("UTC")
        config_tz("UTC")
        s, key, _wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)
        before = self._rollup(s)

        real_backup = ws.backup_workspace_db

        def _fail(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(ws, "backup_workspace_db", _fail)
        config_tz("America/Costa_Rica")
        s.build_rollup(gh)
        assert self._rollup(s) == before
        assert self._zone(s) == "UTC"

        monkeypatch.setattr(ws, "backup_workspace_db", real_backup)
        s.build_rollup(gh)                      # retried on the next pass
        assert self._rollup(s) == {(key, "2026-05-04"): (1, 1, 1)}
        assert self._zone(s) == "America/Costa_Rica"
        assert len(self._backups(tmp_path)) == 1
        s.close()

    def test_no_backup_without_transition(self, tmp_path, system_tz, config_tz):
        system_tz("UTC")
        config_tz("America/Costa_Rica")
        s, _key, _wid, gh = self._fixture(tmp_path)
        s.build_rollup(gh)                      # fresh: no previous zone
        s.build_rollup(gh)                      # same zone
        assert self._backups(tmp_path) == []
        s.close()
