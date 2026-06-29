# Changelog

All notable changes to MoolMesh are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.7.0] — 2026-06-29

### Added
- **Cursor provider** — MoolMesh now auto-discovers and ingests Cursor agent/composer conversations. Bubbles are read incrementally from Cursor's global `state.vscdb` (`cursorDiskKV`) by `rowid`, and attributed to projects via each workspace's `composer.composerData` + `workspace.json` mapping. Captures user/assistant messages, per-bubble `tokenCount`, tool/file hints, and composer line/file statistics in session metadata.
- New per-provider quartet: `hub/models/cursor.py`, `hub/parsers/cursor_parser.py`, `hub/adapters/cursor_adapter.py`, `hub/watchers/cursor_watcher.py`; `Provider.CURSOR`; `discover_cursor()`; dashboard provider styling.
- Multi-platform Cursor base path (macOS / Linux / Windows), read-only DB access (`?mode=ro`, WAL-safe), and graceful degradation on unrecognized schema.
- 18 new tests across models, parser, adapter, watcher, discovery, and server registration.

### Notes
- Cursor stores no per-message timestamps locally; MoolMesh approximates them from composer metadata. Token counts come from Cursor's own `tokenCount` and may not split input/output.

---

## [1.6.0] — 2026-06-26

### Added
- **Cross-session linking** — `session_links` table tracks relationships between sessions across providers with type (`continues`, `references`, `reviews`, `temporal`) and confidence score. (#6)
- **`mool link`** — explicitly link two sessions by ID with `--type` flag.
- **`mool chain`** — view the chain of linked sessions (predecessors and successors). `--json` for agents.
- **`mool detect-links`** — Phase 2 temporal detection: finds sessions in the same project that share files within a configurable time window. `--auto` stores detected links automatically.
- **`mool query chain`** — agent-friendly JSON output of session chains.
- **`get_session_chain` MCP tool** — read-only query of linked sessions for agents connected via MCP.
- **`get_session_detail` enriched** — automatically includes `linked_sessions` when links exist, so agents see related sessions without a second call.
- 26 new tests for link creation, dedup, chain queries, temporal detection, and MCP integration.

### Changed
- MCP server stays fully read-only (`?mode=ro`) — links are created via CLI, read via both CLI and MCP.

---

## [1.5.0] — 2026-06-26

### Added
- **Full text storage** — new `event_content` table stores complete message text alongside the truncated 120-char summary. The events table stays lean for SSE/dashboard; full text is queried on demand.
- **Session export** (`mool export`) — generates complete transcripts of any AI session. Supports `--format markdown` (default) and `--format json`, with optional `-o` file output.
- **Full-text search** — `mool query search "text" --full` searches across complete session content instead of truncated summaries. Also available as MCP tool `search_session_content`.
- **`get_session_events` MCP tool** — retrieves all events from a session with optional `include_full_text` parameter.
- **Session metadata table** — `sessions` table captures titles, models, git branches, costs, and initial prompts. Automatic backfill from existing events on first startup. (#4)
- **`mool sessions` CLI** — lists sessions with human-readable output. Filters: `--hours`, `--provider`, `--branch`, `--json`. (#7)
- **Git branch correlation** — `--branch` filter on `mool sessions` + `get_branch_sessions` MCP tool. Claude's `gitBranch` field now stored in sessions table. (#9)
- **`to_session_meta()` on all 4 adapters** — Claude, Codex, OpenCode, Qwen adapters extract provider-specific metadata (titles, costs, git branches, CLI versions) into the sessions table.
- 22 new tests for full text storage, export, and search. 17 new tests for session metadata.

### Fixed
- **Parser truncation removed** — tool output previously cut at 500 chars in opencode_parser, qwen_parser, and codex_adapter is now stored in full.
- **OpenCode parser ignoring session title** — `_row_to_entry()` now uses the `session_title` column that was already SELECTed but never mapped.

### Changed
- `UnifiedEvent` gains `full_text: str | None` field. `full_text` is stripped from SSE broadcast to keep the dashboard stream lightweight.
- `store_with_offset()`, `store()`, and `store_batch()` insert into `event_content` when `full_text` is present, within the same transaction.

---

## [1.4.3] — 2026-06-23

### Fixed
- **systemd compatibility** — `mool daemon start` auto-detects process supervisors (`$INVOCATION_ID`, `$NOTIFY_SOCKET`) and stays in foreground instead of double-forking. `Type=simple` systemd services now work correctly. (#1)
- **Global install docs** — added "Production Install" section with `pipx install moolmesh` as recommended method and systemd unit file example. (#2)

---

## [1.4.0] — 2026-06-22

### Added (DX & Operations)
- **Daemon mode** (`mool daemon start|stop|status|restart`) — run the dashboard as a background service. Double-fork Unix daemon with PID file (`~/.moolmesh/moolmesh.pid`), log redirection (`~/.moolmesh/daemon.log`), and SIGTERM graceful shutdown.
- **Global CLI install** (`mool install`) — places a shell wrapper in `~/.local/bin/mool` pointing to the venv Python. Works without activating the virtualenv.
- **System diagnostics** (`mool doctor`) — checks Python version, database health, registered repos, GitHub token, port availability, disk space, and daemon status.
- **`mool status`** — quick alias for `mool daemon status` showing PID, uptime, and log size.
- **`mool --version`** — prints `moolmesh X.Y.Z`.
- **`mool repo add` defaults to current directory** — `path` argument is now optional across `add`, `remove`, and `sync`.
- **`GET /health` endpoint** — returns JSON with status, version, uptime, and event count.
- **ANSI colors in CLI** (`hub/colors.py`) — TTY-aware colored output with `NO_COLOR` support.
- **Agent-friendly CLI** (`mool query`) — 6 JSON-output subcommands (`events`, `sessions`, `tokens`, `tools`, `search`, `project`) for agents without MCP. Reuses the same pure functions as the MCP server. Also `--json` flag on `status` and `discover`.
- **PyPI publishing** — trusted publisher via GitHub Actions OIDC. `pip install moolmesh` works.

### Added
- **MCP stdio server** (`hub/mcp_server.py`) — read-only FastMCP server exposing AI session data via stdio transport. 2 resources (`hub://schema`, `hub://projects`) + 6 tools (`get_recent_events`, `get_active_sessions`, `get_token_usage`, `get_tool_stats`, `search_events`, `get_project_activity`). PEP 723 inline deps — runs with `uv run`, no project dependency on `mcp`.
- **Pure function architecture** — all query logic in testable `_get_xxx(db_path, ...)` functions, MCP decorators are thin delegators. Import guard allows pytest to test without mcp SDK installed.
- **Read-only SQLite** — connections use `?mode=ro` URI. Impossible for the MCP server to modify data.
- **29 tests** in `test_mcp_server.py`: fixture DB with 50 events across 3 providers, read-only enforcement, all 6 tools + 2 resources tested with real data, stdio JSON-RPC end-to-end, integration tests against real `~/.moolmesh/events.db`.

### Added
- **OpenCode live watcher** (`hub/watchers/opencode_watcher.py`) — SQLite polling watcher using `rowid` as incremental cursor. Polls `~/.local/share/opencode/opencode.db` every 2s. Extends `BaseHarvester` — same atomic `store_with_offset` pattern as JSONL watchers. ~17 KB/cycle in normal use, batches of 500 rows max (~3.3 MB) during backfill.
- **`parse_incremental()` in OpenCodeParser** — `WHERE pt.rowid > ? ORDER BY pt.rowid ASC LIMIT 500` query with WAL-safe reads. Returns `(entries, new_rowid)` for cursor-based polling.
- **OpenCode in dashboard UI** — full integration across all dashboard components: Provider Tokens chart, Activity (last 60s) timeline, Live Feed filter button, status dot, project cards. Color: magenta (`#d2a8ff`).
- **`get_project_summary()` in EventStore** — SQL-aggregated project list (`GROUP BY provider, project`) queried directly from SQLite. Replaces the in-memory `SessionTracker.get_projects()` for `/api/sessions`, which only saw the last 500 events and missed older Claude projects after OpenCode backfill.
- 7 tests nuevos en `test_opencode_watcher.py`: discover, provider name, parse from zero, incremental no-new-data, incremental picks up new, SSE buffer integration.

### Added
- **Multi-provider LLM** — soporte para OpenRouter, OpenAI, Together, Groq como providers L3. Cualquier API OpenAI-compatible (`/v1/chat/completions`) funciona.
- **Sección `[llm]` en `config.toml`** — reemplaza `[ollama]` como sección canónica. Campos: `provider`, `api_url`, `model`, `api_key`. Backward compatible: configs existentes con `[ollama]` siguen funcionando sin cambios (migración lazy).
- **`OpenAICompatClient`** (`hub/integrations/openai_compat_client.py`) — client genérico para APIs OpenAI-compatible. Mismo contrato que `OllamaCloudClient` (`.chat()`, `.is_available()`). Parsea formato `choices[0].message.content`.
- **`create_llm_client()` factory** (`hub/integrations/__init__.py`) — instancia el client correcto según provider. `"ollama"` → `OllamaCloudClient`, cualquier otro → `OpenAICompatClient`. Retorna `None` si no hay API key.
- **`_resolve_llm_key()` en `DashboardServer`** — cascada de resolución de API key: config → `LLM_API_KEY` env → env específico del provider (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`, `OLLAMA_API_KEY`).
- 18 tests nuevos: 10 en `test_openai_compat_client.py`, 4 en `test_llm_factory.py`, 4 en `test_config.py` (backward compat `[ollama]` → `[llm]`).

### Added
- **`--complete` CLI flag** — full-content mode: no truncation, all messages, all operations, all assistant response parts. Threaded through CLI → `batch_reporter` → analyzers.
- **QA analyzer multi-answer fix** — `current_answer: UnifiedMessage | None` changed to `current_answers: list[UnifiedMessage]`. Was losing all but the last assistant response per turn. New `answer_all` field captures 100% of assistant content (was 9.7%).
- **Analyzer truncation removed in complete mode** — `user_messages` (was 2000 chars → full), `file_ops` (was 30 hot files → all), `efficiency` (was top 20 sessions → all), `qa` (was 30 tools/15 files → all).
- **Memory optimization for `_all` report** — runs sequentially after per-project reports (was parallel), `_opencode_cache.clear()` after each phase, workers reduced from 6 to 4. Prevents doubling peak memory.
- **Report Examples section in README** — CLI examples for auto, project-specific, provider-specific, and complete exports. Compact vs Complete comparison table.

### Added
- **`mool repo sync`** — re-ingests commit history for an already-registered repo without modifying config. Accepts `--days N` or `--all` flags. Dedup via `UNIQUE(repo_id, sha)` — safe to re-run.
- **`get_commit_days()` in GitStore** — `SELECT DISTINCT DATE(timestamp)` for mini-calendar chips based on real git activity, not only cached digest dates. Returns up to 30 days.
- **Loading spinners on navigation** — all sections (commits, authors, hot-files, digest) reset to spinner state before fetching when navigating dates. Eliminates stale data visible for 1–3 seconds.
- **`ollama_status` field in digest response** — DigestEngine now returns `ollama_status` in every digest dict: `not_configured`, `skipped_historical`, `attempted`, `failed`, or `success`. Frontend displays this as a tooltip on the level badge.
- **`delete_cached()` in GitStore** — deletes all digest rows for `(repo_id, date, period)` across all levels. Called by DigestEngine when `force_refresh=True` before saving the new result.
- **Regenerar spinner** — pressing "Regenerar" or "Regenerar Semanal" immediately shows a spinner in the digest text area, replacing stale content during the Ollama call (30–120s).
- **Weekly level badge and technical summary** — `weeklyLevelBadge` and `weeklyTechnical` elements added to the weekly section. Frontend now renders level badge and technical summary for weekly digests, matching daily section parity.

### Fixed
- **Active Projects showing stale data** — `/api/sessions` used `SessionTracker.get_projects()` which only tracked the last 500 events loaded at startup. After OpenCode backfill (356 events), only 144 Claude events fit, hiding most Claude projects. Fixed: endpoint now uses `EventStore.get_project_summary()` which queries SQLite directly with `GROUP BY provider, project` — shows all 106 projects with accurate stats.
- **Dashboard missing OpenCode across all UI components** — Provider Tokens, Activity timeline, Live Feed filter, status dots, and project cards only knew about Claude/Codex/Qwen. Added OpenCode with magenta color (`#d2a8ff`) to all CSS classes, HTML elements, and JS rendering functions.
- **Sprint A: `mool repo sync` missing** — Sprint A items (`versioned migrations`, `git log --all`, `repo add --days/--all`, `_truncate`, `USER_AGENT`, branch extraction fix, `list_cached_digests` aggregate, startup false migration report, `github_handle` double source of truth) are present in `[Unreleased]` from a prior update. No change needed here.
- **Regenerar not updating text** — handler called `renderDigest()` (stats-only) after force fetch, leaving the spinner in place. Fixed: handler now calls `loadDailyDigest()` / `loadWeeklyDigest()` after the force fetch, which fetches from cache and renders text, badge, technical summary, and stats completely.
- **Old L3 cache blocking force-refresh** — `_load_cached()` prioritizes L3 > L2. When force-refresh failed Ollama and saved a new L2, the old L3 row persisted and `_load_cached()` returned it. Fixed: `get_daily_digest()` and `get_weekly_digest()` call `delete_cached()` before saving when `force_refresh=True`.
- **Ollama timeout 30s** — `OllamaCloudClient` used `timeout=30`. Weekly digests (1200 tokens) routinely exceeded 30s, causing silent `TimeoutError` → L2 fallback. Increased to `timeout=120`.
- **"Se pierden los títulos" — h2 visually indistinguishable** — `.digest-text h2` had `font-size: 14px` (1px above body 13px) and inherited `white-space: pre-wrap` from parent, causing whitespace artifacts. Fixed: `font-size: 16px`, `font-weight: 700`, `white-space: normal`, `margin-top: 16px`, `:first-child { margin-top: 0 }`. Added `.digest-text h3` rule.
- **Collapsed digest too small** — `max-height: 120px` with 40px gradient showed only ~3 lines. Increased to `max-height: 320px` (~12 lines visible before fade).
- **Daily title not resetting to "Digest del Día"** — `renderDigest()` skipped title update when `selectedDate` was null (today view). Fixed: title now updates unconditionally with `selectedDate ? formatDisplayDate(selectedDate) : 'Digest del Día'`.
- **Daily collapse state desync** — `loadAllData()` removed `collapsed` class but never re-added it after content loaded. Fixed: `digestExpanded` reset to false at start of `loadAllData()`; `renderDigest()` restores class and button text from `digestExpanded` state.
- **Weekly stats not reset on navigation** — `loadAllData()` reset daily stats (`dailyCommits`, etc.) but not weekly equivalents. Fixed: `weeklyCommits`, `weeklyAdded`, `weeklyRemoved`, `weeklyPRs`, `weeklyIssues` now reset to `…` on each navigation.
- **"Digest del Hoy" heading bug** — when `selectedDate` is null, the L3 heading prefix used `'Hoy'` literal. Fixed to use `formatDisplayDate(getCurrentDate())` which always returns a real date.
- **Commits feed showing today's commits on historical dates** — `loadCommits()` now sends `since`/`until` derived from the selected date. Feed shows commits for the navigated day.
- **Authors/hot-files using current week on historical dates** — `loadAuthors()` and `loadHotFiles()` now use `getWeekRange(getCurrentDate())` around the selected date.
- **Weekly digest showing single date instead of range** — weekly digest header now renders "13 abr – 19 abr" using `getWeekRange()`.
- **Mini-calendar chips empty on fresh install** — `loadDigestHistory()` now fetches from `/api/timeline/commit-days` (real git activity) instead of only cached digest dates.
- **`navigateDay()` UTC midnight bug** — `new Date(dateStr)` parsed `"2026-04-16"` as UTC midnight, shifting to April 15 in UTC-negative timezones. Fixed to use local date constructor.
- **Migration 3 (`_mig_3_utc_to_local`)** — re-converts existing UTC timestamps (`+00:00`) in `git_commits` to local naive format, fixing date boundary mismatches.

### Changed
- **LLM policy for historical dates** — dates prior to the current Monday no longer auto-trigger Ollama on navigation. L3 narrative is generated only on explicit "Regenerar" click. `allow_llm` parameter propagated through `DigestEngine.get_daily_digest()` and `get_weekly_digest()`.
- **`max_tokens` daily** — increased from 800 → 1000 to prevent truncation of the "Perspectiva" section on high-activity days (26+ commits, 8+ PRs).
- **Digest collapsed preview** — from 120px (~3 lines) to 320px (~12 lines).
- **`renderDigest()` architecture** — function now only handles stats rendering. Text, badge, technical summary, and collapse state are handled exclusively in `loadDailyDigest()` / `loadWeeklyDigest()`.
- **Version bump** — `pyproject.toml` and `README.md` updated to 1.4.0.
- **MCP Setup docs** — README now correctly references `~/.claude/.mcp.json` (not `settings.json`) for global MCP config.

---

## [1.3.0] — 2026-04-17

### Added
- **Digest date navigation** — `← Ayer` / `Mañana →` / `Hoy` buttons and date picker on Code Timeline. Navigate historical digests by date without reloading the page.
- **Mini-calendar digest history** — shows last 14 cached digests as clickable day chips with L2/L3 level badge. Clicking loads that day's digest instantly.
- **`/api/timeline/digest-history` endpoint** — returns list of cached digest dates, periods, and levels for the mini-calendar.
- **`list_cached_digests()` in GitStore** — queries `daily_digests` table and returns dates with digest level for display.
- **Programmatic technical summary** — `render_technical_summary()` in `template.py` produces a data-driven block below the L3 narrative: commit/author/LOC metrics, PRs merged with titles, issues opened/closed, contributor percentages, top 5 hot files, active branches. Not LLM-generated — always accurate.
- **Continuity context for daily digests** — `DigestEngine._build_continuity_context()` computes previous day's commits, merged PRs, and opened/closed issues, plus stale issues open >3 days. Context is passed to `generate_daily_narrative()` → `_build_daily_prompt()`, giving the LLM temporal awareness across days.
- **Branch data migration** — `_mig_2_extract_branches()` (versioned migration #2) parses existing merge commit messages to extract branch names. Patterns: `Merge pull request #N from owner/branch`, `Merge branch 'name'/"name"`. Only processes `is_merge=1` commits to avoid false positives. Tracked in `schema_migrations` table — runs exactly once per DB.

### Fixed
- **Routing bug: `digest-history` unreachable** — `startswith("/api/timeline/digest")` intercepted `/api/timeline/digest-history` before it could be matched. Fixed by reordering routes: `digest-history` now matches before `digest`.
- **`is_available()` blocks L3** — `OllamaCloudClient.is_available()` did `GET /api` which returns 404, causing all digests to fall back to L2. Removed the `is_available()` guard in `llm.py`; `chat()` is already fully defensive (never raises, returns None on any failure).
- **Timestamp timezone bug** — git commits stored with local timezone (e.g. `-06:00`) were mismatched by SQLite string comparison in date range queries. Fixed by normalizing all timestamps to UTC at ingest (`_normalize_timestamp()` in GitHarvester). `_mig_1_normalize_timestamps()` (versioned migration #1) converts existing data — runs exactly once, tracked in `schema_migrations`.
- **Template early return on 0 commits** — `render_daily()` returned "No hubo actividad" when `commits == 0`, ignoring real GitHub activity (PRs merged, issues opened). Now checks `has_activity` across commits + PRs + issues before early-returning.
- **Weekly digest hidden 5 of 7 days** — `loadWeeklyDigest()` only ran on Monday (day 1) and Friday (day 5). Removed the day restriction; weekly digest is now visible every day.
- **`today` stale when browser left open overnight** — `today` was computed once at page load. Now recalculated via `getToday()` on each polling cycle.

### Changed
- **L3 narrative length** — `max_tokens` increased from 500 → 800 (daily) and 700 → 1200 (weekly). System prompts updated to request 4-section structured analysis (daily) and 6-section weekly balance with explicit word limits (500 daily, 800 weekly).
- **Digest layout** — digest content wrapped in `.digest-content` container with `max-width: 720px` centered, `line-height: 1.8`, `letter-spacing: 0.01em`. Improved readability on wide screens.
- **Collapsible digests** — digest narrative sections collapse to 320px preview with gradient fade and "Expandir"/"Contraer" toggle. Technical summary always visible without expanding.
- **`generate_daily_narrative()`** — now accepts `context` keyword argument and passes it through to `_build_daily_prompt()`.
- `pyproject.toml` version bumped to 1.3.0.
- `README.md` version reference updated to 1.3.0.

---

## [1.2.0] — 2026-04-16

### Added
- **Git repository management** — `mool repo add/list/remove` CLI subcommands. Registers repos in `~/.moolmesh/config.toml`, ingests 14 days of commit history on add.
- **TOML configuration** — `~/.moolmesh/config.toml` for repos, GitHub token, Ollama settings, github_handle. Reads with `tomllib`, writes with manual serializer.
- **GitStore** — `~/.moolmesh/github.db` SQLite database with 9 tables: repos, git_refs, git_commits, commit_files, github_issues, github_milestones, github_project_items, daily_digests, api_cache. WAL mode, foreign key cascades, threading.Lock.
- **GitHarvester** — daemon thread polling registered repos every 120s. Runs `git fetch --all`, compares refs, ingests new commits with numstat file stats. Pushes to SSE buffer.
- **GitHubHarvester** — 3 daemon threads polling GitHub API: issues/PRs every 15s (REST + ETags), milestones every 60s, Projects v2 every 60s (GraphQL). Config cached with 60s TTL.
- **GitHubClient** — zero-dependency HTTP client using `urllib.request`. REST with ETags (304 = free), GraphQL for Projects v2. Rate limit tracking.
- **OllamaCloudClient** — zero-dependency client for Ollama Cloud API. Bearer token auth, 30s timeout. Never raises — returns None on any failure.
- **Project Pulse page** (`/projects`) — PR pipeline kanban (draft → review → approved → merged → closed), issues list with labels, milestones with progress bars, Project v2 board. Live polling. Spanish UI.
- **Code Timeline page** (`/timeline`) — daily/weekly digests, commit feed with author avatars, author stats bar chart, hot files table. Regenerate button for digests. Spanish UI.
- **Digest engine** — 3-level pipeline: L1 SQL stats (always), L2 Spanish text template (always), L3 LLM narrative via Ollama Cloud (optional). Cached in github.db. `force_refresh` parameter to regenerate.
- **SessionCommitLinker** — correlates commits with AI sessions: Co-Author detection, issue refs (`#42`, `fixes #45`), timestamp proximity (10min window). `run_batch()` persists results to SQLite.
- **Navigation** — unified nav bar across all 4 pages: Sesiones AI, Analytics, Project Pulse, Code Timeline.
- **`git_utils.py`** — subprocess wrappers: `is_git_repo`, `get_remote_url`, `parse_github_remote` (SSH + HTTPS), `git_fetch`, `get_remote_refs`, `git_log_range`, `git_log_since`. All with timeouts and exception safety.
- 9 new API endpoints: `/api/repos`, `/api/github/{issues,prs,milestones,project-board}`, `/api/timeline/{commits,authors,hot-files,digest,pending}`.
- 93 new tests (331 total) across 10 new test files.

### Fixed
- **Query param validation** — all API handlers use `_parse_int()` helper with try/except. Invalid params (e.g., `?repo_id=abc`) return defaults instead of 500 errors.
- **Digest fallback `until` parameter** — server-side L1 stats fallback now passes `until` to `get_author_stats()` and `get_hot_files()`, preventing unbounded date ranges.
- **Token preservation** — `_serialize_toml()` now writes actual token/api_key values instead of hardcoded empty strings. `save_config()` no longer erases secrets on `repo add/remove`.
- **Labels JSON parse crash** — `projects.html` `renderIssues()` now handles both array (from parsed backend) and JSON string formats via `Array.isArray()` check.
- **github_handle deduplication** — removed duplicate `handle` field from `[github]` TOML section. Single source of truth in `[user].github_handle`.
- **SSE buffer falsy check** — harvesters use `is not None` instead of truthiness check for empty deque (which is falsy).
- **Dead code cleanup** — removed redundant JSON parsing in `_serve_timeline_pending` (assignees already parsed by `get_issues()`).
- **Config I/O reduction** — `GitHubHarvester._poll_all_repos()` caches config with 60s TTL instead of reading TOML from disk on every 15s tick.
- **Correlation persistence** — `run_batch()` now calls `update_commit_correlations()` to persist `ai_assisted` and `session_id` in SQLite.
- **`get_author_stats()` and `get_hot_files()` date bounds** — added optional `until` parameter to both methods for accurate historical digest stats.

### Changed
- `pyproject.toml` version bumped to 1.2.0.
- `DashboardServer.__init__` initializes `digest_engine` and `ollama_client` to None before the `if config.repos:` block, preventing AttributeError when no repos are configured.
- `get_issues()` and `get_pr_pipeline()` now parse `assignees` and `labels` from JSON strings to Python lists in the SQLite read path.
- `renderMarkdown()` — zero-dependency regex-based markdown renderer replaces the non-existent `marked.parse()` CDN dependency.
- L3 narrative deduplication — `digestNarrative` element hidden to prevent showing the same text twice.

---

## [1.1.0] — 2026-04-11

### Added
- **Unified Harvester pattern** — single loop per provider: discover -> read offset from SQLite -> parse with chunk-and-tail -> store_with_offset() atomically -> push to SSE deque -> sleep -> repeat. Eliminates 5 redundant code paths (queue dispatcher, gap_fill, backfill, _find_safe_offset, per-session timestamp scanning).
- **Transactional offsets** — `store_with_offset()` inserts events and updates file offset in a single `BEGIN IMMEDIATE` transaction. Exactly-once semantics with crash safety.
- **Content fingerprint tracking** — `file_registry` table uses SHA-256 of first 1KB to identify files regardless of path or inode changes. Replaces path-based offset tracking.
- **SSE reconnection without event loss** — stream includes `id:` field (SQLite autoincrement IDs). Browser sends `Last-Event-ID` on reconnect, server replays via `load_since_id()`. `retry:3000` directive for automatic reconnection.
- **Snapshot+stream initialization** — `/api/recent` returns `{events, max_id}`. Client opens SSE with `?last_id=max_id`. Zero-gap guarantee on page load.
- **`collections.deque` SSE buffer** — replaces `queue.Queue`. O(1) append/popleft, auto-discards oldest at maxlen=1000. CPython GIL-atomic operations.
- **`load_since_id()`** — `SELECT * FROM events WHERE id > ? ORDER BY id ASC` for SSE replay.
- **`get_max_id()`** — `SELECT MAX(id)` for snapshot+stream pattern.
- 32 new tests (238 total).

### Fixed
- **Critical: `_broadcast_sse` deque desync** — seen-counter grew past `deque.maxlen`, causing `current_len > seen` to become permanently False after 1000 events. Replaced with `popleft()` consumption pattern.
- **SSE event loss on reconnect** — previously, reconnecting clients received only new events, losing anything that happened during the disconnect. Now replays from SQLite.

### Removed
- `queue.Queue` event dispatcher and `_dispatch_events()` thread — replaced by deque + popleft.
- `gap_fill()` and `backfill()` active logic — now no-op stubs. Harvesters handle this automatically.
- `_find_safe_offset()` per-watcher method — replaced by SQLite `file_registry` offsets.
- `get_last_timestamp_per_session()` — replaced by per-file fingerprint offsets.
- Per-session timestamp scanning in all 3 watchers — ~120 LOC removed.

### Changed
- `BaseWatcher` renamed to `BaseHarvester` (alias kept for backwards compat).
- Watchers receive `(store, sse_buffer)` instead of `(event_queue)`.
- `store_with_offset()` returns `list[dict]` with assigned SQLite IDs (individual INSERT to capture `lastrowid`). Duplicates excluded from return value.
- `load_recent()` includes `id` field in returned dicts.
- `/api/events` accepts `?last_id=` query parameter and `Last-Event-ID` header.
- SSE stream emits `id:`, `retry:3000`, and 20s keepalive comments.
- `backfill.py` reduced to no-op stub (23 LOC).
- Net reduction: ~620 LOC removed across the codebase.

---

## [1.0.0] — 2026-04-09

### Added
- **Fingerprint deduplication** — events are deduplicated by MD5 hash of (provider, session_id, timestamp, event_type, summary). `backfill --full` is now idempotent and safe to re-run.
- **Partial unique index** — `CREATE UNIQUE INDEX ... WHERE fingerprint IS NOT NULL` allows migrated events (NULL fingerprint) to coexist without conflicts.
- **Automatic schema migration** — databases from v0.5.0 are transparently migrated on first startup. No manual steps required.
- **Startup gap prevention** — watchers calculate read offset by scanning JSONL for the last known event timestamp from the database. A second gap-fill pass after watcher startup acts as a safety net for events written during the restart window.
- **`_find_safe_offset()` method** — all 3 watchers scan files to find the first line newer than the last known database timestamp, ensuring no events are lost between gap-fill and watcher startup.
- **Per-session offset calculation** — `get_last_timestamp_per_session(provider)` returns the last known timestamp per session instead of a single global max. Each watcher extracts the `session_id` from each JSONL file and uses its specific timestamp. Sessions not yet in the database start from offset 0 (fingerprint dedup prevents duplicates).
- **Qwen parser/adapter tests** — 13 parser tests + 14 adapter tests with real JSONL fixtures.
- **Dashboard API tests** — 9 tests covering all HTTP endpoints (stats, recent, sessions, tools, analytics, db-stats, history).
- **Batch reporter tests** — 7 tests for `_safe_dirname`, `_filter_by_time`, and `load_project_messages`.
- **Watcher reactivation tests** — 3 tests verifying offset preservation across idle periods.
- **Watcher startup gap tests** — 4 tests verifying `_find_safe_offset()` calculates correct byte offsets.
- **Per-session offset tests** — 2 tests verifying independent offsets per session and fallback for unknown sessions.
- **Concurrency test** — verifies CodexParser `parse_file()` is thread-safe with shared instances.
- **LICENSE file** — MIT license added to project root.
- 54 new tests (206 total).

### Fixed
- **Critical: watcher startup gap** — on dashboard restart, watchers started at EOF (`f.stat().st_size`), silently losing all events between the last gap-fill and the current file size. Now watchers scan the JSONL to find the correct byte offset based on the last known timestamp from the EventStore. A second gap-fill pass captures events written during the restart window.
- **Critical: per-session offset** — `_find_safe_offset()` used a single global timestamp per provider (the max across all sessions). Sessions with older timestamps got offset=EOF, making them invisible in the Live Feed. Now uses per-session timestamps from `get_last_timestamp_per_session()`.
- **Critical: watcher stops detecting after 4h idle** — `_rescan_new_files()` deleted file offsets when unregistering stale files, causing events to be lost when the file became active again. Now preserves offsets and processes pending changes before unregistering. Affects all platforms, most visible on Linux.
- **CodexParser thread safety** — `parse_file()` now uses local context instead of shared `_session_ctx`, preventing cross-contamination in parallel batch reports.
- **Fingerprint mismatch in migration** — migration originally generated SQL-based fingerprints incompatible with the Python MD5 formula. Fixed to use NULL for migrated events with a partial unique index.
- **`query()` ordering** — `/api/history` endpoint used `ORDER BY id DESC` (insertion order) instead of `ORDER BY timestamp DESC` (chronological). Fixed to match `load_recent()`.
- **`load_recent()` ordering** — changed from `ORDER BY id DESC` to `ORDER BY timestamp DESC`. After backfill, events appeared out of chronological order in the Live Feed.
- **ConnectionResetError spam** — browser SSE disconnects no longer flood the server console.

### Changed
- Schema: `fingerprint TEXT UNIQUE` column replaced by `fingerprint TEXT` + partial unique index.
- `_rescan_new_files()` uses `_find_safe_offset()` with per-session timestamps for newly discovered mid-session files instead of defaulting to EOF.
- `_watch_loop()` and `_rescan_new_files()` use `_session_timestamps` (per-session) instead of `_last_known_ts` (global).
- Stale offset cleanup only triggers when dict exceeds 200 entries and files are >24h old.

---

## [0.5.0] — 2026-04-08

### Added
- **Historical backfill** — new `mool backfill` command imports all historical JSONL data into the EventStore. `--full` flag for complete import, default mode does gap-fill only.
- **Automatic gap-fill on startup** — dashboard detects and imports events that occurred while it was offline. No manual intervention needed.
- **EventStore methods** — `get_last_timestamp_per_provider()` and `has_events()` for backfill coordination.
- **fd limit safety net** — `resource.setrlimit(RLIMIT_NOFILE, 4096)` at CLI startup (macOS defaults to 256).
- **Watcher recency filter** — all 3 watchers only monitor files modified in the last 4 hours, with automatic unregistration of stale files.
- 12 new tests (152 total): 3 concurrency, 5 watcher filtering, 3 backfill, 1 fd limit.

### Fixed
- **Critical: SQLite fd leak** — `ThreadingHTTPServer` created orphan SQLite connections via `threading.local()`, exhausting 256 fd limit in ~60 seconds with analytics page open. Replaced with single shared connection + `threading.Lock()`.
- **Watcher fd waste** — kqueue registered 132 JSONL files but only 3 were active. Now watches only recent files (reduced to 3-5 fds).
- **Analytics timezone bug** — "Today" filter used UTC midnight which in UTC-negative timezones meant filtering for tomorrow's date. Fixed to use local time.

### Changed
- Codex watcher filter tightened from `_MAX_AGE_DAYS=7` to `_MAX_AGE_HOURS=4`.
- `_rescan_new_files()` in all watchers now unregisters inactive files (previously only added new ones, fd count grew monotonically).

---

## [0.3.0-beta] — 2026-04-07

### Added
- **Linux support** — `PollingWatcher` as cross-platform fallback when `kqueue` is unavailable. Factory `create_file_watcher()` auto-selects.
- **Codex system prompt detection** — `_is_system_prompt()` filters `<permissions>`, `<skills_instructions>`, `<environment_context>` XML tags and `developer` role from QA pairs.
- **`watched_count` property** — public property on all watchers, replacing direct `_kq` access.
- README documentation aligned with actual CLI behavior.

### Fixed
- Codex adapter no longer classifies system prompts as user messages.

---

## [0.2.0] — 2026-04-05

### Added
- **Analytics dashboard** — `/analytics` page with charts: tokens by provider, event types, hourly activity, top tools, top projects, models.
- **EventStore** — SQLite persistence for dashboard events (`~/.moolmesh/events.db`).
- **Batch reports** — `mool report` generates Markdown analysis per project with full/week/day time windows.
- **Export Reports button** — trigger report generation from analytics page.
- **Session tracking** — `SessionTracker` with per-project stats, tool histogram, provider tokens.
- **Multi-provider watchers** — Codex and Qwen watchers alongside Claude.

---

## [0.1.0] — 2026-03-28

### Added
- Initial release.
- **Claude Code parser** — JSONL parser with incremental reading, content block extraction, usage parsing.
- **Claude Code adapter** — converts entries to `UnifiedMessage` and `UnifiedEvent`.
- **Project discovery** — auto-discovers Claude, Codex, Qwen session directories.
- **Live dashboard** — HTTP + SSE real-time event feed with dark theme.
- **kqueue watcher** — macOS file monitoring for instant event detection.
- **CLI** — `mool dashboard`, `mool discover` commands.
- Unified data model: `Provider`, `MessageRole`, `TokenUsage`, `ToolCall`, `UnifiedMessage`, `UnifiedEvent`.
