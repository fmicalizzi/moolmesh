# Effort export — schema v1 (`moolmesh.effort`)

`mool effort export` (#140) writes what ONE device recorded on an explicit list
of repos, per local day and provider. An organization's tooling merges the
files of several devices; it never reads MoolMesh's SQLite schema.

```
mool device init [--label TEXT] [--reset]      # once per machine, opt-in
mool effort export --org ORG --timezone IANA --since YYYY-MM-DD --until YYYY-MM-DD \
    (--repo OWNER/NAME ... | --repos-from FILE) [--output FILE]
```

## Example

```json
{
  "device": {"id": "<64 hex: HMAC-SHA256(device secret, org)>", "label": "laptop"},
  "data_through": "2026-11-09T11:59:00Z",
  "generated_at": "2026-11-09T12:00:00Z",
  "kind": "moolmesh.effort",
  "measures": {"primary": "days_worked", "days_worked": "...", "sessions": "...",
               "touches": "...", "scope": "..."},
  "org": "acme-dev",
  "period": {"since": "2026-11-01", "until": "2026-11-07"},
  "repos": [{"repo_id": 1001, "repo": "acme-dev/portal"},
            {"repo_id": 1002, "repo": "acme-dev/api"}],
  "rows": [
    {"repo_id": 1001, "repo": "acme-dev/portal", "day": "2026-11-03",
     "provider": "claude", "sessions": 1, "touches": 2}
  ],
  "schema_version": "1.0",
  "stale": false,
  "timezone": "America/Costa_Rica"
}
```

## Header

| Field | Meaning |
|---|---|
| `schema_version` | `"1.0"`. A new optional field bumps the minor; a removal or rename the major. |
| `kind` | Always `"moolmesh.effort"`. |
| `device.id` | `HMAC-SHA256(secret, org)` in hex, with `org` lowercased. The secret stays in `~/.moolmesh/device.json`; the same device reporting to two organizations shows two unrelated ids. A new `mool device init --reset` (or a new machine) gives a new id: the registry maps several ids to one person. |
| `device.label` | The optional `--label` of `mool device init`, or `null`. |
| `org` | The `--org` value, lowercased. |
| `timezone` | The `--timezone` IANA name (or `UTC`). It defines `day`. |
| `period` | `since` / `until`, inclusive local days. |
| `generated_at` | UTC time of the export. |
| `data_through` | UTC time up to which the daemon's own record says data is complete: the newest watcher heartbeat (it only advances once a startup catch-up is drained), capped at the ingest time of the first event not yet attributed to a repo. `null` when unknown. |
| `stale` | `true` when `data_through` is unknown or earlier than the end of the period (or now, if the period is still open) by more than `[daemon] stale_after_minutes`. A stale file should be re-exported later. |
| `repos` | The listed repos that could be exported: `repo_id` + current `repo`. A repo here with no rows had no recorded activity. |
| `measures` | Text definitions of the measures below. |

## Rows

One row per `(repo_id, day, provider)` with activity, sorted by those keys.

| Field | Meaning |
|---|---|
| `repo_id` | Numeric GitHub id (#94). Stable across transfers and renames. |
| `repo` | Current `owner/name` label from `github.db`. |
| `day` | Local day in `timezone` of each event's own activity clock (live rows: their ingest time, or their own time when it lies within the live window; caught-up/backfilled rows: their own time — the #53/#131 rule). Never the machine's zone. |
| `provider` | The agent (`claude`, `codex`, `opencode`, …). |
| `sessions` | Distinct sessions of that provider with at least one event on that repo that day. |
| `touches` | Distinct session edges with at least one event on that repo that day: one per (session, file) — the portfolio rollup's `session_touches` unit — or per (session, working directory) for a session with no file edge. |

**Primary measure: days worked** — the count of distinct `day` values, per
repo, per device or across devices. `sessions` and `touches` are secondary and
**not additive** across repos, providers or days: one session can touch several
repos and span several days.

## What is never in the file

- Repos not listed with `--repo` / `--repos-from`, `exclude_owners` /
  `exclude_repos` (#95), and folders without a git remote: no row, no count, no
  device total. A session that touched a listed repo and a confidential one
  contributes only its edges on the listed repo.
- A listed repo without a numeric GitHub id yet (not synced since #94): omitted,
  with a warning on stderr that names only that repo.
- Paths, hostnames, user names, author identities (plain or hashed), session
  ids, prompts, the device secret.

## Merging and re-exports

The merge key is `(device.id, repo_id, day)`: a re-export of a period
**replaces** that device's rows for those days (catch-up after an outage can
change past days). Two runs over the same data and arguments produce identical
`rows`.

## Refusals (exit codes)

| Case | Exit |
|---|---|
| No repo list | 2 |
| Bad date, unknown `--timezone`, malformed repo | 2 |
| No `device.json` (→ `mool device init`) | 3 |
| `device.json` from another machine (→ `mool device init --reset`) | 3 |
| None of the listed repos can be exported / no `github.db` | 1 |

## Reads

`events.db`, `workspace.db` and `github.db`, all `mode=ro`. No GitHub API call,
no migration. A repo is matched to its workspaces by its current `owner/name`
remote or by its registered clone path.
