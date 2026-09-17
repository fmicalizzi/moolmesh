# CLAUDE.md

**Read [`AGENTS.md`](AGENTS.md) first — it is the full system prompt for working in
this repository** (identity, invariants, architecture map, conventions, dev/test and
release workflow, git rules).

Quick orientation:
- **This repo IS the live product** — MoolMesh v1.8.x, PyPI, CI. Never plan against
  any older `ai_session_hub` / `live-monitor` predecessor.
- Why: [`PHILOSOPHY.md`](PHILOSOPHY.md). Where to: [`VISION_ROADMAP.md`](VISION_ROADMAP.md)
  (español: [`VISION_ROADMAP.es.md`](VISION_ROADMAP.es.md)). What shipped / queued:
  [`ROADMAP.md`](ROADMAP.md), [`CHANGELOG.md`](CHANGELOG.md).
- Non-negotiables: zero-dependency (stdlib + SQLite), zero-cloud, read-only
  observation base, don't break the hot path, no provider lock-in.
- Commit only when asked; use the owner's git signature; never add `Co-Authored-By`.
