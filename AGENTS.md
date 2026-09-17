# AGENTS.md — Working in the MoolMesh repo

This file is the system prompt for anyone — human or AI agent — working in this
repository. Read it before making changes. It is the single source of truth for
*how* work happens here; `PHILOSOPHY.md` is *why*, `VISION_ROADMAP.md` is *where to*.

---

## 0. Golden rule

**This repository (`moolmesh`) IS the live product.** v1.8.x, published to PyPI
(`pip install moolmesh`), remote `github.com/fmicalizzi/moolmesh`, CI in
`publish.yml`. Any older `ai_session_hub` / `live-monitor` tree elsewhere on disk
is a historical predecessor — never analyze, verify, or plan against it. Everything
here stands on the current code.

---

## 1. What MoolMesh is

An **observer that evolves into a coordinator** for autonomous AI coding agents. It
auto-discovers session files from every supported agent, normalizes them into a
unified event model in SQLite, and exposes that state to humans (dashboard) and
machines (MCP). The observation is the substrate for agent-to-agent coordination.

- Read `PHILOSOPHY.md` for the Dual Axiom (Human-First & Agent-First) and the
  Interaction Matrix (A2A / H2A / H2H / Team-to-Swarm).
- Read `VISION_ROADMAP.md` (or `.es.md`) for the Observe → Correlate → Coordinate
  ladder and current priorities.

---

## 2. Invariants — do not cross without an explicit, separate decision

1. **Zero external dependencies.** Python 3.11+ stdlib + SQLite only. `dependencies = []`
   in `pyproject.toml` stays empty. A new runtime dep requires an explicit owner
   decision and a documented justification.
2. **Zero cloud.** Everything runs locally. No accounts, no keys leaving the machine.
3. **Read-only observation base.** MoolMesh reads session files, never mutates them.
   Any write/coordination capability is a deliberate, separately-decided step.
4. **Don't break the hot path.** New features go in separate tables; the SSE stream
   and dashboard stay fast. Two DBs stay separate: `~/.moolmesh/events.db` (sessions)
   and `~/.moolmesh/github.db` (git/GitHub).
5. **No provider lock-in.** Each provider is a pluggable quartet; adding one must not
   change the others or the core.
6. **State is the single source of truth** — if it matters, it is persisted in SQLite
   and queryable.

---

## 3. Architecture map

```
hub/
├── models/       # dataclasses per provider + UnifiedEvent (models/base.py: Provider enum)
├── parsers/      # raw session file (JSONL/SQLite) → provider model
├── adapters/     # provider model → UnifiedEvent
├── watchers/     # incremental file/rowid tailing per provider (+ kqueue/polling)
├── harvesters/   # git_harvester, github_harvester (discover → offset → parse → store → SSE)
├── integrations/ # github_client, LLM (OpenAI-compat) client
├── cache/        # event_store.py (events.db), git_store.py (github.db)
├── correlation/  # cross-session linking (linker.py)
├── digests/      # L1 stats → L2 template → L3 LLM narrative
├── dashboard/    # HTTP server + SSE + static/*.html (4 views)
├── analyzers/ renderers/   # analytics, output rendering
├── mcp_server.py # read-only MCP (stdio) query tools
├── cli.py        # `mool` entrypoint
├── daemon.py discovery.py config.py log.py backfill.py
```

- **Provider quartet:** `model → parser → adapter → watcher`, ~300–500 LOC total.
  The 5 current providers: Claude, Codex, Qwen, OpenCode, Cursor (`models/base.py`).
- **MCP server** exposes read-only query tools (`get_active_sessions`,
  `get_session_chain`, `search_session_content`, `get_session_detail`, …). These are
  the coordination primitives — treat them as product surface, not plumbing.

---

## 4. Conventions

- **Language:** code, comments, and repo docs (`README`, `ROADMAP`, `PHILOSOPHY`,
  this file) in **English**. **User-facing output** — dashboard UI, digests, and
  end-user logs — in **Spanish**. Spanish doc variants use the `.es.md` suffix.
- **Style:** match surrounding code. Stdlib idioms. No new abstractions where a
  function does.
- **Errors:** never silence with bare `except: pass`. Use `hub/log.py` and log with
  context (`exc_info=True` for unexpected failures). Catch specific expected
  exceptions narrowly.
- **Migrations:** schema changes go through versioned migrations
  (`schema_migrations`), idempotent, run once — never a migration that runs on every
  startup.
- **SSE contract:** don't change the SSE event schema without bumping the API
  version; the static HTML has coupled listeners.

---

## 5. Dev & test workflow

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # dev extra = pytest
pytest                            # full suite — keep it green (656+ tests baseline)
mool --help                       # run the CLI
mool daemon start                 # or run the daemon / dashboard locally
```

- Every change keeps the suite green. New behavior ships with tests.
- CI (`publish.yml`) runs on push; do not merge red.
- Quality tools when available: `ruff` (lint), `radon`, `bandit`, `vulture`,
  `pytest-cov`.

---

## 6. Adding a provider

1. `models/<name>.py` — dataclass(es) for the raw session format + map to `UnifiedEvent`.
2. `parsers/<name>_parser.py` — read the session file (JSONL or `sqlite3` in
   `mode=ro`) into the model.
3. `adapters/<name>_adapter.py` — model → `UnifiedEvent`.
4. `watchers/<name>_watcher.py` — incremental tailing (file offset or rowid polling).
5. Register in the `Provider` enum + the parser map (`batch_reporter.py`), add
   discovery paths, and write tests + a fixture.

Nothing in the core pipeline, dashboard, or MCP server should need to change. If it
does, that's a signal to invest in the provider-template enabler first
(`VISION_ROADMAP.md` §5).

---

## 7. Release workflow

1. Bump `version` in `pyproject.toml` (single source; `hub/__init__.py` reads it via
   `importlib.metadata`).
2. Update `CHANGELOG.md` (one entry per release) and, if strategy changed,
   `ROADMAP.md` / `VISION_ROADMAP.md`.
3. Push to `main`, then `gh release create vX.Y.Z`. CI publishes to PyPI and
   auto-syncs the badge/SVG assets.
4. **Rebase local `main` after each release** (CI commits back the asset sync).

---

## 8. Git & collaboration

- **Commit only when the owner asks.** When authorized to commit/push/release/close
  issues: use the owner's own git signature, and **never** add a `Co-Authored-By`
  trailer.
- Never `amend` a commit already pushed to `main` — always a new commit.
- If starting new work on `main`, branch first when appropriate.
- Discuss provider/feature approach in a GitHub issue before a PR (see
  `CONTRIBUTING.md`).

---

## 9. Current priorities (snapshot)

Near-term is Observe-base hygiene — see `VISION_ROADMAP.md` §4 and issues
[#16](https://github.com/fmicalizzi/moolmesh/issues/16) (session lifecycle),
[#17](https://github.com/fmicalizzi/moolmesh/issues/17) (`tool_result` event type),
[#18](https://github.com/fmicalizzi/moolmesh/issues/18) (resumed-session timestamps).
Provider expansion and synchronous-coordination direction are owner decisions
tracked in `VISION_ROADMAP.md` §8.
