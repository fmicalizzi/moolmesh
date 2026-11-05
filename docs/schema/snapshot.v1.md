# Snapshot schema v1

`mool snapshot export` and the MCP tool `get_snapshot` return one JSON object
per repo and range — a stable contract for tools that build on MoolMesh
(reporting, coordination) without reading the internal SQLite schema, which
changes through migrations.

Machine-readable schema: [`snapshot.v1.schema.json`](snapshot.v1.schema.json).

```bash
mool snapshot export --repo acme/widget-services --since 2026-11-02 --until 2026-11-03
mool snapshot export --repo acme/widget-services --since 2026-11-02 --until 2026-11-03 \
    --output reports/widget-services/2026-11-03.json
```

`--output` writes atomically (temp file in the same folder, then rename), so
a cron job never leaves a half-written file. `--format json` is the only
format (and the default).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `org` | string | The client the owner maps to through `client_owner_aliases` (#95, local config or shared registry); otherwise the GitHub owner |
| `repo` | object | `id` (numeric GitHub id; `null` until the first GitHub sync records it, #94), `label` (`owner/name` as last synced), `kind` (`code` or `board`; a remote-only repo, #94, is `code`) |
| `date` | date | The cut: the last day of the period (`until`) |
| `timezone` | string | Zone the days are bucketed in: IANA zone of `[user] timezone`, `"UTC"`, or when unset the system zone's IANA name (or `"UTC±HH:MM"` if it cannot be named). Never empty since v1.36.1 (#146) |
| `period` | object | `start`, `end` (local days, inclusive), `days` |
| `issues` | object | `opened_this_period`, `closed_this_period`, and at the cut `total_open`, `total_closed` |
| `prs` | object | `merged_this_period`, `open` and `open_bots` (open at the cut), `total_additions`, `total_deletions` (of the PRs merged in the period) |
| `by_developer` | map | Always `{}` in MoolMesh 1.x for now: per-person credit is a separate opt-in decision |
| `by_pr` | array | Each PR merged in the period: `number`, `title`, `author`, `additions`, `deletions`, `files_changed`, `merged_at` (UTC, `…Z`), `closes[]` (`owner/repo#n`), `bot` |
| `freshness` | object | `complete` (bool); when `false`, also `last_github_sync_at` (UTC) |
| `blockers` | array | `[]` (reserved for text written by a person or agent) |
| `notes` | array | Automatic notes, e.g. PRs whose stats are not fetched yet |
| `extensions` | object | `{}` (reserved) |

### Local days

Every "which day" is a local day in `[user] timezone` (`hub/localtime.py`,
#81) — the same definition as digests, the workspace rollup and the
portfolio. A PR merged at 03:30 UTC on Nov 3 is a Nov 2 PR in
`America/Costa_Rica`.

### Totals at the cut

`total_open` / `total_closed` are computed from the dates, not from the
current state: issues created on or before `until`, minus those closed on or
before it. A past range keeps its numbers when the repo moves on (an issue
closed later still counts as open at that cut). `prs.open` follows the same
rule (created on or before the cut, not closed or merged by then). Pull
requests are never counted as issues.

The totals are as complete as MoolMesh's GitHub cache for that repo.

### PR stats and cross-repo references

`additions`, `deletions`, `files_changed` and `closes` come from the PR stats
(#80). `closes` is always qualified — `acme/backlog#5`, never `#5` — because a
PR can close an issue in another repo, and two repos share numbers. A PR
whose stats are not fetched yet has no `additions`/`deletions`/
`files_changed` (and an empty `closes`), is left out of
`total_additions`/`total_deletions`, and is named in `notes`; run
`mool repo backfill-pr-stats` to fill older PRs.

Bots (`*[bot]`, `app/*`, dependabot) count in every total; `bot: true` marks
them so a reader never credits them to a person.

### Exclusions

A repo excluded through `exclude_owners` / `exclude_repos` (#95) answers
exactly like an unregistered repo: `repo no registrado`, exit 1.

## Determinism

Two exports of the same range over the same data are byte-identical: keys
are sorted, `by_pr` is ordered by `merged_at` then `number`, `closes` is
sorted, the text is UTF-8 with a 2-space indent and a trailing newline. The
body carries **no generation time** and no sync time for a complete period
(the sync time changes every few minutes while the data for a closed period
does not). If you need when a file was made, use the file's mtime or your
own wrapper.

## Freshness: a range newer than the last sync

The period is **complete** when the repo's last successful GitHub sync
(`repos.last_github_sync_at`, the clock `hub/freshness.py` reads for #76)
happened after the end of `until` — local midnight closing that day.

- Complete → `"freshness": {"complete": true}`, exit 0.
- Not complete (the range includes today, or the daemon was down past the
  end of the range, or the repo never synced) → **refused**: a message on
  stderr and **exit 3**; the MCP tool answers
  `{"error": …, "reason": "incomplete", "last_github_sync_at": …}`.
- With `--allow-incomplete` (MCP: `allow_incomplete=true`) it is exported
  anyway, marked `"freshness": {"complete": false, "last_github_sync_at": …}`
  plus a note. Such a payload is not reproducible by nature: the next sync
  can change it.

Refusing by default keeps cron jobs honest: a job that exports "yesterday"
after midnight gets a stable file, and one that runs while the daemon is
down fails loudly instead of writing frozen numbers. The MCP tool's
top-level `freshness` / `stale` (#76) are the global data flag, like every
other MCP tool; the payload is under `snapshot`.

Exit codes: `0` ok, `1` repo not registered / no GitHub data, `2` bad
arguments, `3` period not covered by the last sync.

## Compatibility

- A new optional field → minor version (`1.1`). Removing or renaming a field,
  or changing its meaning → major version (`2.0`).
- Readers must ignore unknown fields.
- Optional fields defined in the schema but not emitted yet (`devs_active`,
  `devs_available`, `repo.membership`, `kind: docs`) keep their meaning if a
  later minor version starts emitting them.

## Differences from the mool-ops proposal

The design kit in `docs/proposals/mool-ops/` proposed this schema. MoolMesh
adopts it with these changes:

- **`freshness` (required, new).** The kit had no coverage field; the export
  must say whether a period is covered by the last sync (#82 acceptance).
- **`repo.id` may be `null`.** MoolMesh learns the numeric GitHub id on the
  first API sync (#94); a repo registered before that still has a label. A
  complete period implies a sync, so in practice it is set.
- **`period.days` required; `by_pr[].closes` and `bot` required;
  `merged_at` pinned to `YYYY-MM-DDTHH:MM:SSZ`.** Always emitted, so readers
  can rely on them.
- **`prs.open` is open at the cut, not open now.** The kit's reference read
  the current state, which changes a past period's snapshot on every run;
  MoolMesh exports any range, so it computes it from the dates.
- **`by_developer` is empty and `devs_active` / `devs_available` are not
  emitted.** Per-person credit is a separate opt-in decision (kit issue 05).
- **No chaining from a previous snapshot.** The kit's reference starts each
  period at the last snapshot's end and reconciles chained totals in
  `notes`. MoolMesh holds the full issue history in `github.db`, so it
  computes the totals at the cut directly for any range; there is nothing to
  chain or adjust.
