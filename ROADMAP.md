# MoolMesh Roadmap

Last updated: June 2026

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

---

## In Progress

### v1.6 — Cross-Session Linking (#6)

Link related sessions across providers and time. Three phases:

| Phase | What | Confidence | Status |
|-------|------|-----------|--------|
| Explicit links | Record when an agent queries another session via MCP | High | Ready to implement |
| Temporal + file proximity | SQL-based detection: same project, same files, close in time | Medium | Ready to implement |
| Semantic similarity | LLM-based comparison of session summaries (uses existing digest infra) | Lower | Planned |

New table: `session_links`. New MCP tool: `get_session_chain(session_id)`.

Small, additive change — no modifications to existing tables or hot paths.

---

## Planned

### v1.7 — New Providers (first wave)

Expand beyond the original four. Priority based on local session file availability and community demand.

| Provider | Session format | Effort | Notes |
|----------|---------------|--------|-------|
| **Aider** | `~/.aider/history/` text + SQLite metadata | Low | Well-documented format, easiest first provider to add |
| **GitHub Copilot CLI** | Local logs | Medium | Format needs investigation |
| **Pi** | TBD | Medium | Depends on whether sessions are stored locally |

Each provider follows the established pattern: model → parser → adapter → watcher (~300-500 LOC total). Adding a provider does not require changes to the core pipeline, dashboard, or MCP server.

**Goal**: validate that the provider pattern scales cleanly, document it for community contributions.

### v1.8 — Provider Template & Contributor Guide

- `hub/providers/template/` — skeleton model, parser, adapter, watcher with inline docs
- Automated provider test scaffolding
- `CONTRIBUTING.md` section: "Adding a New Provider"
- Provider auto-detection: scan filesystem for known session formats

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
