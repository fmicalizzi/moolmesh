# Versioned snapshot export (stable contract for complementary tools)

## Context
Tools that build on MoolMesh (reporting, coordination) need a stable contract that does not depend on the internal SQLite schema, which evolves through migrations.

## Proposal
`mool snapshot export --repo OWNER/REPO --since D --until D --format json`, idempotent and cron-friendly, with `schema_version` and a documented schema: period (local days), issues opened/closed, totals at the cut, merged PRs with their stats, and a freshness field. Same payload as an MCP tool. A proposed v1 schema exists in a design kit shared with the maintainers.

## Acceptance
Two exports of the same range return identical JSON; the schema is documented under `docs/`; the export refuses (or flags) a range newer than the last sync.
