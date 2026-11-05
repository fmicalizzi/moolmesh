"""Device identity and per-device effort export (#140). Synthetic fixtures only.

The owner's acceptance table (issue #140) scenarios that apply on one device:
6 (tracked + confidential in one session), 7 (no repo list), 9 (two orgs),
10 (transfer), 11 (day follows --timezone), 12 (outage then catch-up),
13 (same run twice), 14 (no confidential names, paths, host or user) — plus
the device file cases and ``--repos-from``.
"""
import getpass
import json
import os
import socket
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hub import device as devmod
from hub.cache.git_store import GitStore
from hub.cache.workspace_store import WorkspaceStore
from hub.config import HubConfig, RepoConfig
from hub.effort import EffortError, build_effort, dumps, read_repo_list
from tests.test_workspace_store import _mkrepo

UTC = timezone.utc
CR = "America/Costa_Rica"  # UTC-6, no DST
CFG = HubConfig(exclude_repos=["acme-secret/vault"])
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC).timestamp()


def _epoch(y, m, d, h=12, minute=0) -> float:
    return datetime(y, m, d, h, minute, tzinfo=UTC).timestamp()


def _z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture(autouse=True)
def fake_machine(monkeypatch):
    """A fixed synthetic machine id (never the real one)."""
    monkeypatch.setattr(devmod, "_machine_identifier",
                        lambda: ("machine-id", "synthetic-machine-alpha"))


class World:
    """events.db + workspace.db + github.db of one synthetic device."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.base = tmp / "home" / ".moolmesh"
        self.base.mkdir(parents=True)
        self.events = self.base / "events.db"
        self.workspace = self.base / "workspace.db"
        self.github = self.base / "github.db"
        work = tmp / "work"
        self.portal = _mkrepo(work, "portal", "git@github.com:acme-dev/portal.git")
        self.api = _mkrepo(work, "api", "https://github.com/acme-dev/api.git")
        self.vault = _mkrepo(work, "vault", "git@github.com:acme-secret/vault.git")
        self.notes = work / "notes"  # a folder with no git remote
        self.notes.mkdir(parents=True)
        c = sqlite3.connect(self.events)
        c.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
            event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
            tokens_json TEXT, tool_name TEXT, file_path TEXT, model TEXT, cwd TEXT,
            fingerprint TEXT, created_at REAL NOT NULL,
            historical INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE watcher_state (provider TEXT PRIMARY KEY,
            last_cycle_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        c.commit()
        c.close()
        gs = GitStore(self.github)
        self.rids = {}
        for path, owner, name, gid in ((self.portal, "acme-dev", "portal", 1001),
                                       (self.api, "acme-dev", "api", 1002),
                                       (self.vault, "acme-secret", "vault", 2001)):
            rid = gs.register_repo(RepoConfig(
                path=str(path), remote_url=f"github.com/{owner}/{name}",
                owner=owner, repo=name, added_at="2026-10-01T00:00:00"))
            gs.set_github_identity(rid, gid, owner, name)
            self.rids[name] = rid
        self.gs = gs
        self.heartbeat(NOW - 60)

    def event(self, session, path, at, *, provider="claude", tool="Edit",
              created=None, historical=0, cwd=None):
        c = sqlite3.connect(self.events)
        c.execute(
            "INSERT INTO events (provider, project, event_type, timestamp, summary,"
            " session_id, tool_name, file_path, cwd, created_at, historical)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (provider, "p", "tool_use", _z(at), "s", session, tool,
             str(path) if path else None, cwd,
             at if created is None else created, historical))
        c.commit()
        c.close()

    def heartbeat(self, at):
        c = sqlite3.connect(self.events)
        c.execute("INSERT OR REPLACE INTO watcher_state VALUES ('claude', ?, ?)",
                  (at, at))
        c.commit()
        c.close()

    def attribute(self):
        ws = WorkspaceStore(self.workspace)
        ws.backfill_from_events(self.events)
        ws.close()

    def export(self, repos=("acme-dev/portal", "acme-dev/api"), *, org="acme-dev",
               tz=CR, since="2026-11-01", until="2026-11-07", dev=None, now=NOW,
               warn=None):
        return build_effort(
            events_db=self.events, workspace_db=self.workspace,
            github_db=self.github, org=org, timezone_name=tz, since=since,
            until=until, repos=list(repos),
            device=dev or devmod.init_device("laptop", path=self.tmp / "dev.json",
                                             reset=True),
            config=CFG, now=now, threshold_minutes=60, warn=warn)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.gs.close()


@pytest.fixture
def dev(tmp_path):
    return devmod.init_device("laptop", path=tmp_path / "device.json")


# --- device file ---------------------------------------------------------------------

def test_device_init_creates_private_file(tmp_path):
    p = tmp_path / "device.json"
    data = devmod.init_device("laptop", path=p)
    on_disk = json.loads(p.read_text())
    assert on_disk == data
    assert len(bytes.fromhex(data["secret"])) == 32
    assert data["label"] == "laptop"
    assert "synthetic-machine-alpha" not in p.read_text()  # only the hash
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_device_init_refuses_existing_and_reset_replaces(tmp_path):
    p = tmp_path / "device.json"
    first = devmod.init_device(None, path=p)
    with pytest.raises(devmod.DeviceError) as exc:
        devmod.init_device(None, path=p)
    assert exc.value.reason == "exists" and "--reset" in str(exc.value)
    second = devmod.init_device("new", path=p, reset=True)
    assert second["secret"] != first["secret"]
    assert devmod.load_device(p)["label"] == "new"


def test_device_missing_points_to_init(tmp_path):
    with pytest.raises(devmod.DeviceError) as exc:
        devmod.load_device(tmp_path / "nope.json")
    assert exc.value.reason == "missing" and "mool device init" in str(exc.value)


def test_device_copied_from_another_machine_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "device.json"
    devmod.init_device(None, path=p)
    monkeypatch.setattr(devmod, "_machine_identifier",
                        lambda: ("machine-id", "synthetic-machine-beta"))
    with pytest.raises(devmod.DeviceError) as exc:
        devmod.load_device(p)
    assert exc.value.reason == "copied"
    assert "mool device init --reset" in str(exc.value)
    devmod.init_device(None, path=p, reset=True)
    assert devmod.load_device(p)


def test_device_hostname_fallback_is_hashed(monkeypatch):
    monkeypatch.setattr(devmod, "_machine_identifier",
                        lambda: ("hostname", "synthetic-host"))
    source, fp = devmod.machine_fingerprint()
    assert source == "hostname" and len(fp) == 64 and "synthetic" not in fp


def test_scenario_9_ids_differ_per_org_and_hide_the_secret(dev):
    a = devmod.device_id(dev["secret"], "acme-dev")
    b = devmod.device_id(dev["secret"], "globex")
    assert a != b and len(a) == 64
    assert devmod.device_id(dev["secret"], "ACME-DEV") == a
    assert dev["secret"] not in (a, b)


# --- export --------------------------------------------------------------------------

def _seed_basic(w: World):
    """Session s1 (claude) edits portal and the confidential vault on Nov 3;
    s2 (codex) edits api on Nov 3 and Nov 4; s3 works only in vault; s4 only in
    the folder without a remote."""
    t = _epoch(2026, 11, 3, 15)
    w.event("s1", w.portal / "a.py", t)
    w.event("s1", w.portal / "b.py", t + 60)
    w.event("s1", w.vault / "secret.py", t + 120)
    w.event("s2", w.api / "x.py", t, provider="codex")
    w.event("s2", w.api / "x.py", _epoch(2026, 11, 4, 15), provider="codex")
    w.event("s3", w.vault / "only.py", t)
    w.event("s4", w.notes / "plan.md", t)
    w.attribute()


def test_rows_header_and_measures(world, dev):
    _seed_basic(world)
    doc = world.export(dev=dev)
    assert doc["schema_version"] == "1.0" and doc["kind"] == "moolmesh.effort"
    assert doc["device"] == {"id": devmod.device_id(dev["secret"], "acme-dev"),
                             "label": "laptop"}
    assert doc["org"] == "acme-dev" and doc["timezone"] == CR
    assert doc["period"] == {"since": "2026-11-01", "until": "2026-11-07"}
    assert doc["generated_at"] == "2026-11-09T12:00:00Z"
    assert doc["stale"] is False
    assert doc["measures"]["primary"] == "days_worked"
    assert "NOT additive" in doc["measures"]["sessions"]
    assert doc["repos"] == [{"repo_id": 1001, "repo": "acme-dev/portal"},
                            {"repo_id": 1002, "repo": "acme-dev/api"}]
    assert doc["rows"] == [
        {"repo_id": 1001, "repo": "acme-dev/portal", "day": "2026-11-03",
         "provider": "claude", "sessions": 1, "touches": 2},
        {"repo_id": 1002, "repo": "acme-dev/api", "day": "2026-11-03",
         "provider": "codex", "sessions": 1, "touches": 1},
        {"repo_id": 1002, "repo": "acme-dev/api", "day": "2026-11-04",
         "provider": "codex", "sessions": 1, "touches": 1},
    ]


def test_scenario_6_confidential_repo_adds_nothing(world, dev):
    _seed_basic(world)
    doc = world.export(dev=dev)
    text = dumps(doc)
    assert "vault" not in text and "acme-secret" not in text and "2001" not in text
    # s1 touched portal and vault: only its two portal edges count.
    portal = [r for r in doc["rows"] if r["repo_id"] == 1001]
    assert [(r["sessions"], r["touches"]) for r in portal] == [(1, 2)]
    # Listing the confidential repo does not bring it in either.
    warnings = []
    doc2 = world.export(repos=("acme-dev/portal", "acme-secret/vault"), dev=dev,
                        warn=warnings.append)
    assert {r["repo_id"] for r in doc2["rows"]} == {1001}
    assert "vault" not in json.dumps(doc2["repos"])


def test_scenario_7_no_repo_list_refuses(world, dev):
    with pytest.raises(EffortError) as exc:
        world.export(repos=(), dev=dev)
    assert exc.value.reason == "no_repo_list" and exc.value.exit_code != 0
    assert "--repo" in str(exc.value) and "--repos-from" in str(exc.value)


def test_unlisted_repo_never_appears(world, dev):
    _seed_basic(world)
    doc = world.export(repos=("acme-dev/portal",), dev=dev)
    assert {r["repo"] for r in doc["rows"]} == {"acme-dev/portal"}
    text = dumps(doc)
    assert "acme-dev/api" not in text and "1002" not in text


def test_repo_without_github_id_is_omitted_with_warning(world, dev):
    _seed_basic(world)
    c = sqlite3.connect(world.github)
    c.execute("UPDATE repos SET github_id = NULL WHERE repo_name = 'api'")
    c.commit()
    c.close()
    warnings = []
    doc = world.export(dev=dev, warn=warnings.append)
    assert {r["repo_id"] for r in doc["rows"]} == {1001}
    assert len(warnings) == 1 and "acme-dev/api" in warnings[0]
    assert "portal" not in warnings[0] and "vault" not in warnings[0]


def test_scenario_9_two_orgs_two_ids_only_listed_repos(world, dev):
    _seed_basic(world)
    a = world.export(repos=("acme-dev/portal",), org="acme-dev", dev=dev)
    b = world.export(repos=("acme-dev/api",), org="globex", dev=dev)
    assert a["device"]["id"] != b["device"]["id"]
    assert {r["repo"] for r in a["rows"]} == {"acme-dev/portal"}
    assert {r["repo"] for r in b["rows"]} == {"acme-dev/api"}


def test_scenario_10_transfer_keeps_id_and_updates_label(world, dev):
    _seed_basic(world)
    before = world.export(repos=("acme-dev/portal",), dev=dev)
    # GitHub transfer: the harvester re-labels the same row (#94); the local
    # clone's remote still says acme-dev/portal.
    world.gs.set_github_identity(world.rids["portal"], 1001, "acme-labs", "portal")
    after = world.export(repos=("acme-labs/portal",), dev=dev)
    assert [r["repo_id"] for r in after["rows"]] == [1001]
    assert after["rows"][0]["repo"] == "acme-labs/portal"
    strip = lambda rows: [{k: v for k, v in r.items() if k != "repo"} for r in rows]
    assert strip(after["rows"]) == strip(before["rows"])


def test_scenario_11_day_follows_timezone_not_machine(world, dev, monkeypatch):
    # 21:30 in Costa Rica on Nov 3 = 03:30 UTC on Nov 4.
    t = _epoch(2026, 11, 4, 3, 30)
    world.event("s9", world.portal / "late.py", t)
    world.attribute()
    if hasattr(time, "tzset"):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
    try:
        cr = world.export(repos=("acme-dev/portal",), dev=dev)
        utc = world.export(repos=("acme-dev/portal",), tz="UTC", dev=dev)
    finally:
        if hasattr(time, "tzset"):
            monkeypatch.undo()
            time.tzset()
    assert [r["day"] for r in cr["rows"]] == ["2026-11-03"]
    assert [r["day"] for r in utc["rows"]] == ["2026-11-04"]
    assert utc["timezone"] == "UTC"


def test_unknown_timezone_is_an_error(world, dev):
    with pytest.raises(EffortError) as exc:
        world.export(tz="Mars/Olympus", dev=dev)
    assert exc.value.reason == "bad_args"


def test_scenario_12_outage_is_stale_then_catch_up_replaces(world, dev):
    world.event("s1", world.portal / "a.py", _epoch(2026, 11, 1, 15))
    world.attribute()
    world.heartbeat(_epoch(2026, 11, 1, 16))  # daemon down from Nov 1 to Nov 9
    gap = world.export(dev=dev)
    assert gap["stale"] is True
    assert gap["data_through"] == "2026-11-01T16:00:00Z"
    assert [r["day"] for r in gap["rows"]] == ["2026-11-01"]

    # Restart on Nov 9: the catch-up ingests the gap (historical rows, their
    # own clocks), then the heartbeat advances once it is drained.
    for d in (3, 4, 5):
        world.event("s1", world.portal / "a.py", _epoch(2026, 11, d, 15),
                    created=NOW - 300, historical=1)
    world.event("s1", world.portal / "a.py", _epoch(2026, 11, 1, 15),
                created=NOW - 300, historical=1)  # re-read of a known day
    world.attribute()
    world.heartbeat(NOW - 30)
    full = world.export(dev=dev)
    assert full["stale"] is False
    assert [(r["day"], r["sessions"], r["touches"]) for r in full["rows"]] == [
        ("2026-11-01", 1, 1), ("2026-11-03", 1, 1),
        ("2026-11-04", 1, 1), ("2026-11-05", 1, 1)]


def test_stale_when_events_not_yet_attributed(world, dev):
    _seed_basic(world)
    world.event("s1", world.portal / "new.py", NOW - 7200, created=NOW - 7200)
    doc = world.export(dev=dev, until="2026-11-09")
    assert doc["stale"] is True  # the attribution pass is behind


def test_scenario_13_same_run_twice_identical(world, dev):
    _seed_basic(world)
    a = world.export(dev=dev)
    b = world.export(dev=dev)
    assert a["rows"] == b["rows"]
    assert dumps(a) == dumps(b)


def test_scenario_14_no_confidential_names_paths_host_or_user(world, dev):
    _seed_basic(world)
    doc = world.export(dev=dev)
    text = dumps(doc)
    needles = ["acme-secret", "vault", "notes", str(world.tmp),
               world.tmp.name, "synthetic-machine-alpha", dev["secret"]]
    host = socket.gethostname()
    user = getpass.getuser()
    for extra in (host, host.split(".")[0], user):
        if extra and len(extra) >= 4:
            needles.append(extra)
    assert [n for n in needles if n in text] == []


def test_repos_from_file(tmp_path):
    f = tmp_path / "repos.txt"
    f.write_text("# acme repos\nacme-dev/portal\n\nacme-dev/api  # main api\n")
    assert read_repo_list(["acme-dev/web"], str(f)) == [
        "acme-dev/web", "acme-dev/portal", "acme-dev/api"]
    with pytest.raises(EffortError):
        read_repo_list([], str(tmp_path / "missing.txt"))


# --- CLI -----------------------------------------------------------------------------

def _run_cli(monkeypatch, home: Path, *argv):
    from hub import cli
    import hub.config as config
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(config, "load_config", lambda *a, **k: CFG)
    monkeypatch.setattr(sys, "argv", ["mool", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


EXPORT = ("effort", "export", "--org", "acme-dev", "--timezone", CR,
          "--since", "2026-11-01", "--until", "2026-11-07")


def test_cli_no_repo_list_refuses_before_device(world, monkeypatch, capsys):
    code = _run_cli(monkeypatch, world.tmp / "home", *EXPORT)
    assert code == 2
    assert "--repo" in capsys.readouterr().err


def test_cli_without_device_points_to_init(world, monkeypatch, capsys):
    code = _run_cli(monkeypatch, world.tmp / "home", *EXPORT,
                    "--repo", "acme-dev/portal")
    assert code == 3
    assert "mool device init" in capsys.readouterr().err


def test_cli_copied_device_points_to_reset(world, monkeypatch, capsys):
    devmod.init_device(None, path=world.base / "device.json")
    monkeypatch.setattr(devmod, "_machine_identifier",
                        lambda: ("machine-id", "synthetic-machine-beta"))
    code = _run_cli(monkeypatch, world.tmp / "home", *EXPORT,
                    "--repo", "acme-dev/portal")
    assert code == 3
    assert "mool device init --reset" in capsys.readouterr().err


def test_cli_init_show_and_export_to_file(world, monkeypatch, capsys):
    _seed_basic(world)
    home = world.tmp / "home"
    assert _run_cli(monkeypatch, home, "device", "init", "--label", "laptop") == 0
    secret = json.loads((world.base / "device.json").read_text())["secret"]
    capsys.readouterr()
    assert _run_cli(monkeypatch, home, "device", "show", "--org", "acme-dev") == 0
    shown = capsys.readouterr().out
    assert "laptop" in shown and "coincide" in shown and secret not in shown
    assert devmod.device_id(secret, "acme-dev") in shown

    repos = world.tmp / "repos.txt"
    repos.write_text("acme-dev/portal\nacme-dev/api\n")
    out = world.tmp / "out" / "effort.json"
    code = _run_cli(monkeypatch, home, *EXPORT, "--repos-from", str(repos),
                    "--output", str(out))
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["kind"] == "moolmesh.effort"
    assert {r["repo_id"] for r in doc["rows"]} == {1001, 1002}
    assert secret not in out.read_text()
