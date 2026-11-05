"""Per-device effort export for one organization (#140).

``mool effort export`` writes, for an explicit list of repos, one row per
``(repo, local day, provider)`` with the sessions and touches this device
recorded there. The organization's tooling (mool-ops) merges the files of
several devices; the contract is ``docs/schema/effort.v1.md``.

Rules (the spec in #140):

- **Deny by default.** No repo list → refused. Only listed repos appear; an
  unlisted repo, an ``exclude_repos``/``exclude_owners`` one (#95) and a
  folder with no remote add nothing — no row, no count, no total. A session
  that touched a listed repo and a confidential one contributes only its
  edges on the listed repo.
- **Stable key.** ``repo_id`` is the numeric GitHub id (#94); ``repo`` is the
  current ``owner/name`` from ``github.db`` (a transfer keeps the id and
  updates the label). A listed repo without an id yet is OMITTED with a
  warning on stderr that names only that repo.
- **One day definition.** ``day`` is the local day in ``--timezone`` of each
  event's own activity clock (``_edge_activity_dt``, #53/#131) — never the
  machine zone, never ``edge_active_days`` (bucketed in ``[user] timezone``).
- **Measures.** ``sessions``: distinct sessions of that provider with at least
  one event on that repo that day. ``touches``: distinct session edges — the
  rollup's ``session_touches`` unit, one per (session, provider, file), or per
  (session, provider, cwd) for a session with no file edge (#40) — with at
  least one event that day. Neither is additive across repos, providers or
  days; the primary measure is days worked (distinct ``day`` values).
- **Determinism.** ``rows`` depend only on the data and the arguments; the
  header carries ``generated_at``/``data_through``/``stale``.

Read-only: ``events.db``, ``workspace.db`` and ``github.db`` are opened
``mode=ro``; no GitHub API call, no migration.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

from hub import localtime

SCHEMA_VERSION = "1.0"
KIND = "moolmesh.effort"

EXIT_CODES = {"bad_args": 2, "no_repo_list": 2, "no_repos": 1, "no_data": 1}

MEASURES = {
    "primary": "days_worked",
    "days_worked": ("distinct `day` values (per repo, per device, or across "
                    "devices by person); the primary measure"),
    "sessions": ("distinct agent sessions of `provider` with activity on `repo` "
                 "that `day`; NOT additive across repos, providers or days "
                 "(one session can touch several repos and span days)"),
    "touches": ("distinct (session, file) edges on `repo` with activity that "
                "`day` (the portfolio rollup's session_touches unit); NOT "
                "additive across repos, providers or days"),
    "scope": ("only the repos listed for this export; work in unlisted or "
              "confidential repos and folders without a remote is not here"),
}

_CHUNK = 400


class EffortError(Exception):
    """An export that cannot be built; ``reason`` is a stable token."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.reason, 1)


def _parse_day(value: str, name: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise EffortError("bad_args",
                          f"--{name} debe ser una fecha YYYY-MM-DD (recibí {value!r})"
                          ) from None


def resolve_zone(name: str) -> tzinfo:
    """A strict IANA zone: unlike ``localtime.resolve_timezone`` an unknown
    name is an error, never a silent fall back to the machine zone."""
    text = str(name or "").strip()
    if not text:
        raise EffortError("bad_args", "--timezone es obligatorio (nombre IANA)")
    if text.upper() in ("UTC", "ETC/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise EffortError("bad_args",
                          f"--timezone {text!r} no es una zona IANA conocida en "
                          "esta máquina") from None


def read_repo_list(repos: Iterable[str] | None, repos_from: str | None) -> list[str]:
    """``--repo`` values plus the lines of ``--repos-from`` (``#`` comments)."""
    out = [str(r) for r in (repos or [])]
    if repos_from:
        try:
            text = Path(repos_from).read_text(encoding="utf-8")
        except OSError as exc:
            raise EffortError("bad_args",
                              f"no pude leer --repos-from: {exc.strerror}") from None
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def _normalize_repos(specs: list[str]) -> list[tuple[str, str]]:
    from hub.config import parse_board_slug
    seen: dict[str, tuple[str, str]] = {}
    for spec in specs:
        parsed = parse_board_slug(spec)
        if parsed is None:
            raise EffortError("bad_args", f"repo inválido {spec!r}: usá OWNER/NAME")
        seen.setdefault(f"{parsed[0]}/{parsed[1]}".lower(), parsed)
    return [seen[k] for k in sorted(seen)]


def _ro(path: str | Path) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    from hub.sqlite_ro import connect_ro
    return connect_ro(path)


def _resolve_repos(github_db: str | Path, listed: list[tuple[str, str]],
                   exclusions: Any, warn) -> dict[int, dict[str, Any]]:
    """``{github_id: {repo, path}}`` for the listed repos that can be exported."""
    conn = _ro(github_db)
    if conn is None:
        raise EffortError("no_data", "no existe github.db — registrá los repos "
                                     "con mool repo add")
    out: dict[int, dict[str, Any]] = {}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        gid_col = "github_id" if "github_id" in cols else "NULL"
        for owner, name in listed:
            spec = f"{owner}/{name}"
            if exclusions.matches(owner, name):
                warn(f"{spec}: no registrado; se omite")  # #95: same as unknown
                continue
            row = conn.execute(
                f"SELECT owner, repo_name, path, {gid_col} FROM repos"
                " WHERE lower(owner) = lower(?) AND lower(repo_name) = lower(?)"
                " ORDER BY id LIMIT 1", (owner, name)).fetchone() if cols else None
            if row is None:
                warn(f"{spec}: no registrado; se omite")
                continue
            cur_owner, cur_name, path, gid = row
            if exclusions.matches(cur_owner, cur_name):
                warn(f"{spec}: no registrado; se omite")
                continue
            if gid is None:
                warn(f"{spec}: todavía sin id numérico de GitHub (esperá un sync "
                     "del daemon o corré mool repo sync); se omite")
                continue
            out.setdefault(int(gid), {"repo": f"{cur_owner}/{cur_name}",
                                      "path": path})
    except sqlite3.Error as exc:
        raise EffortError("no_data", f"no pude leer github.db ({exc})") from None
    finally:
        conn.close()
    return out


def _workspace_map(workspace_db: str | Path, targets: dict[int, dict[str, Any]],
                   exclusions: Any) -> dict[int, int]:
    """``{workspace_id: github_id}`` for the workspaces of the listed repos.

    A workspace belongs to a repo when its remote is the repo's current
    ``owner/name`` or its git root is the repo's registered clone (which
    keeps matching after a transfer, while the local remote still has the
    old name). Workspaces without a remote (``git_root``/``path_hash``) and
    excluded remotes never match.
    """
    from hub.config import parse_board_slug
    by_name = {v["repo"].lower(): gid for gid, v in targets.items()}
    by_path = {os.path.normcase(os.path.normpath(v["path"])): gid
               for gid, v in targets.items()
               if v.get("path") and os.path.isabs(v["path"])}
    conn = _ro(workspace_db)
    if conn is None:
        return {}
    out: dict[int, int] = {}
    try:
        for wid, remote, root in conn.execute(
                "SELECT id, remote_url, root_path FROM workspaces"
                " WHERE kind = 'git_remote'"):
            if not remote or exclusions.matches_remote(remote):
                continue
            parsed = parse_board_slug(remote)
            gid = by_name.get(f"{parsed[0]}/{parsed[1]}".lower()) if parsed else None
            if gid is None and root:
                gid = by_path.get(os.path.normcase(os.path.normpath(root)))
            if gid is not None:
                out[int(wid)] = gid
    except sqlite3.Error as exc:
        raise EffortError("no_data", f"no pude leer workspace.db ({exc})") from None
    finally:
        conn.close()
    return out


def _edges(workspace_db: str | Path, ws_map: dict[int, int]):
    """File edges and cwd edges of the mapped workspaces → github_id."""
    files: dict[tuple[str, str, str], int] = {}
    cwds: dict[tuple[str, str, str], int] = {}
    if not ws_map:
        return files, cwds
    conn = _ro(workspace_db)
    if conn is None:
        return files, cwds
    try:
        ids = sorted(ws_map)
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for sid, prov, fp, via, wid in conn.execute(
                    "SELECT session_id, provider, file_path, via, workspace_id"
                    f" FROM path_attributions WHERE workspace_id IN ({marks})",
                    chunk):
                target = cwds if via == "cwd" else files
                target[(sid, prov or "", fp)] = ws_map[wid]
    except sqlite3.Error as exc:
        raise EffortError("no_data", f"no pude leer workspace.db ({exc})") from None
    finally:
        conn.close()
    return files, cwds


def _count(events_db: str | Path, files, cwds, zone: tzinfo, since: str,
           until: str) -> dict[tuple[int, str, str], tuple[set, set]]:
    """``{(github_id, day, provider): (sessions, edges)}`` within since..until."""
    from hub.cache.workspace_store import _edge_activity_dt
    from hub.models.base import SHELL_TOOLS

    cells: dict[tuple[int, str, str], tuple[set, set]] = {}
    sessions = sorted({(k[0], k[1]) for k in files} | {(k[0], k[1]) for k in cwds})
    if not sessions:
        return cells
    conn = _ro(events_db)
    if conn is None:
        return cells
    start, _ = localtime.day_bounds(since, until, zone)
    # An event's activity clock is never later than its ingest time
    # (created_at), so events ingested before the period cannot fall in it.
    floor = start.timestamp()
    try:
        has_hist = "historical" in {r[1] for r in conn.execute(
            "PRAGMA table_info(events)")}
        hist = "historical" if has_hist else "0"
        sids = sorted({s for s, _ in sessions})
        for i in range(0, len(sids), _CHUNK):
            chunk = sids[i:i + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for sid, prov, fp, cwd, ts, created, tool, h in conn.execute(
                    "SELECT session_id, provider, file_path, cwd, timestamp,"
                    f" created_at, tool_name, {hist} FROM events"
                    f" WHERE session_id IN ({marks}) AND created_at >= ?",
                    (*chunk, floor)):
                prov = prov or ""
                hits = []
                if fp and (tool or "") not in SHELL_TOOLS:
                    gid = files.get((sid, prov, fp))
                    if gid is not None:
                        hits.append((gid, ("f", sid, prov, fp)))
                if cwd:
                    gid = cwds.get((sid, prov, cwd))
                    if gid is not None:
                        hits.append((gid, ("c", sid, prov, cwd)))
                if not hits:
                    continue
                dt = _edge_activity_dt(ts, created, bool(h))
                if dt is None:
                    continue
                day = dt.astimezone(zone).date().isoformat()
                if not since <= day <= until:
                    continue
                for gid, edge in hits:
                    s, e = cells.setdefault((gid, day, prov), (set(), set()))
                    s.add(sid)
                    e.add(edge)
    except sqlite3.Error as exc:
        raise EffortError("no_data", f"no pude leer events.db ({exc})") from None
    finally:
        conn.close()
    return cells


def _utc_z(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_effort(
    *,
    events_db: str | Path,
    workspace_db: str | Path,
    github_db: str | Path,
    org: str,
    timezone_name: str,
    since: str,
    until: str,
    repos: list[str],
    device: dict[str, Any],
    config: Any = None,
    now: float | None = None,
    threshold_minutes: int | None = None,
    warn=None,
) -> dict[str, Any]:
    """The v1 effort document. ``device`` is a loaded ``device.json``.

    Raises :class:`EffortError`. ``warn(text)`` receives per-repo notices
    (each names only the listed repo it is about).
    """
    import time

    from hub import freshness
    from hub.config import exclusions_of, load_config
    from hub.device import device_id, org_key

    warn = warn or (lambda _msg: None)
    if not org_key(org):
        raise EffortError("bad_args", "--org es obligatorio")
    if not repos:
        raise EffortError(
            "no_repo_list",
            "falta la lista de repos: pasá --repo OWNER/NAME (repetible) o "
            "--repos-from ARCHIVO. Sin lista no se exporta nada (deny by default).")
    zone = resolve_zone(timezone_name)
    since = _parse_day(since, "since")
    until = _parse_day(until, "until")
    if since > until:
        raise EffortError("bad_args", f"--since {since} es posterior a --until {until}")
    listed = _normalize_repos(repos)
    if config is None:
        config = load_config()
    exclusions = exclusions_of(config)

    targets = _resolve_repos(github_db, listed, exclusions, warn)
    if not targets:
        raise EffortError("no_repos", "ninguno de los repos listados se puede "
                                      "exportar (ver avisos)")
    ws_map = _workspace_map(workspace_db, targets, exclusions)
    files, cwds = _edges(workspace_db, ws_map)
    cells = _count(events_db, files, cwds, zone, since, until)

    rows = [
        {"repo_id": gid, "repo": targets[gid]["repo"], "day": day,
         "provider": prov, "sessions": len(s), "touches": len(e)}
        for (gid, day, prov), (s, e) in cells.items()
    ]
    rows.sort(key=lambda r: (r["repo_id"], r["day"], r["provider"]))

    now = time.time() if now is None else now
    minutes = (freshness.stale_after_minutes() if threshold_minutes is None
               else threshold_minutes)
    through = freshness.effort_data_through(events_db, workspace_db)
    _, period_end = localtime.day_bounds(since, until, zone)
    needed = min(period_end.timestamp(), now)
    stale = through is None or through < needed - minutes * 60

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "device": {"id": device_id(device["secret"], org),
                   "label": device.get("label") or None},
        "org": org_key(org),
        "timezone": localtime.zone_key(zone),
        "period": {"since": since, "until": until},
        "generated_at": _utc_z(now),
        "data_through": _utc_z(through),
        "stale": stale,
        "repos": [{"repo_id": gid, "repo": targets[gid]["repo"]}
                  for gid in sorted(targets)],
        "measures": MEASURES,
        "rows": rows,
    }


def dumps(doc: dict[str, Any]) -> str:
    """Canonical text: sorted keys, 2-space indent, UTF-8, trailing newline."""
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
