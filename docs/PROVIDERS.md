# Adding a provider to MoolMesh

A provider is a **pluggable quartet** that turns one agent's on-disk session
format into MoolMesh's unified event model. Adding one must not change the
core, the other providers, the dashboard or the MCP server — if it does, that is
a signal the provider template itself needs work (see `VISION_ROADMAP.md` §5).

This document is the checklist, written from the Pi provider (v1.25.0): use
`hub/models/pi.py`, `hub/parsers/pi_parser.py`, `hub/adapters/pi_adapter.py`,
`hub/watchers/pi_watcher.py` and `tests/test_pi_*.py` as the worked example.

> Invariants: zero external dependencies, zero cloud, **read-only** over the
> provider's files (open with `mode=ro` for SQLite; never write into the
> agent's directory), additive registrations only, and `hide_project_names`
> respected on every new human surface.

## 1. The quartet

| Piece | File | Responsibility |
| --- | --- | --- |
| Model | `hub/models/<name>.py` | dataclass(es) for the raw session format |
| Parser | `hub/parsers/<name>_parser.py` | raw file/DB → model entries (full + incremental) |
| Adapter | `hub/adapters/<name>_adapter.py` | model entry → `UnifiedEvent` / `UnifiedMessage` / `SessionMeta` |
| Watcher | `hub/watchers/<name>_watcher.py` | discovery → offset → parse → store → SSE (`BaseHarvester`) |

**Models** (`hub/models/base.py:Provider`) — add the enum member
(`Provider.PI = "pi"`). Keep the model a plain dataclass with `slots=True`; no
logic beyond field definitions.

**Parser** — subclass `BaseParser` and implement:
- `parse_file(path)` for the batch/backfill path.
- `parse_incremental(path, offset)` reading only complete bytes since the
  byte offset (JSONL) or a rowid cursor (SQLite); return
  `(entries, new_offset)`. Never parse a partial trailing line: wait for the
  newline so a half-written entry is re-read whole next cycle.
- `can_parse(path)` (JSONL: check the first line's type; DB: check the table
  set and filename) so discovery never confuses providers.
- Keep per-file context (session id, cwd, last model, seen entry ids) in a
  `self._session_ctx` dict keyed by path: a chunk read from `offset > 0`
  carries no header of its own. `BaseHarvester.harvest_history_file` clears
  that dict for the file after a history walk, so memory stays bounded.
- **Dedupe by entry id** when re-reading (a re-read from 0 must not duplicate
  rows); the store's fingerprint dedupes too, but the parser should be honest
  on its own.
- If the format is a tree (entries carry `parentId`, like Pi) ingest **every**
  entry once, in file order, and keep `parent_id` on the model. Provide a
  `linearize(entries, leaf_id=None)` helper to walk the active branch
  leaf → root for consumers that need a transcript.

**Adapter** — subclass `BaseAdapter` and implement `to_unified`,
`to_event` (plus `to_events` when one entry can touch several paths) and
`to_session_meta`. Rules:
- `event_type` must be a `MessageRole` value: `user`, `assistant`, `system`,
  `tool_use`, `tool_result`, `thinking`, `summary`.
- `file_path` is the **absolute, normalized, untruncated** path a file tool
  touched. Relative paths resolve against the session `cwd` (per-call working
  directory first, when the provider reports one). Shell tools are listed in
  `hub.models.base.SHELL_TOOLS`: their command must NEVER become `file_path`
  (#58). Directory arguments are not file paths — skip them.
- Tokens map to `TokenUsage(input_tokens, output_tokens, cache_creation,
  cache_read)` and to the event dict keys the other providers use
  (`input`, `output`, `cached_input`, `reasoning`). Check whether the
  provider's `output` already includes reasoning before adding it.
- A cost, when the format reports one, goes in the event `tokens` JSON as
  `cost` (the `sessions.cost` column is a monotonic maximum — do not put a
  per-message cost there).
- `to_session_meta` fills id, cwd, model (last `model_change` or the
  assistant's own), title + initial prompt (first user prompt; explicit names
  win) and any small additive `metadata` (parent session, active leaf id).

**Watcher** — subclass `BaseHarvester`, set `CATCHUP = True` for file-based
providers, and:
- `provider_name` returns the enum value string.
- `discover_files(since, skip_dir)` goes through `ProjectDiscovery` (see §2),
  filters by `mtime >= cutoff` and records the project label per file.
- `_parse_and_adapt(path, offset)` runs parser → adapter, upserts session
  metadata (once per session per chunk, from the **last** entry so the
  accumulated context is complete) and returns `(event_dicts, new_offset)`.
- Nothing else: `BaseHarvester` owns the loop, health snapshot (#65),
  quarantine, catch-up (#45) and history chunks (`harvest_history_file`).

## 2. Discovery

`hub/discovery.py:ProjectDiscovery` — add `<name>_base` to `__init__`
(defaulting to the provider's real directory, honoring its env override — Pi
reads `PI_CODING_AGENT_DIR`), a `discover_<name>()` returning
`DiscoveredProject`s, and register it in `discover_all()`.

- Group sessions by the **real cwd** when the folder encoding is lossy
  (encoding `/`, `_` and `-` all to `-`, like Claude and Pi do): read the
  header from the file, never trust the directory name.
- Files with an unreadable header go to one `<name>-sessions` fallback bucket,
  never dropped.
- Forward `skip_dir` into the walk and prune directories in place (cloud
  placeholders must never be listed, #45).
- Windows: homes are `%USERPROFILE%`, so default to `Path.home() / ...`; the
  filesystem layout (`~/.pi/agent/sessions/--C--Users-...--/`) is the same.
  Any `\\?\` prefix must be stripped/normalized by the shared path helpers.

## 3. Registration checklist

- [ ] `Provider` enum member (`hub/models/base.py`).
- [ ] Watcher registration in `hub/dashboard/server.py`: the `active` default
      set, the `if "<name>" in active` block, the label
      (`self.watchers.append(("Pi", watcher))`) — this is what `/health`
      reports (#65).
- [ ] `FILE_PROVIDERS` in `hub/backfill.py` + a `make_watcher` branch (file-based
      providers only); update the legacy `backfill`/`gap_fill` stub dicts too.
- [ ] `mool backfill --provider <name>` choices and help (`hub/cli.py`).
- [ ] `hub/batch_reporter.py`: parser + adapter maps (and `provider_map` for
      `--provider` filtering).
- [ ] CLI `--provider` choices for `report`, `discover`, `sessions`.
- [ ] MCP: provider filters are free-form (no code change needed) — update the
      docstring examples that enumerate providers.
- [ ] `SHELL_TOOLS` (`hub/models/base.py`) if the provider has shell tools.
- [ ] UI: dashboard `dashboard.html` (status dot, token bar, feed filter,
      timeline, CSS class with a distinct color), `portfolio.html`
      (`PROV_COLOR`, `provClass`, legend) and `analytics.html` (`PCOLORS`,
      stacked areas). Identity never depends on color alone (#37): every bar
      carries a text label.

Workspace attribution (#39/#40) needs no per-provider code: absolute
`file_path`s enter the incremental pass automatically; verify it on real data.

## 4. Fixtures and tests

- **Anonymize the fixture**: fictional cwd, project names, prompts and tool
  outputs. Never a real owner path, client name or transcript. JSONL fixtures
  live in `tests/fixtures/<name>_sample.jsonl`; SQLite providers build a
  minimal DB in the test.
- Unit tests for the quartet:
  - parser: every entry type, tree branches kept, id dedupe on re-read,
    incremental offset (complete lines only), context seeded after a restart;
  - adapter: role mapping, absolute `file_path` from file tools, shell commands
    never in `file_path`, tokens/cost mapping, session metadata;
  - watcher: discovery window, `_parse_and_adapt` ingest, idempotent re-read,
    history harvest refreshes session stats;
  - discovery: cwd grouping, fallback bucket, `skip_dir`, env override;
  - registration: watcher in `DashboardServer`, backfill provider,
    reporter maps, an end-to-end `run_backfill`.
- **Contract test** (`tests/test_provider_contract.py`): add the fixture loader
  to `CASES`. It asserts provider, session id, parseable timestamp, valid
  event type, absolute-or-None `file_path` and the shell-tool rule for every
  provider — one bar for all seven.

## 5. Validate with real data (read-only)

1. Parse the provider's real sessions with the new parser/adapter **without**
   touching the real store: count entries per type, sessions, tokens and
   extracted paths (masked in any report); assert zero exceptions.
2. On a **copy** of `~/.moolmesh` (never the real one), run
   `mool backfill --provider <name>` and one attribution cycle, then check the
   sessions and workspaces that got attributed.
3. Never start/stop the owner's daemon for a validation run.

## 6. Definition of done

- [ ] Quartet + discovery + every registration above.
- [ ] Anonymized fixture, unit tests and the provider contract test pass.
- [ ] Full suite green; CI (`publish.yml`) not red.
- [ ] Real-data validation recorded in the issue/PR (read-only).
- [ ] Any deviation (e.g. a format quirk, a deferred export path) documented as
      a follow-up issue rather than silently skipped.

## 7. Provider notes

Per-provider facts a maintainer needs when the format moves. The Pi section is
the worked example above; Antigravity is the second file-based provider and
shows what changes when a format is less friendly.

### Antigravity CLI (`agy`, v1.27.0, #74)

**Scope.** The CLI only (`~/.gemini/antigravity-cli`). The desktop app
(`~/.gemini/antigravity`) and the IDE (`~/.gemini/antigravity-ide`) keep
other data dirs and are out of scope. `mool mcp setup --target antigravity`
is a separate unit (its server entry must be named `moolmesh`).

**Data dir.** `$ANTIGRAVITY_APP_DATA_DIR` when set (a variable seen in the
binary, not documented by the vendor), else `~/.gemini/antigravity-cli` on
every platform. The vendor documents no Windows data dir: `%USERPROFILE%` plus
the same relative path is used — unverified there.

**What is read** (strictly read-only, never written):

| File | Used for |
| --- | --- |
| `brain/<conversation-uuid>/.system_generated/logs/transcript.jsonl` | the events — plain JSONL, append-only, one step per line |
| `conversation_summaries.db` (`PRAGMA user_version = 3`) | session metadata only: title, preview, `workspace_uris`, `parent_conversation_id`, `nesting_depth`, `last_modified_time` |

**What is NOT read, and why.**
`conversations/<uuid>.db` (SQLite in WAL with payloads in protobuf — opening it
would take locks on a live store and needs a schema we do not have),
`transcript_full.jsonl` (same step indices, untruncated: the bounded
`transcript.jsonl` is the contract the vendor documents through hooks, and
keeps stored text small, #52), `presence/<uuid>.lock` (created at start, never
updated, left behind after a close: not a liveness signal), `history.jsonl`,
`cache/*`, `log/*`, `annotations/`, `implicit/`, and anything holding accounts
or credentials.

**Format facts the parser relies on** (verified on real transcripts):

- A transcript is append-only and a step is never rewritten (a `RUNNING` step
  stays `RUNNING`); a resumed conversation keeps appending to the same file. No
  tail refresh is needed.
- `step_index` is **not** unique or ordered: some transcripts repeat their
  steps (a copy restarting at 0), some write steps out of order, indices have
  gaps. Every step is ingested once per conversation (first occurrence wins);
  event order comes from `created_at`, never from the line order.
- Every value in a tool call's `args` is a **JSON-encoded string** (a path
  arrives as `"\"/a/b.py\""`). The parser decodes each value once; a value that
  does not decode (cut by the writer) is never turned into a path.
- Tool calls live on `PLANNER_RESPONSE` steps. Their results are separate steps
  (`GENERIC`, `RUN_COMMAND`, `VIEW_FILE`, `LIST_DIRECTORY`, `FIND`, `CODE_ACTION`,
  `INVOKE_SUBAGENT`, `ASK_QUESTION`) with only `content`; nothing pairs a call
  with its result.
- `created_at` has one-second resolution (`...Z`, UTC). `status` is
  `DONE | RUNNING | ERROR` and informational.
- `conversation_summaries.db` indexes **every** Antigravity surface. Rows are
  kept only when `app_data_dir` (stored as a name relative to `~/.gemini`, e.g.
  `antigravity-cli`) is this CLI's own. A subagent is an ordinary conversation
  with its own transcript and a `parent_conversation_id`.

**Mapping.** `USER_INPUT` → `user`; `PLANNER_RESPONSE` → `assistant` (content),
`thinking` and one `tool_use` per call; any step's `tool_calls` become `tool_use`;
result steps → `tool_result` (no `tool_name`, no `file_path`);
`SYSTEM_MESSAGE` / `ERROR_MESSAGE` → `system`; `CHECKPOINT` /
`CONVERSATION_HISTORY` → `summary`; `EPHEMERAL_MESSAGE` and unknown types are
skipped (unknown ones are counted and logged once). `file_path` comes only from
`view_file` (`AbsolutePath`) and `write_to_file` / `replace_file_content` /
`multi_replace_file_content` (`TargetFile`); `run_command` is a shell tool and
never a path (#58). Subagents are separate sessions (`is_sidechain`, parent in
`metadata`), never folded into the parent. Project: first `workspace_uris`
entry, else the first `run_command` `Cwd`, else a fallback.

**Incremental read.** Streamed line by line from the byte offset, complete
lines only. The set of seen `step_index` values is **derived, not persisted**: it
is a pure function of the file up to the stored offset (append-only), so a
parser that sees a new offset (daemon restart, history walk, a retry after a
failed store, a truncated file) rebuilds it from the bytes before that offset;
`events` and `file_registry` are unchanged. The offset is keyed by
`antigravity:<path>` (`STABLE_KEY`). Session first/last event times are
written from the chunk's earliest and latest `created_at`, because the last
line is not necessarily the latest step.

**Summaries index.** Read once per rescan, not per poll; the last good answer is
kept. Absent, locked, corrupt, another schema or an unknown `user_version`
means the transcripts alone are ingested (one warning, one notice when it reads
again). With the CLI running its `-wal` sits next to the file and the index is
opened `mode=ro`; with no `-wal` it is opened `mode=ro&immutable=1` so the read
never leaves an empty `-wal`/`-shm` behind (#54).

**Ids (ahead of #67).** `UnifiedMessage.id = <uuid>:<step_index>`,
`ToolCall.tool_id = <uuid>:<step_index>:<k>`. The event fingerprint is
unchanged, so two *identical* calls of one step (same tool, same args)
collapse into one event, as Pi's do, and so do identical `system` /
`tool_result` events of different steps written in the same second (0.9 % of
the events of the real conversations checked; no `tool_use` ever). The ids
above make #67 a pure schema change.

**Gaps (left empty on purpose).** Model, tokens, cost and git branch. The
transcript carries `input_tokens` / `output_tokens` / `cache_read_tokens` on
some `PLANNER_RESPONSE` steps (about 5 % of steps in recent versions); they are
**not** mapped in v1 — whether they are per call or cumulative is unverified —
and the UI shows no token figure for the provider instead of a `0`. The
per-conversation `.db` holds more in protobuf, which stays unread (zero
dependencies).

**Risk.** The layout is not documented as an API and the vendor ships several
releases a week (the format already moved once, `.pb` → `.db`). Guards: lines
without a numeric `step_index`, a `type` or a valid `created_at` are skipped
(logged once per file and reason); an unknown `type` is counted and skipped; an
unknown `user_version` ignores the index. None of them stops a watcher (#52,
#65).
