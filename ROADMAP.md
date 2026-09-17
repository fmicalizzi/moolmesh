# MoolMesh Roadmap

Last updated: September 2026 — v1.8.5

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

---

## Planned

> Strategy lives in [`VISION_ROADMAP.md`](VISION_ROADMAP.md) (español: [`VISION_ROADMAP.es.md`](VISION_ROADMAP.es.md)); this is the tactical log. Version numbers below are indicative, not committed.

### Observe-base hygiene (highest priority — VISION §4)

Harden session-lifecycle fidelity before climbing further:

- **#16** — honest session lifecycle (`starting → active → idle → closed`); today `is_active` is set once and never returns to `0`.
- **#17** — dedicated `tool_result` event type, distinct from a user message. Also a prerequisite for the Workspace resolver below.
- **#18** — timestamp honesty on resumed sessions (ingest / last-activity distinct from original event time).

### Workspace axis (new direction — VISION §6)

Recover the project-first model of MoolMesh's root and add **direct folder observation**, so work is legible beyond agent sessions — materials-gathering folders, non-CLI agents, non-software projects. A multi-release arc:

- **Phase A — `path → workspace` resolver** over already-persisted `file_path` / `cwd`: correct multi-project attribution of agent work (M:N). Additive; does not touch `linker.py`.
- **Phase B — filesystem watcher** with marked roots + bounded scan + excludes; its own `workspace.db` (protects the `events.db` hot path / SSE).
- **Phase C — portfolio rollup** + `delivery_candidate` (surfaced as candidate-with-confidence, never as fact).
- **Phase D — cross-machine aggregation** (opt-in, Wakapi-style split; deferred).

Indicative release mapping (features = minor bumps; each phase independently shippable per its issue's Definition of Done): Phase A → `v1.10.0` ([#20](https://github.com/fmicalizzi/moolmesh/issues/20)), Phase B → `v1.11.0` ([#21](https://github.com/fmicalizzi/moolmesh/issues/21)), Phase C → `v1.12.0` ([#22](https://github.com/fmicalizzi/moolmesh/issues/22)), Phase D → `v2.x`. Preceded by the Observe-hygiene line (`v1.9.x`). Standard flow: AGENTS.md §7 + CI `preflight`. Epic: [#19](https://github.com/fmicalizzi/moolmesh/issues/19).

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
