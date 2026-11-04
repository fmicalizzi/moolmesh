# 04 · Snapshot schema v1

Machine-readable: [schema/snapshot.v1.schema.json](../schema/snapshot.v1.schema.json). Generated example: [examples/data/snapshots/1001/2026-11-03.json](../examples/data/snapshots/1001/2026-11-03.json).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `org` | string | Registry `organization.id` |
| `repo` | object | `id` (GitHub numeric id), `label` (`owner/name`), `kind`, `membership` |
| `date` | date | Cut date (local) |
| `timezone` | string | IANA zone used for grouping |
| `period` | object | `start`, `end` (local days, inclusive), `days` |
| `issues` | object | `closed_this_period`, `opened_this_period`, `total_open`, `total_closed` (at the cut, verified) |
| `prs` | object | `merged_this_period`, `open`, `open_bots`, `total_additions`, `total_deletions` |
| `by_developer` | map | login → `alias`, `issues_closed`, `prs_merged`, `additions`, `deletions`, `prs[]` (bots excluded) |
| `by_pr` | array | `number`, `title`, `author`, `additions`, `deletions`, `files_changed`, `merged_at` (UTC), `closes[]` (`owner/repo#n`), `bot` |
| `devs_active` | int | People with activity in the period |
| `devs_available` | array | `team` members with no activity |
| `blockers` | array | Free text, written by a person or agent |
| `notes` | array | Automatic notes, e.g. chain adjustments |
| `extensions` | object | Per-repo free-form data from the registry |

## Compatibility

- New optional fields → minor version (`1.1`). Removing or renaming → major version.
- Readers must ignore unknown fields.
- `by_developer` may be empty when per-person data is disabled for the organization.
