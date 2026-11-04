"""Versioned snapshot export (#82). Synthetic fixtures, no network.

The payload is validated against ``docs/schema/snapshot.v1.schema.json`` with
a minimal validator written here (zero dependencies: no ``jsonschema``). The
mool-ops design kit's fixtures (``docs/proposals/mool-ops/examples``, read
only) are loaded into a ``github.db`` to check that the totals match its
reference snapshot.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hub.cache.git_store import GitStore
from hub.config import HubConfig, RepoConfig, board_repo_config
from hub.snapshot import SnapshotError, build_snapshot, dumps, write_atomic

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs/schema/snapshot.v1.schema.json").read_text())
KIT = ROOT / "docs/proposals/mool-ops/examples"
TZ = ZoneInfo("America/Costa_Rica")  # UTC-6, no DST


# --- minimal JSON Schema validator (the subset the v1 schema uses) -------------

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(value, schema, path="$"):
    """Errors of ``value`` against ``schema`` (type, required, properties,
    additionalProperties, items, enum, pattern, minimum, format=date)."""
    errors = []
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, n) for n in names):
            return [f"{path}: {value!r} is not {schema['type']}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: {value!r} is not a date")
    if _is_type(value, "number") and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: {value} < {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key!r}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors += validate(item, props[key], f"{path}.{key}")
            elif extra is False:
                errors.append(f"{path}: unexpected {key!r}")
            elif isinstance(extra, dict):
                errors += validate(item, extra, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for n, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{n}]")
    return errors


class TestValidator:
    def test_catches_errors(self):
        assert validate({"a": 1}, {"type": "object", "required": ["b"]})
        assert validate(True, {"type": "integer"})
        assert validate("x", {"enum": ["y"]})
        assert validate("2026-13-01", {"type": "string", "format": "date"})
        assert validate({"n": -1}, {"type": "object",
                                    "additionalProperties": {"type": "integer", "minimum": 0}})
        assert validate(["a#1"], {"type": "array", "items": {"pattern": "^[^/]+/[^#]+#[0-9]+$"}})
        assert not validate(None, {"type": ["integer", "null"]})

    def test_kit_example_is_valid_v1(self):
        # The kit's example only lacks the field MoolMesh adds (`freshness`).
        example = json.loads((KIT / "data/snapshots/1001/2026-11-03.json").read_text())
        assert validate(example, SCHEMA) == ["$: missing 'freshness'"]


# --- fixtures -----------------------------------------------------------------

def _repo(owner, name, path=None):
    return RepoConfig(path=path or f"/Users/u/work/{name}",
                      remote_url=f"github.com/{owner}/{name}", owner=owner, repo=name,
                      added_at="2026-09-01T00:00:00+00:00")


def _set_sync(db, rid, ts, github_id=None):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE repos SET last_github_sync_at = ?, github_id = COALESCE(?, github_id)"
                 " WHERE id = ?", (ts, github_id, rid))
    conn.commit()
    conn.close()


def _load_kit_repo(store, owner, name):
    """A kit fixture (gh CLI shape) → github_issues + PR stats rows."""
    data = json.loads((KIT / "fixtures" / f"{owner}__{name}.json").read_text())
    rid = store.register_repo(_repo(owner, name))
    rows, stats = [], []
    for i in data["issues"]:
        rows.append({
            "number": i["number"], "title": i["title"], "state": i["state"].lower(),
            "author": "alice", "assignees": "[]", "labels": "[]",
            "created_at": i["createdAt"], "updated_at": i.get("closedAt") or i["createdAt"],
            "closed_at": i.get("closedAt"), "is_pull_request": 0})
    for p in data["prs"]:
        rows.append({
            "number": p["number"], "title": p["title"],
            "state": "open" if p["state"] == "OPEN" else "closed",
            "author": p["author"]["login"], "assignees": "[]", "labels": "[]",
            "created_at": p["createdAt"],
            "updated_at": p.get("closedAt") or p["createdAt"],
            "closed_at": p.get("closedAt"), "is_pull_request": 1,
            "pr_state": "merged" if p.get("mergedAt") else "review",
            "pr_merged_at": p.get("mergedAt")})
        refs = [(f"{r['repository']['owner']['login']}/{r['repository']['name']}", r["number"])
                for r in p.get("closingIssuesReferences") or []]
        stats.append({"number": p["number"], "additions": p["additions"],
                      "deletions": p["deletions"], "changed_files": p["changedFiles"],
                      "updated_at": p.get("closedAt"), "closing_refs": refs,
                      "closing_refs_total": len(refs)})
    store.upsert_issues(rid, rows)
    store.upsert_pr_stats(rid, stats)
    return rid, data["id"]


@pytest.fixture
def kit_db(tmp_path):
    db = tmp_path / "github.db"
    store = GitStore(db)
    rid, gid = _load_kit_repo(store, "acme-dev", "portal")
    store.close()
    _set_sync(str(db), rid, "2026-11-04T12:00:00+00:00", github_id=gid)
    return str(db)


CFG = HubConfig(client_owner_aliases={"acme-dev": "acme"})


def _snap(db, since="2026-11-02", until="2026-11-03", repo="acme-dev/portal", **kw):
    kw.setdefault("tz", TZ)
    kw.setdefault("config", CFG)
    return build_snapshot(db, repo, since, until, **kw)


# --- reference totals --------------------------------------------------------------

class TestKitReference:
    def test_matches_kit_reference_snapshot(self, kit_db):
        ref = json.loads((KIT / "data/snapshots/1001/2026-11-03.json").read_text())
        s = _snap(kit_db)
        assert validate(s, SCHEMA) == []
        assert s["schema_version"] == "1.0"
        assert s["org"] == ref["org"] == "acme"
        assert s["repo"] == {"id": 1001, "label": "acme-dev/portal", "kind": "code"}
        assert s["timezone"] == ref["timezone"]
        assert s["period"] == ref["period"] and s["date"] == ref["date"]
        assert s["issues"] == ref["issues"]
        assert s["prs"] == ref["prs"]
        keys = ("number", "title", "author", "additions", "deletions", "files_changed",
                "merged_at", "closes", "bot")
        assert [{k: p[k] for k in keys} for p in s["by_pr"]] == ref["by_pr"]
        assert s["freshness"] == {"complete": True}
        assert s["notes"] == []

    def test_matches_kit_seed_snapshot(self, kit_db):
        # The kit's first cut (2026-11-01) covers the history up to then.
        ref = json.loads((KIT / "data/snapshots/1001/2026-11-01.json").read_text())
        s = _snap(kit_db, since=ref["period"]["start"], until=ref["period"]["end"])
        assert (s["issues"]["total_open"], s["issues"]["total_closed"]) == (
            ref["issues"]["total_open"], ref["issues"]["total_closed"])

    def test_local_day_not_utc_prefix(self, kit_db):
        # PR #11 merged 03:30 UTC Nov 3 = 21:30 Nov 2 in Costa Rica.
        nov2 = _snap(kit_db, since="2026-11-02", until="2026-11-02")
        assert 11 in [p["number"] for p in nov2["by_pr"]]
        utc = _snap(kit_db, since="2026-11-02", until="2026-11-02",
                    tz=ZoneInfo("UTC"))
        assert 11 not in [p["number"] for p in utc["by_pr"]]
        assert utc["timezone"] == "UTC"


# --- determinism -------------------------------------------------------------------

class TestDeterminism:
    def test_two_exports_byte_identical(self, kit_db):
        assert dumps(_snap(kit_db)) == dumps(_snap(kit_db))

    def test_independent_of_insertion_order_and_sync_time(self, tmp_path, kit_db):
        # Same data inserted in reverse order, synced at another time.
        db = tmp_path / "other.db"
        store = GitStore(db)
        rid = store.register_repo(_repo("acme-dev", "portal"))
        conn = sqlite3.connect(kit_db)
        conn.row_factory = sqlite3.Row
        issues = [dict(r) for r in conn.execute(
            "SELECT * FROM github_issues ORDER BY number DESC")]
        stats = conn.execute("SELECT * FROM github_pr_stats ORDER BY pr_number DESC").fetchall()
        refs = conn.execute("SELECT * FROM github_pr_closing_refs").fetchall()
        conn.close()
        for i in issues:  # already local naive; upsert keeps naive values as-is
            i.pop("id")
        store.upsert_issues(rid, issues)
        store.upsert_pr_stats(rid, [
            {"number": s["pr_number"], "additions": s["additions"],
             "deletions": s["deletions"], "changed_files": s["changed_files"],
             "closing_refs": [(r["issue_repo"], r["issue_number"])
                              for r in refs if r["pr_number"] == s["pr_number"]]}
            for s in stats])
        store.close()
        _set_sync(str(db), rid, "2026-11-20T08:00:00+00:00", github_id=1001)
        assert dumps(_snap(str(db))) == dumps(_snap(kit_db))

    def test_no_generation_time_and_sorted(self, kit_db):
        text = dumps(_snap(kit_db))
        assert "generated" not in text and "2026-11-04T12" not in text
        assert text.endswith("}\n")
        assert json.loads(text) == _snap(kit_db)
        assert text == json.dumps(json.loads(text), sort_keys=True, indent=2,
                                  ensure_ascii=False) + "\n"


# --- freshness: range vs last sync -------------------------------------------------

class TestFreshness:
    def test_range_after_last_sync_is_refused(self, kit_db, tmp_path):
        # Last sync 2026-11-04T12:00Z = 06:00 local on Nov 4: Nov 4 is not closed.
        with pytest.raises(SnapshotError) as exc:
            _snap(kit_db, since="2026-11-02", until="2026-11-04")
        assert exc.value.reason == "incomplete" and exc.value.exit_code == 3
        assert exc.value.detail["last_github_sync_at"] == "2026-11-04T12:00:00Z"

    def test_sync_just_after_midnight_covers_the_day(self, kit_db):
        conn = sqlite3.connect(kit_db)
        # 06:00:00Z = local midnight closing Nov 3 in Costa Rica.
        conn.execute("UPDATE repos SET last_github_sync_at = '2026-11-04T06:00:00+00:00'")
        conn.commit()
        conn.close()
        assert _snap(kit_db)["freshness"] == {"complete": True}
        conn = sqlite3.connect(kit_db)
        conn.execute("UPDATE repos SET last_github_sync_at = '2026-11-04T05:59:59+00:00'")
        conn.commit()
        conn.close()
        with pytest.raises(SnapshotError):
            _snap(kit_db)

    def test_allow_incomplete_marks_payload(self, kit_db):
        s = _snap(kit_db, since="2026-11-02", until="2026-11-04", allow_incomplete=True)
        assert validate(s, SCHEMA) == []
        assert s["freshness"] == {"complete": False,
                                  "last_github_sync_at": "2026-11-04T12:00:00Z"}
        assert any("not covered" in n for n in s["notes"])

    def test_never_synced(self, tmp_path):
        db = tmp_path / "github.db"
        store = GitStore(db)
        store.register_repo(_repo("acme", "widget-services"))
        store.close()
        with pytest.raises(SnapshotError) as exc:
            _snap(str(db), repo="acme/widget-services")
        assert exc.value.reason == "incomplete"
        s = _snap(str(db), repo="acme/widget-services", allow_incomplete=True)
        assert s["freshness"] == {"complete": False, "last_github_sync_at": None}
        assert s["repo"]["id"] is None and s["org"] == "acme"
        assert validate(s, SCHEMA) == []


# --- identity, cross-repo, exclusions -------------------------------------------

@pytest.fixture
def cross_db(tmp_path):
    db = tmp_path / "github.db"
    store = GitStore(db)
    rid = store.register_repo(_repo("acme", "widget-services"))
    store.register_repo(board_repo_config("acme/backlog"))
    store.upsert_issues(rid, [
        {"number": 7, "title": "feat: export", "state": "closed", "author": "ownerhandle",
         "created_at": "2026-10-30T15:00:00Z", "updated_at": "2026-11-02T17:00:00Z",
         "closed_at": "2026-11-02T17:00:00Z", "is_pull_request": 1,
         "pr_state": "merged", "pr_merged_at": "2026-11-02T17:00:00Z"},
        {"number": 8, "title": "chore(deps): bump", "state": "closed",
         "author": "dependabot[bot]", "created_at": "2026-11-01T15:00:00Z",
         "updated_at": "2026-11-02T18:00:00Z", "closed_at": "2026-11-02T18:00:00Z",
         "is_pull_request": 1, "pr_state": "merged", "pr_merged_at": "2026-11-02T18:00:00Z"},
        {"number": 9, "title": "wip", "state": "open", "author": "ownerhandle",
         "created_at": "2026-11-01T15:00:00Z", "updated_at": "2026-11-01T15:00:00Z",
         "closed_at": None, "is_pull_request": 1, "pr_state": "review"},
    ])
    store.upsert_pr_stats(rid, [
        {"number": 7, "additions": 40, "deletions": 4, "changed_files": 3,
         "closing_refs": [("acme/widget-services", 2), ("acme/backlog", 5)]},
    ])  # PR #8 has no stats yet
    store.close()
    _set_sync(str(db), rid, "2026-11-10T00:00:00+00:00", github_id=4242)
    return str(db)


class TestCrossRepoAndBots:
    def test_pr_closing_issue_in_other_repo(self, cross_db):
        s = _snap(cross_db, repo="acme/widget-services", config=HubConfig())
        assert validate(s, SCHEMA) == []
        pr7 = s["by_pr"][0]
        assert pr7["number"] == 7
        assert pr7["closes"] == ["acme/backlog#5", "acme/widget-services#2"]
        assert pr7["merged_at"] == "2026-11-02T17:00:00Z"
        assert s["org"] == "acme"

    def test_bot_and_missing_stats(self, cross_db):
        s = _snap(cross_db, repo="acme/widget-services", config=HubConfig())
        pr8 = s["by_pr"][1]
        assert pr8["bot"] is True and "additions" not in pr8 and pr8["closes"] == []
        assert s["prs"] == {"merged_this_period": 2, "open": 1, "open_bots": 0,
                            "total_additions": 40, "total_deletions": 4}
        assert s["notes"] == ["PR stats missing for #8: not in total_additions/total_deletions"]
        assert s["by_developer"] == {}

    def test_open_at_the_cut_not_now(self, cross_db):
        # Before PR #9 existed, nothing was open; before #7/#8 merged, they were.
        s = _snap(cross_db, repo="acme/widget-services", since="2026-10-31",
                  until="2026-10-31", config=HubConfig())
        assert s["prs"]["open"] == 1 and s["prs"]["merged_this_period"] == 0
        s = _snap(cross_db, repo="acme/widget-services", since="2026-11-01",
                  until="2026-11-01", config=HubConfig())
        assert s["prs"]["open"] == 3 and s["prs"]["open_bots"] == 1

    def test_board_kind_and_case_insensitive_repo(self, tmp_path):
        db = tmp_path / "github.db"
        store = GitStore(db)
        rid = store.register_repo(board_repo_config("acme/backlog"))
        store.close()
        _set_sync(str(db), rid, "2026-11-10T00:00:00+00:00")
        s = _snap(str(db), repo="ACME/Backlog", config=HubConfig())
        assert s["repo"]["kind"] == "board" and s["repo"]["label"] == "acme/backlog"
        assert s["issues"] == {"opened_this_period": 0, "closed_this_period": 0,
                               "total_open": 0, "total_closed": 0}

    def test_excluded_repo_answers_as_unregistered(self, cross_db):
        for cfg in (HubConfig(exclude_repos=["acme/widget-services"]),
                    HubConfig(exclude_owners=["ACME"])):
            with pytest.raises(SnapshotError) as exc:
                _snap(cross_db, repo="acme/widget-services", config=cfg)
            assert exc.value.reason == "not_registered"
            assert str(exc.value) == "repo no registrado: acme/widget-services"

    def test_bad_args(self, cross_db, tmp_path):
        for kw in ({"repo": "acme"}, {"since": "2026-11-05", "until": "2026-11-01"},
                   {"since": "nov"}):
            with pytest.raises(SnapshotError) as exc:
                _snap(cross_db, **{"repo": "acme/widget-services", **kw})
            assert exc.value.exit_code == 2
        with pytest.raises(SnapshotError) as exc:
            _snap(str(tmp_path / "none.db"), repo="acme/x")
        assert exc.value.reason == "no_data"
        with pytest.raises(SnapshotError) as exc:
            _snap(cross_db, repo="acme/unknown")
        assert exc.value.reason == "not_registered"

    def test_read_only(self, cross_db):
        before = Path(cross_db).read_bytes()
        _snap(cross_db, repo="acme/widget-services", config=HubConfig())
        assert Path(cross_db).read_bytes() == before


# --- output, CLI and MCP ----------------------------------------------------------

def test_write_atomic(tmp_path):
    out = tmp_path / "sub" / "snap.json"
    write_atomic(out, "a\n")
    write_atomic(out, "b\n")
    assert out.read_text() == "b\n"
    assert [p.name for p in out.parent.iterdir()] == ["snap.json"]


def _cli(home, *args):
    env = {**os.environ, "HOME": str(home)}
    env.pop("MOOLMESH_PRIVACY_DENYLIST", None)
    return subprocess.run([sys.executable, "-m", "hub.cli", "snapshot", "export", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


class TestCli:
    @pytest.fixture
    def home(self, tmp_path, cross_db):
        home = tmp_path / "home"
        (home / ".moolmesh").mkdir(parents=True)
        (home / ".moolmesh" / "github.db").write_bytes(Path(cross_db).read_bytes())
        return home

    def test_stdout_and_output_identical(self, home, tmp_path):
        args = ["--repo", "acme/widget-services", "--since", "2026-11-02",
                "--until", "2026-11-02"]
        r1, r2 = _cli(home, *args), _cli(home, *args)
        assert r1.returncode == 0, r1.stderr
        assert r1.stdout == r2.stdout
        out = tmp_path / "out" / "snap.json"
        r3 = _cli(home, *args, "--format", "json", "--output", str(out))
        assert r3.returncode == 0 and r3.stdout == ""
        assert out.read_text() == r1.stdout
        snap = json.loads(r1.stdout)
        assert validate(snap, SCHEMA) == [] and snap["timezone"] == ""

    def test_incomplete_exit_3_and_allow(self, home):
        args = ["--repo", "acme/widget-services", "--since", "2026-11-02",
                "--until", "2026-11-12"]
        r = _cli(home, *args)
        assert r.returncode == 3 and r.stdout == ""
        assert "--allow-incomplete" in r.stderr
        r = _cli(home, *args, "--allow-incomplete")
        assert r.returncode == 0
        assert json.loads(r.stdout)["freshness"]["complete"] is False

    def test_unregistered_exit_1(self, home):
        r = _cli(home, "--repo", "acme/nope", "--since", "2026-11-02", "--until", "2026-11-02")
        assert r.returncode == 1 and "no registrado" in r.stderr


class TestMcp:
    def test_same_payload_under_snapshot(self, cross_db):
        from hub.mcp_server import _get_snapshot
        res = _get_snapshot(cross_db, "acme/widget-services", "2026-11-02", "2026-11-02",
                            tz=TZ, config=HubConfig())
        assert res == {"snapshot": _snap(cross_db, repo="acme/widget-services",
                                         since="2026-11-02", until="2026-11-02",
                                         config=HubConfig())}

    def test_errors_are_dicts(self, cross_db):
        from hub.mcp_server import _get_snapshot
        res = _get_snapshot(cross_db, "acme/widget-services", "2026-11-02", "2026-11-30",
                            tz=TZ, config=HubConfig())
        assert res["reason"] == "incomplete" and "error" in res
        assert res["last_github_sync_at"] == "2026-11-10T00:00:00Z"
        res = _get_snapshot(cross_db, "acme/widget-services", "2026-11-02", "2026-11-30",
                            tz=TZ, config=HubConfig(), allow_incomplete=True)
        assert res["snapshot"]["freshness"]["complete"] is False
