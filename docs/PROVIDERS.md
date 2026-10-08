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
  provider — one bar for all six.

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
