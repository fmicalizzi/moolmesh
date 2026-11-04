#!/usr/bin/env python3
"""
ops_snapshot.py — reference implementation of the incremental, verified
snapshot method described in docs/05-methodology.md.

Stdlib only (Python 3.11+, uses tomllib). Data comes from the `gh` CLI or, for
tests and examples, from fixture files.

What it does, per repo of kind "code" or "board" in the registry:
  1. Reads the last snapshot of the repo and starts at period.end + 1.
  2. Fetches PRs and issues (read-only).
  3. Groups by LOCAL day (registry timezone), not by UTC date prefix.
  4. Chains totals (previous + period) and VERIFIES them against the live
     count at the cut. On mismatch it keeps the live value and records the
     difference in `notes` (never silently).
  5. Writes a new snapshot; never overwrites an existing one. No activity in
     the period → no snapshot.

Identity rules (multi-owner safe):
  - A repo is keyed by its numeric GitHub id; "owner/name" is only a label.
  - Issue references are always "owner/name#n", never bare numbers, because a
    PR can close issues in other repos.
  - Bots count in totals but are never credited to a person.

Usage:
  python3 ops_snapshot.py --registry registry.toml --data DIR --cut 2026-11-03
  python3 ops_snapshot.py --registry registry.toml --data DIR --cut 2026-11-03 \
          --fixtures ../examples/fixtures          # offline, used by examples
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1.0"
PR_FIELDS = ("number,title,author,state,createdAt,mergedAt,closedAt,"
             "additions,deletions,changedFiles,closingIssuesReferences")
ISSUE_FIELDS = "number,title,state,createdAt,closedAt,assignees,labels"


# ---------------------------------------------------------------- sources
class GhSource:
    """Reads from the gh CLI. One token per owner can be supplied through the
    env var GH_TOKEN_<OWNER> (upper-case, '-' → '_'); falls back to gh auth."""

    def _env(self, owner: str) -> dict:
        env = dict(os.environ)
        tok = os.environ.get("GH_TOKEN_" + owner.upper().replace("-", "_"))
        if tok:
            env["GH_TOKEN"] = tok
        return env

    def _json(self, owner: str, args: list[str]):
        r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           timeout=300, env=self._env(owner))
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return json.loads(r.stdout or "null")

    def repo_id(self, owner, name):
        return self._json(owner, ["api", f"repos/{owner}/{name}", "-q", ".id"])

    def prs(self, owner, name):
        return self._json(owner, ["pr", "list", "--repo", f"{owner}/{name}", "--state", "all",
                                  "--limit", "5000", "--json", PR_FIELDS])

    def issues(self, owner, name):
        return self._json(owner, ["issue", "list", "--repo", f"{owner}/{name}", "--state", "all",
                                  "--limit", "5000", "--json", ISSUE_FIELDS])


class FixtureSource:
    """Reads <dir>/<owner>__<name>.json = {"id": int, "prs": [...], "issues": [...]}."""

    def __init__(self, root: str):
        self.root = root

    def _load(self, owner, name):
        with open(os.path.join(self.root, f"{owner}__{name}.json")) as f:
            return json.load(f)

    def repo_id(self, owner, name):
        return self._load(owner, name)["id"]

    def prs(self, owner, name):
        return self._load(owner, name)["prs"]

    def issues(self, owner, name):
        return self._load(owner, name)["issues"]


# ---------------------------------------------------------------- helpers
def local_day(ts: str | None, tz: ZoneInfo) -> date | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()


def is_bot(login: str | None, extra: set[str]) -> bool:
    if not login:
        return True
    return (login in extra or login.endswith("[bot]") or login.startswith("app/")
            or "dependabot" in login)


def ref_key(ref: dict, default_owner: str, default_name: str) -> str:
    repo = ref.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login") or default_owner
    name = repo.get("name") or default_name
    return f"{owner}/{name}#{ref['number']}"


def last_snapshot(folder: str):
    files = sorted(glob.glob(os.path.join(folder, "????-??-??.json")))
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


# ---------------------------------------------------------------- core
def build(repo_cfg: dict, org_cfg: dict, people: dict, src, data_dir: str,
          cut: date, start_override: date | None, dry: bool) -> dict | None:
    owner, name = repo_cfg["repo"].split("/", 1)
    tz = ZoneInfo(org_cfg.get("timezone", "UTC"))
    bots = set(org_cfg.get("bots", []))
    rid = src.repo_id(owner, name)
    folder = os.path.join(data_dir, "snapshots", str(rid))
    os.makedirs(folder, exist_ok=True)

    last = last_snapshot(folder)
    if last:
        start = date.fromisoformat(last["period"]["end"]) + timedelta(days=1)
    elif start_override:
        start = start_override
    else:
        print(f"[{owner}/{name}] no previous snapshot: pass --start to seed it")
        return None
    if start > cut:
        print(f"[{owner}/{name}] up to date ({last['period']['end']})")
        return None

    prs, issues = src.prs(owner, name), src.issues(owner, name)
    in_win = lambda ts: (d := local_day(ts, tz)) is not None and start <= d <= cut  # noqa: E731

    merged = sorted([p for p in prs if p.get("mergedAt") and in_win(p["mergedAt"])],
                    key=lambda p: p["mergedAt"])
    closed = [i for i in issues if i["state"] == "CLOSED" and in_win(i.get("closedAt"))]
    opened = [i for i in issues if in_win(i["createdAt"])]

    live_closed = sum(1 for i in issues if i["state"] == "CLOSED"
                      and local_day(i.get("closedAt"), tz) <= cut)
    live_total = sum(1 for i in issues if local_day(i["createdAt"], tz) <= cut)
    live_open = live_total - live_closed
    notes = []
    if last:
        chain = (last["issues"]["total_closed"] + len(closed),
                 last["issues"]["total_open"] - len(closed) + len(opened))
        if chain != (live_closed, live_open):
            notes.append(f"chain {chain[0]}/{chain[1]} adjusted to live {live_closed}/{live_open}")

    if not (merged or closed or opened):
        print(f"[{owner}/{name}] no activity {start}..{cut}: no snapshot")
        return None

    alias = people.get("aliases", {})
    assignee = {f"{owner}/{name}#{i['number']}": (i["assignees"][0]["login"] if i["assignees"] else None)
                for i in issues}
    by_dev, by_pr, closer = {}, [], {}
    for p in merged:
        login = p["author"]["login"]
        refs = [ref_key(r, owner, name) for r in (p.get("closingIssuesReferences") or [])]
        for r in refs:
            closer[r] = login
        by_pr.append({"number": p["number"], "title": p["title"], "author": login,
                      "additions": p["additions"], "deletions": p["deletions"],
                      "files_changed": p["changedFiles"], "merged_at": p["mergedAt"],
                      "closes": refs, "bot": is_bot(login, bots)})
        if is_bot(login, bots):
            continue
        d = by_dev.setdefault(login, {"alias": alias.get(login, login), "issues_closed": 0,
                                      "prs_merged": 0, "additions": 0, "deletions": 0, "prs": []})
        d["prs_merged"] += 1
        d["additions"] += p["additions"]
        d["deletions"] += p["deletions"]
        d["prs"].append(p["number"])
    for i in closed:
        k = f"{owner}/{name}#{i['number']}"
        who = assignee.get(k) or closer.get(k)
        if who and not is_bot(who, bots):
            by_dev.setdefault(who, {"alias": alias.get(who, who), "issues_closed": 0,
                                    "prs_merged": 0, "additions": 0, "deletions": 0, "prs": []})
            by_dev[who]["issues_closed"] += 1

    open_prs = [p for p in prs if p["state"] == "OPEN"]
    team = repo_cfg.get("team", [])
    snap = {
        "schema_version": SCHEMA_VERSION,
        "org": org_cfg["id"],
        "repo": {"id": rid, "label": f"{owner}/{name}", "kind": repo_cfg.get("kind", "code"),
                 "membership": repo_cfg.get("membership", "org")},
        "date": cut.isoformat(),
        "timezone": str(tz),
        "period": {"start": start.isoformat(), "end": cut.isoformat(),
                   "days": (cut - start).days + 1},
        "issues": {"closed_this_period": len(closed), "opened_this_period": len(opened),
                   "total_open": live_open, "total_closed": live_closed},
        "prs": {"merged_this_period": len(merged), "open": len(open_prs),
                "open_bots": sum(1 for p in open_prs if is_bot(p["author"]["login"], bots)),
                "total_additions": sum(p["additions"] for p in merged),
                "total_deletions": sum(p["deletions"] for p in merged)},
        "by_developer": by_dev,
        "by_pr": by_pr,
        "devs_active": len(by_dev),
        "devs_available": [t for t in team if t not in by_dev],
        "blockers": [],
        "notes": notes,
        "extensions": repo_cfg.get("extensions", {}),
    }
    out = os.path.join(folder, f"{cut.isoformat()}.json")
    if os.path.exists(out):
        print(f"[{owner}/{name}] {out} exists: not overwritten")
        return None
    print(f"[{owner}/{name}] {start}..{cut}: {len(merged)} PRs, {len(closed)} closed, "
          f"{len(opened)} opened -> {live_closed}/{live_open}" + (f"  ({notes[0]})" if notes else ""))
    if not dry:
        with open(out, "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--data", required=True, help="private data repo (output root)")
    ap.add_argument("--cut", required=True, help="cut date YYYY-MM-DD (local)")
    ap.add_argument("--start", help="first day when a repo has no snapshot yet")
    ap.add_argument("--fixtures", help="offline fixture folder instead of gh")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    with open(a.registry, "rb") as f:
        reg = tomllib.load(f)
    src = FixtureSource(a.fixtures) if a.fixtures else GhSource()
    cut = date.fromisoformat(a.cut)
    start = date.fromisoformat(a.start) if a.start else None
    org = reg["organization"]
    confidential = {o["login"] for o in reg.get("owners", []) if o.get("relation") == "confidential"}
    bad = [r["repo"] for r in reg.get("repos", [])
           if (r["repo"].split("/")[0] in confidential or r.get("membership") == "confidential")
           and r.get("track", True) is not False]
    if bad:
        print("registry error: confidential repos must have track = false: " + ", ".join(bad))
        return 2
    for repo in reg.get("repos", []):
        if repo.get("kind") == "docs" or repo.get("track", True) is False:
            print(f"[{repo['repo']}] not tracked (kind={repo.get('kind')}, track={repo.get('track', True)})")
            continue
        try:
            build(repo, org, reg.get("people", {}), src, a.data, cut, start, a.dry_run)
        except Exception as e:  # report and continue with the next repo
            print(f"[{repo['repo']}] ERROR: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
