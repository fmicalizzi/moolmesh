<p align="center">
  <img src="docs/cli-banner.svg" alt="MoolMesh CLI" width="560">
</p>

# MoolMesh

**The context mesh for autonomous agents.**

Unified observability, telemetry, and inter-agent coordination — running entirely on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Tests: 855 passing](https://img.shields.io/badge/Tests-855%20passing-green.svg)](#development)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-brightgreen.svg)](#)
[![PyPI](https://img.shields.io/pypi/v/moolmesh.svg)](https://pypi.org/project/moolmesh/)
[![Español](https://img.shields.io/badge/Docs-Espa%C3%B1ol-orange.svg)](README.es.md)

---

## Why MoolMesh?

Modern software development isn't human-to-keyboard anymore. It's an ecosystem of AI agents working in parallel — each with its own logs, token counters, and reasoning traces, all locked in separate silos.

When Claude Code gets stuck in a loop, your other agents don't know. When you spend tokens across five providers, you can't see which git commit justified it. When your team uses different AI tools on the same repo, nobody has the full picture.

**MoolMesh congregates what is scattered.** It auto-discovers sessions from every major AI coding agent, normalizes them into a single queryable database, and exposes that state to both humans (via a dashboard) and machines (via MCP).

Read our [Philosophy](PHILOSOPHY.md) to understand the dual axiom behind MoolMesh: **Human-First & Agent-First**.

---

## What You Get

Four views in a single browser tab:

| View | What it shows |
|------|---------------|
| **AI Sessions** | Live event feed from all agents — messages, tool calls, token usage, models |
| **Analytics** | Token consumption by provider, hourly activity, top tools, top projects |
| **Project Pulse** | PR kanban, issues list, milestones, GitHub Projects v2 board |
| **Code Timeline** | Commit feed, author stats, hot files, daily/weekly digest narratives |

Plus a **MCP server** that lets other AI agents query your session data programmatically — enabling agent-to-agent supervision and orchestration.

---

## Quick Start

### Install

**Requires Python 3.11 or later.** MoolMesh uses `match/case`, `tomllib`, and other features that don't exist in earlier versions. Python 3.10 reached end-of-life in October 2026.

```bash
# Recommended — isolated install, global command
pipx install moolmesh

# Or with pip (inside a virtual environment)
pip install moolmesh
```

> **Note:** On modern Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+),
> `pip install` outside a virtual environment is blocked by
> [PEP 668](https://peps.python.org/pep-0668/). Use `pipx` instead,
> or create a venv first:
> ```bash
> python3 -m venv ~/.venvs/moolmesh && source ~/.venvs/moolmesh/bin/activate
> pip install moolmesh
> ```

### Windows

```powershell
# Recommended — handles PATH and dependencies automatically
uv tool install moolmesh

# Alternative
pipx install moolmesh

# Or with pip (may need to add Scripts to PATH)
pip install moolmesh
```

If `mool` is not found after installing with pip, add the Scripts directory to your PATH:

```powershell
pip show -f moolmesh | findstr Scripts
$env:PATH += ";C:\Users\YourUser\AppData\Local\...\Scripts"
```

### Start

```bash
# Foreground (blocks the terminal)
mool dashboard

# Background daemon (recommended — keeps running after you close the terminal)
mool daemon start
# → open http://localhost:5200
```

The daemon runs on all platforms (macOS, Linux, Windows). Use `mool daemon stop` to stop it, `mool daemon status` to check.

That's it. MoolMesh auto-discovers your AI sessions immediately. No configuration needed.

> **Running from source:**
> ```bash
> git clone https://github.com/fmicalizzi/moolmesh.git
> cd moolmesh
>
> # macOS / Linux
> python3 -m venv .venv && source .venv/bin/activate
>
> # Windows (PowerShell)
> python -m venv .venv; .venv\Scripts\Activate.ps1
>
> pip install -e ".[dev]"
> mool dashboard
> ```

---

## Production Install

For system-wide access (run `mool` from any directory):

```bash
# Recommended — isolated venv, global binary
pipx install moolmesh

# Or with pip (requires a venv on modern Python)
pip install moolmesh
```

### systemd service (Linux)

Use `mool dashboard` (foreground) as the entry point — MoolMesh auto-detects systemd and skips the double-fork:

```ini
# ~/.config/systemd/user/moolmesh.service
[Unit]
Description=MoolMesh Dashboard
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/mool daemon start --port 5200
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now moolmesh
systemctl --user status moolmesh
```

> **Note:** `mool daemon start` auto-detects systemd (`$INVOCATION_ID`) and stays in the foreground, so `Type=simple` works correctly. Outside systemd, it double-forks as usual.

---

## Supported Agents

| Provider | Session source | Format |
|----------|---------------|--------|
| **Claude Code** | `~/.claude/projects/` | JSONL per session + subagent logs |
| **Codex (GPT-5)** | `~/.codex/sessions/` + `state_5.sqlite` | Rollout JSONL + SQLite metadata |
| **Qwen CLI** | `~/.qwen/projects/` | JSONL per chat |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | SQLite (session → message → part) |
| **Cursor** | `~/Library/Application Support/Cursor/User/` (macOS) | SQLite (`state.vscdb` key-value: composer bubbles) |

Sessions are auto-discovered on startup. No configuration, no API keys, no cloud services.

---

## Git & GitHub Integration

Register a git repository to unlock Project Pulse and Code Timeline:

```bash
cd /path/to/your/repo
mool repo add                            # Registers the current directory
```

This ingests commit history and starts polling GitHub for issues, PRs, milestones, and Projects v2.

```bash
mool repo list                           # Show registered repos
mool repo remove                         # Unregister current repo
mool repo sync --all                     # Re-ingest full history
```

All `repo` subcommands default to the current directory when no path is given.

### GitHub token

A token is resolved automatically in this order:

1. `gh auth token` (GitHub CLI — recommended)
2. `GITHUB_TOKEN` environment variable
3. `~/.moolmesh/config.toml` → `[github] token = "..."`

For **public repos**, no token is needed — commit history works without GitHub API access.

For **private repos**, a token with `repo` scope is required. The easiest way:

```bash
gh auth login                            # Follow prompts, select repo scope
```

If you don't have the GitHub CLI, set the env var or add it to config:

```toml
# ~/.moolmesh/config.toml
[github]
token = "ghp_xxxxxxxxxxxxxxxxxxxx"
```

Without a valid token, `mool repo add` still works — it ingests local git history, but Project Pulse (issues, PRs, milestones) won't have GitHub data.

---

## Workspace (observe the work, not just the sessions)

A session's directory name is not the project it worked on. The **Workspace axis** attributes every file-touch to the project that *owns the file* — recovering the real multi-project (M:N) picture, and making a folder visible even before or without any agent (VISION §6).

**Attribute agent work you've already captured** — a read-only pass over `events.db` that maps each absolute `file_path` to its owning workspace (identity ladder `git-remote → git-root → path-hash`, so it survives rename/move/clone and works even without git):

```bash
mool workspace backfill                  # populate workspace.db (reads events.db read-only)
mool workspace list                      # workspaces with session / file / fs-touch counts
mool workspace session <session_id>      # which workspaces a session touched
mool workspace sessions <workspace_key>  # which sessions touched a workspace
```

**Observe folders directly** — mark a root and MoolMesh watches it for file changes, so a materials-gathering folder, a non-CLI tool (e.g. a design app whose output is only observable by path), or non-software work lights up with **zero agent or git activity**. Strictly **opt-in**: with no roots marked, nothing is observed.

```bash
mool workspace root add ~/Projects/campaign --max-depth 4 --exclude drafts
mool workspace root list                 # marked roots
mool workspace root remove ~/Projects/campaign
mool workspace touches <workspace_key>   # filesystem touches attributed to a workspace
```

The watcher is pure-stdlib: a bounded recursive scan with an mtime cursor (no watch-per-file), sensible default excludes (VCS internals, dependency dirs, build outputs, sync/cache folders), and a `max_depth` bound. All path-touches land in a **separate `workspace.db`** — the `events.db` hot path and SSE stream are never touched. Set `hide_project_names = true` under `[workspace]` in `~/.moolmesh/config.toml` to mask folder names in the visible surface.

**See the portfolio** — roll every path-touch into a **machine-wide portfolio** that is *signal-agnostic*: a workspace lights up whether the activity came from a session, the filesystem, or git. On top of it, a local **`delivery_candidate`** flags likely-delivered work — surfaced as a *candidate with confidence, never as a fact* (quiescence plus a recorded second signal: a session close, a git commit, or a new artifact at the root; no cloud entity, no LLM).

```bash
mool workspace rollup                    # (re)build the portfolio rollup + run the detector
mool workspace portfolio                 # hot workspaces, by signal
mool workspace delivery                  # delivery candidates (with confidence + the 2nd signal)
```

The dashboard adds a read-on-load **`/portfolio`** view over `workspace.db` (the SSE stream is untouched). Agents can read all of this over MCP via the read-only workspace tools (`list_workspaces`, `get_session_workspaces`, `get_workspace_sessions`, `get_workspace_touches`, `get_portfolio`, `get_workspace_activity`, `get_delivery_candidates`).

---

## MCP Server (Inter-Agent API)

MoolMesh exposes a read-only MCP server over stdio, allowing any MCP-compatible agent to query session data.

The MCP server uses [PEP 723](https://peps.python.org/pep-0723/) inline script metadata for its dependencies (the `mcp` package). This keeps MoolMesh itself zero-dependency while allowing the MCP server to run standalone.

### Quick setup

```bash
mool mcp setup                  # Claude Code (global, user scope)
mool mcp setup claude-desktop   # Claude Desktop (macOS/Linux/Windows)
mool mcp setup cursor           # Cursor IDE
mool mcp setup codex            # Codex (OpenAI CLI)
mool mcp setup qwen             # Qwen CLI
mool mcp setup opencode         # OpenCode
mool mcp setup json             # Print config JSON for any MCP client
```

The command auto-detects your install method (pipx/pip/source), finds the correct Python and server paths, and checks for the `mcp` dependency. If `mcp` is missing it shows the exact command to install it, or use `--install-deps` to install automatically:

```bash
mool mcp setup --install-deps   # Also runs: pipx inject moolmesh mcp
```

Use `--dry-run` to preview changes without modifying any config files.

### Manual configuration

If you prefer to configure manually, here are the two common setups:

**From source** (requires [uv](https://docs.astral.sh/uv/)):

```json
{
  "mcpServers": {
    "moolmesh": {
      "command": "uv",
      "args": ["run", "/path/to/moolmesh/hub/mcp_server.py"]
    }
  }
}
```

**From pipx/pip** (requires `pipx inject moolmesh mcp`):

```json
{
  "mcpServers": {
    "moolmesh": {
      "command": "/path/to/pipx/venvs/moolmesh/bin/python",
      "args": ["/path/to/pipx/venvs/moolmesh/lib/.../hub/mcp_server.py"]
    }
  }
}
```

### Available tools

| Tool | Description |
|------|-------------|
| `get_recent_events` | Latest N events across all providers |
| `get_active_sessions` | Sessions active in the last N hours |
| `get_token_usage` | Token consumption by provider |
| `get_tool_stats` | Top tools used by AI agents |
| `search_events` | Full-text search on event summaries |
| `get_project_activity` | Complete project summary with stats |

Resources: `hub://schema` (database schema), `hub://projects` (project list with stats).

The server opens SQLite in read-only mode (`?mode=ro`). It runs as a separate process (~15-20 MB RAM), independent from the dashboard.

---

## Digest Narratives

Code Timeline generates daily and weekly digests for each registered repo:

| Level | What | When |
|-------|------|------|
| **L1** | Raw SQL stats (commits, PRs, issues, LOC) | Always available |
| **L2** | Structured template with bullet points | Always available |
| **L3** | LLM-generated narrative paragraph | When an LLM provider is configured |

L3 works with any OpenAI-compatible API. Configure in `~/.moolmesh/config.toml`:

```toml
[llm]
provider = "openrouter"
api_url  = "https://openrouter.ai/api/v1"
model    = "google/gemma-4-31b-it:free"
api_key  = "sk-or-v1-..."
```

Supported providers: OpenRouter, OpenAI, Together, Groq, Ollama. If the LLM is unavailable, digests fall back to L2 automatically.

---

## Batch Reports

Generate Markdown analysis reports from the command line:

```bash
# Auto report — writes to ~/.moolmesh/reports/
mool report auto

# Full content (no truncation)
mool report auto --complete

# Filter by project or provider
mool report --project myapp --provider claude --output ./exports
```

---

## CLI Reference

```
mool <command> [options]

Commands:
  dashboard              Start the live monitoring dashboard
  daemon start           Run dashboard as a background service
  daemon stop            Stop the background service
  daemon status          Show daemon PID, uptime, log size
  daemon restart         Restart the background service
  status [--json]        Quick alias for daemon status
  mcp setup [TARGET]     Configure MCP server (claude-code|claude-desktop|cursor|codex|qwen|opencode|json)
  doctor                 Run system diagnostics
  install                Install mool command globally (~/.local/bin)
  report                 Generate batch Markdown analysis reports
  discover [--json]      List all discovered AI agent projects
  repo add [PATH]        Register a git repo (default: current directory)
  repo list              List registered repos with commit counts
  repo remove [PATH]     Unregister a repo (default: current directory)
  repo sync [PATH]       Re-ingest commit history
  query events           Recent events as JSON
  query sessions         Active sessions as JSON
  query tokens           Token usage by provider as JSON
  query tools            Top tools used by agents as JSON
  query search TEXT      Search events by text as JSON
  query project NAME     Project activity summary as JSON

Global options:
  --version              Show version and exit

Dashboard / daemon options:
  --port PORT            Server port (default: 5200)
  --host HOST            Server host (default: localhost)
  --project NAME         Filter to project name
  --providers LIST       Comma-separated: claude,codex,qwen,opencode

Report options:
  --complete             Full-content mode: no truncation
  --output DIR           Output directory
  --provider PROVIDER    Filter by provider
```

### Agent-friendly CLI (`mool query`)

For agents that don't have MCP support, `mool query` exposes the same data as the MCP server via stdout JSON:

```bash
# Get the last 10 events
mool query events -n 10

# Active sessions in the last 2 hours
mool query sessions --hours 2

# Token consumption by provider since a date
mool query tokens --since 2026-06-01

# Top tools used in a project
mool query tools --project moolmesh -n 5

# Search for events mentioning "daemon"
mool query search "daemon" --provider claude

# Full project activity summary
mool query project moolmesh
```

All output is valid JSON — pipe to `jq`, parse with any language, or use from agent subprocess calls. Also: `mool status --json` and `mool discover --json` for machine-parseable output.

### Health endpoint

When the dashboard is running, `GET /health` returns:

```json
{"status": "healthy", "version": "1.4.0", "uptime_seconds": 3600, "events_count": 45231}
```

---

## Architecture

```
hub/
  parsers/         JSONL + SQLite parsers for each provider
  adapters/        Normalize provider entries → unified events
  watchers/        File harvesters: discover → offset → parse → store → SSE
  harvesters/      GitHarvester (120s) + GitHubHarvester (15s/60s)
  integrations/    GitHubClient (REST + GraphQL) + LLM clients
  digests/         L1 Stats → L2 Template → L3 LLM narrative
  correlation/     AI ↔ Git links: Co-Author, issue refs, timestamps
  dashboard/       HTTP server + SSE + 4 HTML pages
  cache/           EventStore (events.db) + GitStore (github.db)
  mcp_server.py    MCP stdio server (read-only, PEP 723 inline deps)
  cli.py           CLI entry point
```

### How data flows

1. **Discovery** scans provider directories for session files
2. **Parsers** read JSONL or query SQLite into typed entries
3. **Adapters** normalize to `UnifiedEvent` with common fields
4. **Watchers** poll incrementally, store atomically in SQLite, push to SSE
5. **Dashboard** serves live feed + analytics via HTTP + Server-Sent Events

All state is persisted in SQLite. Crash-safe, exactly-once semantics via transactional offsets.

---

## Persistence

| Database | Path | Contents |
|----------|------|----------|
| `events.db` | `~/.moolmesh/events.db` | AI session events, file offsets, SSE replay buffer |
| `github.db` | `~/.moolmesh/github.db` | Repos, commits, issues, PRs, milestones, digests |

Both databases are created automatically. Schema migrates on startup.

---

## Reliability

- **Zero-gap SSE** — `id:` fields enable browser reconnection with replay from SQLite
- **Transactional offsets** — events and file positions update in a single transaction
- **Git crash safety** — exceptions caught per-repo, 60s timeout on `git fetch`
- **GitHub ETags** — 304 responses don't consume rate limit
- **Digest fallback** — LLM unavailable → L2 template, no repos → L1 stats
- **OpenCode WAL safety** — read-only SQLite with timeout, never blocks OpenCode writes

---

## Roadmap

MoolMesh started with coding-agent sessions, but the vision is to **observe the work, not just the sessions** — attributing every file-touch to the project that owns it, and eventually seeing a folder directly, before or without an agent (VISION §6).

| Status | Version | Scope |
|--------|---------|-------|
| **Shipped** | v1.6 | Cross-session linking, session metadata, full text export, full-text search, git branch correlation |
| **Shipped** | v1.7 | **Cursor provider (5th)** — Claude, Codex, Qwen, OpenCode, Cursor; universal `mool mcp setup <client>` |
| **Shipped** | v1.8 | Windows daemon, GitHub client hardening (retry/backoff, pagination), MCP pagination & ordering |
| **Shipped** | v1.9 | Observe-base hygiene: honest session lifecycle, `tool_result` classification, timestamp honesty (#16/#17/#18) |
| **Shipped** | v1.10 | **Workspace axis — Phase A:** `path → workspace` resolver, M:N attribution over a separate `workspace.db` (#20) |
| **Shipped** | v1.11 | **Workspace axis — Phase B:** filesystem watcher — observe marked folders directly, no agent/git required; opt-in roots + mtime cursor into `workspace.db` (#21) |
| **Shipped** | v1.12 | **Workspace axis — Phase C:** signal-agnostic portfolio rollup (session + filesystem + git) + `delivery_candidate` (candidate with confidence); read-on-load `/portfolio` dashboard view (#22) |
| **Shipped** | v1.13 | **Portfolio intelligence — Stage 1:** collapse agent-harness folders onto their real project (session-cwd + validated decode) + hierarchical grouping (subdirs/materials nested, config dotfolders de-prioritized); additive `workspace_classification`, grouped `/portfolio` view (#24) |
| **Shipped** | v1.14 | **Portfolio intelligence — Stage 2:** production-over-time — per-project contribution strip (sessions/day by honest ingestion `created_at`), colored by agent, ordered by recency, 4d/week/month toggle; effort = sessions + active days (not duration); image/video deliverable count; inline SVG (zero-dep); leaf-project caret fix (#24) |
| **Shipped** | v1.15 | **Project Intelligence — Unit 1: outcome layer:** merged-PR / closed-issue / open-issue per canonical project in the production view (authoritative delivery from `github.db`, read-only, contributor-agnostic) + explicit `[workspace] filesystem_monitoring` opt-in flag & dashboard indicator (gates only folder monitoring, not the portfolio) (#27) |
| **Shipped** | v1.16 | **Project Intelligence — Unit 2: derived project state:** per canonical project a single state chip — 🟢 activo · 🟡 enfriándose · 🔵 entregado · 🟠 estancado · ⚪ pausado — fusing local activity (session/fs/git over honest clocks, `_parse_ts`) with the GitHub outcome layer; `outcome_measurable` distinguishes gitless (not measurable) from repo-with-0-PRs (measurable); surfaced with its auditable basis, never a bare flag; absorbs the cold-projects view (#25) (#28) |
| **Shipped** | v1.17 | **Project Intelligence — Unit 3: client/org hierarchy:** a third tier client → project → materials over the portfolio; client = git-remote org (case/underscore-insensitive), ladder override → git-org → parent-folder; known owner orgs = client nodes (`client_orgs`, auto-seeded), the owner's own org shown loose (`personal_orgs`), unknown orgs in an externos/referencia drawer, `PRODUCCIONES` as a shared node; effort + outcome + state rolled up to the client (contributor-agnostic); starts invisible until an owner identity is configured; **closes epic #26** (absorbs client attribution #24 Stage 3) (#29) |
| **Shipped** | v1.18 | **Portfolio UX redesign — Part A (layout):** the `/portfolio` view reclaims the wasted horizontal space — production strip fills its column (replaces the left-anchored inline SVG), dense single-line rows (~2–3× more projects/screen), clean names (`github.com/` dropped, 1 line + ellipsis + tooltip), outcome as a first-class tri-state column (merged-PR / real-0 / no-repo-not-measurable), column headers, derived state as a leading scan-anchor dot; presentation-only (resolver/state/outcome #27/#28/#29, SSE and masking untouched), zero new deps (#35) |
| **Planned** | — | **Container split:** decompose shared-workspace nodes (`PRODUCCIONES`) into their distinct child projects/clients — needs resolver re-anchoring (#30). **Team/org visibility** deferred to `v2.x` Org-Scale (outcome data is multi-actor; contributors carried latently today). |
| **Future** | v2.x | Workspace Phase D (cross-machine, opt-in); autonomous agents; org-scale observability |

See [ROADMAP.md](ROADMAP.md) for detailed plans, open questions, and design principles.

---

## Limitations

- **macOS optimal, Linux supported** — macOS uses `kqueue` for instant detection; Linux uses polling (~1s)
- **No authentication** — dashboard binds to localhost. Use a reverse proxy for remote access
- **Single-user design** — not intended for multi-user or server deployment
- **Python 3.11+** — uses `tomllib` from stdlib
- **GitHub Projects v2 only** — classic Projects (v1) not supported
- **Cursor caveats** — Cursor stores no per-message timestamps locally (MoolMesh approximates them from composer metadata) and its on-disk schema is reverse-engineered, so a Cursor update may temporarily reduce ingestion until the parser is adjusted

---

## Development

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=hub
```

855 tests. Zero external dependencies. Python stdlib + SQLite.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

[MIT](LICENSE) — Your telemetry is yours.
