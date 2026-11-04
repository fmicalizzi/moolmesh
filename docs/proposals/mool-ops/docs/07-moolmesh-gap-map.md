# 07 · MoolMesh gap map

Reviewed against MoolMesh v1.27.x (ROADMAP, VISION_ROADMAP §6, `hub/cache/*`, `hub/digests/*`, dashboard routes).

## Already there

| Capability | Where | Use for the tracker |
|---|---|---|
| Client → project → materials hierarchy, derived state (active / cooling / delivered / stalled / paused) | Portfolio (#24–#29) | Portfolio overview per organization |
| Per-repo board: issues, PRs, milestones, Projects v2 | Project Pulse | Board view for coordinators |
| Daily/weekly digests with per-author stats and continuity | `hub/digests` (L1 stats → L2 template → L3 narrative) | Close to a snapshot + report |
| GitHub cache with author, assignees, labels, milestone per issue/PR; lines per commit | `github.db` | Data source once a stable contract exists |
| MCP tools over all of the above | `hub/mcp_server.py` | The coordinator's own agents can query it |
| Cross-machine aggregation, multi-user, auth | VISION §6 Phase D / ROADMAP v2.x | Deferred; the private data repo is a practical seed |

## Missing for operations tracking

| Gap | Impact | Issue draft |
|---|---|---|
| Daemon may stop silently; data goes stale with no flag | A coordinator reads stale numbers as current | [01](../issues/01-stale-data-flag-and-autostart.md) (extends #76) |
| No per-PR lines/files/closing refs | Cannot report what was merged per PR | [02](../issues/02-pr-stats.md) |
| UTC-day digests; all-time outcome only | Evening work moves to the next day; no "this week" answer | [03](../issues/03-local-timezone-and-windows.md) |
| No stable export contract | Complementary tools would couple to SQLite internals | [04](../issues/04-versioned-snapshot-export.md) |
| Outcome is contributor-agnostic by design | No per-person view for a team coordinator | [05](../issues/05-per-contributor-opt-in.md) |
| Issues-only repos look like "no delivery" | Task boards are misread | [06](../issues/06-board-repos.md) |
| A local clone is required per repo | Following many repos of an organization is heavy | [07](../issues/07-remote-only-repos.md) |
| One client per owner, or per-project overrides | A client spanning an org and personal accounts needs one override per repo; grouping differs per machine | [08](../issues/08-client-spanning-owners.md) |

## Suggested order

01 → 02 → 03 → 04 unlock using MoolMesh as the engine's data source. 05 opens the team tier. 06–08 improve multi-owner organizations.
