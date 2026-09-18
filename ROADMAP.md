# MoolMesh Roadmap

Last updated: September 2026 — v1.14.0

---

## Delivered

### v1.0–v1.4 — Foundation

- 4 providers: Claude Code, Codex (GPT-5), Qwen CLI, OpenCode
- Live dashboard with SSE streaming, analytics, Project Pulse, Code Timeline
- MCP server (read-only, stdio) with 6 query tools
- Agent-friendly CLI (`mool query`) for agents without MCP
- Daemon mode, systemd compatibility, auto port fallback
- Digest engine: L1 stats → L2 template → L3 LLM narrative
- Git/GitHub integration: commits, PRs, issues, milestones, Projects v2
- PyPI package (`pip install moolmesh`)

### v1.5 — Session Intelligence

- Session metadata table with titles, models, git branches, costs
- Full text storage in separate `event_content` table
- Session export (`mool export`) — markdown and JSON transcripts
- Full-text search across complete session content
- Git branch correlation — link sessions to branches
- `mool sessions` CLI with filters by provider, branch, time window

### v1.6 — Cross-Session Linking

- Explicit linking via `mool link` between any two sessions
- Temporal detection via `mool detect-links` — finds sessions sharing files within a time window
- `mool chain` / `mool query chain` — view session chains across providers
- `get_session_chain` MCP tool — agents can query related sessions
- `get_session_detail` enriched with `linked_sessions` automatically
- Phase 3 (semantic similarity via LLM) remains planned for a future version

### v1.7 — Cursor, Windows & universal MCP setup

- **Cursor provider (5th)** — auto-discovers and ingests Cursor agent/composer conversations from `state.vscdb` (`cursorDiskKV`) by `rowid`, attributed to projects via workspace mapping. `Provider.CURSOR` + full quartet.
- **`mool mcp setup <client>`** — universal MCP client configuration (Cursor, Codex, Qwen, OpenCode) in each client's native format.
- Windows path handling, daemon graceful degradation, PID/encoding fixes.

### v1.8 — Windows daemon, GitHub hardening & MCP pagination

- **Windows daemon support** — `mool daemon start` on all platforms; `uv tool install moolmesh` as recommended Windows installer.
- **GitHub client resilience** — retry/backoff, `Link`-header pagination for issues, robust handling of truncated / `IncompleteRead` responses.
- **MCP pagination & ordering** — `offset` and `order` on `get_session_events` / `get_recent_events` / `search_events`.
- Codex watcher crash-resilience (None fields, list payloads, ghost `[user input]` events).

### v1.9 — Observe-base hygiene

Hardened session-lifecycle fidelity before climbing further (VISION §4):

- **#16** — honest session lifecycle: `is_active` now means "no end observed", flipped to `0` only on a terminal signal observed in the session file (Claude `/exit`), never inferred from recency; `first_event_at` backfills instead of freezing empty. New additive `ended_at` / `ended_reason` columns record why/when a session ended.
- **#17** — `tool_result` classified distinctly from a user message: Claude tool outputs riding inside `role="user"` entries no longer count as human input (prerequisite for the Workspace resolver).
- **#18** — timestamp honesty on resumed sessions: ingest-based `last_activity_at` (`MAX(events.created_at)`) and per-event `created_at`, distinct from the original message time.

### v1.10 — Workspace axis Phase A

First step of the Workspace axis (VISION §6): recover project-first, multi-project attribution over data MoolMesh already persists.

- **#20** — `path → workspace` resolver: maps each absolute `events.file_path` to the project that owns the file (M:N), via an identity ladder `git-remote → git-root → path-hash` that survives rename/move/clone and exists even without git (`.git/config` parsed by hand — no `git` subprocess). Attribution lives in a **separate `workspace.db`**; `backfill_from_events` reads `events.db` read-only and skips non-absolute rows (Bash command strings). New `mool workspace {backfill,list,session,sessions}` CLI and 3 read-only MCP tools (`get_session_workspaces` / `get_workspace_sessions` / `list_workspaces`, `[]` when the DB is absent). Additive: the existing `project` field, the MCP contract, `events.project`, and `linker.py` are untouched. This is the *recovery half* — correct attribution of agent work — not the full portfolio (that lands with the Phase B watcher).

### v1.11 — Workspace axis Phase B

Second step of the Workspace axis (VISION §6): observe the **work**, not just the sessions — a folder becomes visible even with zero agent/git activity.

- **#21** — filesystem watcher: a pure-stdlib, standalone watcher (never touches `EventStore` / the SSE hot path) that observes **opt-in marked roots** — bounded recursive scan + a **per-root mtime cursor** (no watch-per-file, so no FD ceiling; cursor anchored to `scan_start − 1s` so mid-scan writes are never dropped). **Containment = correctness:** built-in default excludes (VCS internals, dependency dirs, build outputs, OS/cloud sync + caches) pruned in-place, plus per-root excludes and a `max_depth` bound; symlinks not followed. Emits `path-touch` rows resolved to workspaces via the Phase A resolver (#20), into additive `path_touches` / `fs_cursors` tables in the **separate `workspace.db`** (`CREATE TABLE IF NOT EXISTS` — no migration; **zero new writes to `events.db`**). New `mool workspace root {add,list,remove}` + `mool workspace touches` CLI, MCP `get_workspace_touches` (and a filesystem-`touches` count on `list_workspaces` / `get_session_workspaces`), and a `hide_project_names` privacy flag. Additive: the existing `project` field, the MCP contract, `linker.py`, and the #20 resolver are untouched (Phase B *consumes* the resolver).

### v1.12 — Workspace axis Phase C

Final step of the Workspace axis (VISION §6): roll the attributed path-touches into a **machine-wide portfolio** and add a **local delivery signal** — the axis is now complete (A + B + C); only the deferred cross-machine Phase D (v2.x) remains.

- **#22** — portfolio rollup + `delivery_candidate`:
  - **Signal-agnostic portfolio rollup** — a materialized projection (`workspace_rollup` in the separate `workspace.db`, keyed `(workspace, day)`, additive `CREATE TABLE IF NOT EXISTS`) where a node lights up whether the activity came from a **session**, the **filesystem**, or **git** — a real UNION over three differently-shaped sources (git resolved via the Phase A ladder so a repo root and a deep path collide on one node), folded in at build time so the read stays single-table. Keyed `INSERT OR REPLACE` (never wipe-and-rebuild — the rollup is the only durable per-day filesystem record); per-signal counts kept separate (incommensurable units), each node reports which `sources` lit it.
  - **`delivery_candidate`** — a **local, structural** guess of likely delivery, surfaced as a **candidate with confidence, never a bare "done"**. Quiescence is a **precondition only**, measured against the **real clocks** (`path_touches.last_seen`, git commit times, session `ended_at`/`last_event_at` from #16 — read-only, never the backfill-pinned `path_attributions.first_seen`); a row is written only when a **second co-occurring signal closed the burst** and that signal is **recorded per row** (auditable): `session_close`, `git_commit`, or `root_artifact`. **No LLM.** Three clock formats normalized to aware UTC before any comparison.
  - **Dashboard** — the axis's first frontend surface: a **read-on-load** portfolio view (`/portfolio` + `/api/workspace/{portfolio,delivery}`) over `workspace.db`; the **SSE schema is untouched** (no API-version bump), hot path unaffected. New `mool workspace {rollup,portfolio,delivery}` CLI and MCP `get_portfolio` / `get_workspace_activity` / `get_delivery_candidates` (all masked by `hide_project_names`). Additive: the `project` field, the MCP session contract, `linker.py`, and `events.db` are untouched.
  - **Known limitation** — the rollup's per-day bucketing keeps git days in local time vs. session/filesystem in UTC (±1-day edge near local midnight); quiescence is unaffected. Follow-up in [#23](https://github.com/fmicalizzi/moolmesh/issues/23).

### v1.13 — Portfolio intelligence (Stage 1: grouping)

First stage of the portfolio-intelligence epic ([#24](https://github.com/fmicalizzi/moolmesh/issues/24)): the `/portfolio` view now identifies real **projects** instead of listing ~376 flat folders (on a real machine ~49% are agent-harness folders).

- **#24 (Stage 1)** — collapse harness noise + hierarchical grouping. A new **read-layer classifier** (`hub/cache/portfolio_classifier.py`) maps every workspace into a 4-category taxonomy, materialized into an additive, **fully-rebuildable** `workspace_classification` table (contrast the durable `workspace_rollup`):
  - **A. Harness → collapse** onto the real project — primary via the session `cwd` embedded through the scratchpad's session uuid (`events.db`, read-only, no decode); fallbacks are exact encode-match against known real dirs then a filesystem-validated decode (the naive `replace('-','/')` is lossy — Claude encodes `/`, `_` and `-` all to `-` — so `coep-services` is never split into `coep/services`).
  - **B. deep subdirs** + **C. materials/reports/exports** nest under their nearest real ancestor; **D. config dotfolders** de-prioritized (D1 nested) or orphaned (D2 home-level dotfolders + degenerate/system roots → a collapsed "sin clasificar" section).
  - **Surface** — new `get_portfolio_grouped` MCP tool + store method (harness activity folded **at read time**; the rollup is never re-keyed), a `collapsed_harness` count per project (nothing deleted — grouped), `hide_project_names` masking recurses into labels + children. Hierarchical **read-on-load** `/portfolio` (SSE untouched). New `mool workspace classify` / `portfolio --grouped`.
  - **Additive** — resolver (#20), watcher (#21), `events.db`, the `project` field, `delivery_candidate` and SSE are untouched. **No charts (Stage 2) or client attribution (Stage 3)** — those stay in epic #24.

### v1.14 — Portfolio intelligence (Stage 2: production-over-time)

Second stage of the portfolio-intelligence epic ([#24](https://github.com/fmicalizzi/moolmesh/issues/24)): the `/portfolio` view now leads with an **honest production chart** over the Stage-1 grouping.

- **#24 (Stage 2)** — production-over-time. One **contribution strip per canonical project** (a cell per day of the window, shaded by session count that day), built on a deliberately honest metric:
  - **Effort, not duration** — each session is a unit of work (session count + active days); duration is never used (resumed sessions span months; formats differ across providers).
  - **Ingestion dating** — sessions dated by `MAX(events.created_at)`, bucketed to the local day (honest on resumed sessions, #18; timezone-consistent with #23). `first/last_event_at` are not used.
  - **Canonical aggregation** — rolled up over `workspace_classification.project_key` (Stage 1), so harness/scratchpad folds into its real project. **Multi-provider**: colored by the day's dominant agent, ordered by recency. **Window toggle** 4d / week / month (read-on-load).
  - **Deliverables** = image/video count from `path_touches` (the watcher) — 0 until a root is marked, surfaced honestly, never a silent zero.
  - **Surface** — new `get_portfolio_production(days)` MCP tool + `/api/workspace/portfolio/production` route; charts are hand-rolled **inline SVG** (zero-dep); `hide_project_names` masks labels. Also **fixes** the Stage-1 leaf-project caret (no expand affordance on projects without children).
  - **Additive** — resolver (#20), watcher (#21), `events.db`, the `project` field, `delivery_candidate`, SSE and the Stage-1 classification are untouched (consumed only). **No client attribution (Stage 3)** — that stays in epic #24; a "cold projects" view is a known follow-up (**#25**).

---

## Planned

> Strategy lives in [`VISION_ROADMAP.md`](VISION_ROADMAP.md) (español: [`VISION_ROADMAP.es.md`](VISION_ROADMAP.es.md)); this is the tactical log. Version numbers below are indicative, not committed.

### Workspace axis (new direction — VISION §6)

Recover the project-first model of MoolMesh's root and add **direct folder observation**, so work is legible beyond agent sessions — materials-gathering folders, non-CLI agents, non-software projects. A multi-release arc:

- **Phase A — `path → workspace` resolver** over already-persisted `file_path` / `cwd`: correct multi-project attribution of agent work (M:N). Additive; does not touch `linker.py`. **Delivered in `v1.10.0`; see Delivered above.**
- **Phase B — filesystem watcher** with marked roots + bounded scan + excludes; its own `workspace.db` (protects the `events.db` hot path / SSE). **Delivered in `v1.11.0`; see Delivered above.**
- **Phase C — portfolio rollup** + `delivery_candidate` (surfaced as candidate-with-confidence, never as fact). **Delivered in `v1.12.0`; see Delivered above.**
- **Phase D — cross-machine aggregation** (opt-in, Wakapi-style split; deferred).

Indicative release mapping (features = minor bumps; each phase independently shippable per its issue's Definition of Done): Phase A → `v1.10.0` ([#20](https://github.com/fmicalizzi/moolmesh/issues/20), **delivered**; see Delivered above), Phase B → `v1.11.0` ([#21](https://github.com/fmicalizzi/moolmesh/issues/21), **delivered**; see Delivered above), Phase C → `v1.12.0` ([#22](https://github.com/fmicalizzi/moolmesh/issues/22), **delivered**; see Delivered above), Phase D → `v2.x`. Preceded by the Observe-hygiene line (delivered in `v1.9.0`; see Delivered above). Standard flow: AGENTS.md §7 + CI `preflight`. Epic: [#19](https://github.com/fmicalizzi/moolmesh/issues/19).

### Portfolio intelligence (epic #24)

- **Stage 3 — client attribution** ([#24](https://github.com/fmicalizzi/moolmesh/issues/24)): group/re-axis the portfolio by client (git-remote owner → parent-folder convention → manual override). Stages 1 (grouping, `v1.13.0`) and 2 (production-over-time, `v1.14.0`) are delivered — see Delivered above.
- **Cold-projects view** ([#25](https://github.com/fmicalizzi/moolmesh/issues/25)): surface projects with no activity in the selected window (the production strip currently shows only projects active in-window). A known follow-up to Stage 2.

### Provider pipeline (Breadth — VISION §5)

Low-effort first: **Aider**, **Pi**, **Goose**; autonomous agents (Hermes, Odysseus) after schema confirmation; Copilot CLI once its format is confirmed. Enabler first: a provider template + auto-detection so a provider is only its quartet. The Workspace filesystem floor (Phase B) gives never-seen agents baseline output visibility for free — lowering the cost of every future provider.

---

## Future

### v2.0 — Autonomous Agent Support

Interactive coding agents (Claude, Codex) have clear session boundaries: a conversation starts and ends. Autonomous agents (Hermes, Odyssey, Goose) break this model:

- Sessions may last hours or days
- No human-in-the-loop — the "conversation" is internal decisions and tool calls
- The concept of "session" may map to a task, a run, or a pipeline stage

**Open questions:**
- What constitutes a "session" for an always-on agent?
- Where do autonomous agents store their execution logs?
- What is the right granularity for event capture?

**Technical approach**: the existing `UnifiedEvent` model already supports the event types these agents would generate (tool_use, tool_result, thinking, summary). The challenge is parser-level: understanding each agent's log format and mapping it to our model.

| Agent | Log format | Complexity |
|-------|-----------|-----------|
| **Hermes** | TBD — needs investigation | Medium-High |
| **Odyssey** | TBD | Medium-High |
| **Goose** | TBD | Medium |

### v2.x — Organization-Scale Observability

> Converges with **Workspace Phase D** (VISION §6): the cross-machine, multi-user horizon. Local capture stays untouched; aggregation is an opt-in, self-hosted tier keyed by `git-remote` identity.

- Cross-repo model usage analytics
- Team-level dashboards: who is using what AI, where, and at what cost
- Multi-user support with authentication
- Remote dashboard access (reverse proxy + auth layer)
- Webhook/alert integration: notify when token spend exceeds thresholds

---

## Design Principles (what we won't do)

- **No cloud dependency** — MoolMesh runs entirely on your machine. Your telemetry stays yours.
- **No external dependencies** — Python 3.11+ stdlib + SQLite. No pip install surprises.
- **No breaking the hot path** — new features (full text, search, linking) use separate tables. The SSE stream and dashboard stay fast.
- **No provider lock-in** — every provider is a pluggable set of 4 files. Adding one doesn't change the others.

---

## Contributing

Want to add a provider or feature? Open an issue to discuss the approach before submitting a PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.
