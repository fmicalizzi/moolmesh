"""Versioned snapshot export — a stable contract over ``github.db`` (#82).

Tools that build on MoolMesh (reporting, coordination) read this payload
instead of the internal SQLite schema, which evolves through migrations. The
contract is ``docs/schema/snapshot.v1.schema.json`` (explained in
``docs/schema/snapshot.v1.md``); ``schema_version`` follows it: a new optional
field bumps the minor, a removal or rename the major.

One repo, one inclusive range of LOCAL days (``[user] timezone``, the same
definition of "local day" as digests and the portfolio, ``hub.localtime``):

- ``issues``: opened / closed in the period, and the totals at the cut (the
  end of ``until``), computed from the dates — so a past range keeps its
  numbers when the repo moves on.
- ``prs``: merged in the period with their stats (#80) and the PRs open at
  the cut; ``by_pr`` lists each merged PR with ``closes`` qualified as
  ``owner/repo#n`` (a PR can close an issue in another repo).
- ``freshness``: whether the repo's last GitHub sync covers the whole period.

Determinism: the body has no generation time; keys and lists are sorted, so
two exports of the same range over the same data are byte-identical
(:func:`dumps`). A period the last sync does not cover (``until`` is today,
or the daemon was down) is refused with :class:`SnapshotError` reason
``incomplete``; with ``allow_incomplete`` it is exported with
``freshness.complete = false`` and the sync time, and is then — by nature —
not reproducible.

Read-only: ``github.db`` is opened ``mode=ro`` and never migrated from here.
Excluded owners/repos (#95) answer exactly like an unregistered repo.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import date, timezone
from pathlib import Path
from typing import Any

from hub import localtime

SCHEMA_VERSION = "1.0"

# Exit codes of ``mool snapshot export`` per SnapshotError reason.
EXIT_CODES = {"bad_args": 2, "not_registered": 1, "no_data": 1, "incomplete": 3}


class SnapshotError(Exception):
    """A snapshot that cannot be built; ``reason`` is a stable token."""

    def __init__(self, reason: str, message: str, **detail: Any):
        super().__init__(message)
        self.reason = reason
        self.detail = detail

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.reason, 1)

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "reason": self.reason, **self.detail}


def is_bot(login: str | None) -> bool:
    """GitHub Apps and bot accounts; they count in totals, never as a person."""
    if not login:
        return True
    low = login.lower()
    return low.endswith("[bot]") or low.startswith("app/") or "dependabot" in low


def _parse_day(value: str, name: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise SnapshotError("bad_args",
                            f"--{name} must be a date YYYY-MM-DD (got {value!r})"
                            ) from None


def _utc_z(value: Any) -> str | None:
    """Stored timestamp (system-local naive or aware) → ``YYYY-MM-DDTHH:MM:SSZ``."""
    dt = localtime.to_aware(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _org_of(owner: str, config: Any) -> str:
    """The client the owner maps to (#95 aliases), else the owner itself."""
    try:
        aliases = config.effective_owner_aliases() if config is not None else {}
    except Exception:  # noqa: BLE001 — a bad registry must not break the export
        aliases = {}
    norm = {str(k).strip().lower(): v for k, v in (aliases or {}).items()}
    return norm.get(owner.lower(), owner)


def _exclusions(config: Any):
    from hub.config import Exclusions, exclusions_of
    if config is None:
        return Exclusions()
    return exclusions_of(config)


def _load(github_db: str | Path, owner: str, name: str):
    """Repo row, its issues/PR rows and PR stats, read-only."""
    if not os.path.exists(github_db):
        raise SnapshotError("no_data",
                            "github.db does not exist: register a repo with mool repo add")
    from hub.sqlite_ro import connect_ro
    conn = connect_ro(github_db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        if not cols:
            raise SnapshotError("not_registered", f"repo not registered: {owner}/{name}")
        kind = "kind" if "kind" in cols else "'code'"
        gid = "github_id" if "github_id" in cols else "NULL"
        sync = "last_github_sync_at" if "last_github_sync_at" in cols else "NULL"
        repo = conn.execute(
            f"SELECT id, owner, repo_name, {kind} AS kind, {gid} AS github_id,"
            f" {sync} AS last_sync FROM repos"
            " WHERE lower(owner) = lower(?) AND lower(repo_name) = lower(?)"
            " ORDER BY id LIMIT 1", (owner, name)).fetchone()
        if repo is None:
            raise SnapshotError("not_registered", f"repo not registered: {owner}/{name}")
        rows = conn.execute(
            """SELECT number, title, state, author, created_at, closed_at,
                      COALESCE(is_pull_request, 0) AS is_pr, pr_merged_at
               FROM github_issues WHERE repo_id = ?""", (repo["id"],)).fetchall()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        stats: dict[int, dict] = {}
        if "github_pr_stats" in tables:
            for r in conn.execute(
                    "SELECT pr_number, additions, deletions, changed_files"
                    " FROM github_pr_stats WHERE repo_id = ?", (repo["id"],)):
                stats[r[0]] = {"additions": r[1], "deletions": r[2],
                               "files_changed": r[3], "closes": []}
        if "github_pr_closing_refs" in tables:
            for r in conn.execute(
                    "SELECT pr_number, issue_repo, issue_number"
                    " FROM github_pr_closing_refs WHERE repo_id = ?", (repo["id"],)):
                if r[0] in stats:
                    stats[r[0]]["closes"].append(f"{r[1]}#{r[2]}")
    except sqlite3.OperationalError as exc:
        raise SnapshotError("no_data", f"github.db has no GitHub data yet ({exc})") from None
    finally:
        conn.close()
    return dict(repo), [dict(r) for r in rows], stats


def build_snapshot(
    github_db: str | Path,
    repo: str,
    since: str,
    until: str,
    *,
    allow_incomplete: bool = False,
    tz: Any = localtime.CONFIGURED,
    config: Any = None,
) -> dict[str, Any]:
    """The v1 snapshot of ``repo`` (``owner/name``) for local days since..until.

    ``tz`` defaults to the ``[user] timezone``; ``config`` (a ``HubConfig``)
    supplies the exclusions and owner aliases (#95), ``None`` = load it.
    Raises :class:`SnapshotError`.
    """
    parts = str(repo or "").strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise SnapshotError("bad_args", f"--repo must be OWNER/REPO (got {repo!r})")
    owner, name = parts
    since = _parse_day(since, "since")
    until = _parse_day(until, "until")
    if since > until:
        raise SnapshotError("bad_args", f"--since {since} is after --until {until}")
    if config is None:
        from hub.config import load_config
        config = load_config()
    if _exclusions(config).matches(owner, name):
        # Confidential (#95): answered exactly like an unknown repo.
        raise SnapshotError("not_registered", f"repo not registered: {owner}/{name}")

    zone = localtime.user_timezone() if tz is localtime.CONFIGURED else tz
    row, items, stats = _load(github_db, owner, name)
    label = f"{row['owner']}/{row['repo_name']}"

    # Coverage: the period is complete once the last GitHub sync happened
    # after the end of ``until`` (local midnight that closes it).
    _, period_end = localtime.day_bounds(since, until, zone)
    last_sync = localtime.to_aware(row["last_sync"]) if row["last_sync"] else None
    complete = last_sync is not None and last_sync >= period_end
    if not complete and not allow_incomplete:
        synced = _utc_z(last_sync) if last_sync else None
        raise SnapshotError(
            "incomplete",
            (f"the last GitHub sync of {label} ({synced or 'never'}) does not cover "
             f"the period up to {until}: export a closed range or pass "
             "--allow-incomplete"),
            last_github_sync_at=synced, until=until)

    def day(v: Any) -> str | None:
        return localtime.local_day(v, zone) if v else None

    def in_period(d: str | None) -> bool:
        return d is not None and since <= d <= until

    issues = [i for i in items if not i["is_pr"]]
    prs = [p for p in items if p["is_pr"]]

    opened = closed = total_created = total_closed = 0
    for i in issues:
        created_d, closed_d = day(i["created_at"]), day(i["closed_at"])
        is_closed = (i["state"] or "").lower() == "closed"
        if in_period(created_d):
            opened += 1
        if is_closed and in_period(closed_d):
            closed += 1
        if created_d is not None and created_d <= until:
            total_created += 1
            if is_closed and closed_d is not None and closed_d <= until:
                total_closed += 1

    notes: list[str] = []
    merged: list[dict[str, Any]] = []
    open_at_cut = open_bots = 0
    for p in prs:
        created_d = day(p["created_at"])
        merged_d = day(p["pr_merged_at"])
        closed_d = day(p["closed_at"]) or merged_d
        if (created_d is not None and created_d <= until
                and not (closed_d is not None and closed_d <= until)):
            open_at_cut += 1
            open_bots += is_bot(p["author"])
        if in_period(merged_d):
            merged.append(p)

    by_pr = []
    adds = dels = 0
    missing = []
    for p in merged:
        st = stats.get(p["number"])
        entry: dict[str, Any] = {
            "number": p["number"],
            "title": p["title"],
            "author": p["author"] or "",
            "merged_at": _utc_z(p["pr_merged_at"]),
            "closes": sorted(st["closes"]) if st else [],
            "bot": is_bot(p["author"]),
        }
        if st:
            entry.update(additions=st["additions"], deletions=st["deletions"],
                         files_changed=st["files_changed"])
            adds += st["additions"]
            dels += st["deletions"]
        else:
            missing.append(p["number"])
        by_pr.append(entry)
    by_pr.sort(key=lambda e: (e["merged_at"] or "", e["number"]))
    if missing:
        nums = ", ".join(f"#{n}" for n in sorted(missing))
        notes.append(f"PR stats missing for {nums}: not in total_additions/total_deletions")

    freshness: dict[str, Any] = {"complete": complete}
    if not complete:
        freshness["last_github_sync_at"] = _utc_z(last_sync) if last_sync else None
        notes.append("period not covered by the last GitHub sync: numbers may change")

    kind = (row["kind"] or "code").lower()
    return {
        "schema_version": SCHEMA_VERSION,
        "org": _org_of(row["owner"], config),
        "repo": {
            "id": row["github_id"],
            "label": label,
            # 'remote' (#94) is a code repo without a local clone.
            "kind": "board" if kind == "board" else "code",
        },
        "date": until,
        "timezone": localtime.zone_label(zone),
        "period": {
            "start": since,
            "end": until,
            "days": (date.fromisoformat(until) - date.fromisoformat(since)).days + 1,
        },
        "issues": {
            "opened_this_period": opened,
            "closed_this_period": closed,
            "total_open": total_created - total_closed,
            "total_closed": total_closed,
        },
        "prs": {
            "merged_this_period": len(merged),
            "open": open_at_cut,
            "open_bots": open_bots,
            "total_additions": adds,
            "total_deletions": dels,
        },
        # Per-person credit is opt-in territory (mool-ops issue 05): empty here.
        "by_developer": {},
        "by_pr": by_pr,
        "freshness": freshness,
        "blockers": [],
        "notes": notes,
        "extensions": {},
    }


def dumps(snapshot: dict[str, Any]) -> str:
    """Canonical text: sorted keys, 2-space indent, UTF-8, trailing newline."""
    return json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_atomic(path: str | Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same dir + ``os.replace``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
