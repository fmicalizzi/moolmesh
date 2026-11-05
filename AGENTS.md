# AGENTS.md — Working in the MoolMesh repo

This file is the system prompt for anyone — human or AI agent — working in this
repository. Read it before making changes. It is the single source of truth for
*how* work happens here; `PHILOSOPHY.md` is *why*, `VISION_ROADMAP.md` is *where to*.

---

## 0. Golden rule

**This repository (`moolmesh`) IS the live product.** v1.x (the exact version is
in `pyproject.toml`), published to PyPI
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
├── mcp_registry.py # run registry of MCP servers (~/.moolmesh/run/mcp-<pid>.json) for `mool doctor`
├── cli.py        # `mool` entrypoint
├── daemon.py discovery.py config.py log.py backfill.py
```

- **Provider quartet:** `model → parser → adapter → watcher`, ~300–500 LOC total.
  The 7 current providers: Claude, Codex, Qwen, OpenCode, Cursor, Pi, Antigravity
  (`models/base.py`). `docs/PROVIDERS.md` is the template for a new one.
- **MCP server** exposes read-only query tools (`get_active_sessions`,
  `get_session_chain`, `search_session_content`, `get_session_detail`, …). These are
  the coordination primitives — treat them as product surface, not plumbing.
- **MCP servers outlive upgrades**: each agent session keeps its own `hub.mcp_server`
  until it closes, so the server reports when it runs old code (`get_server_info`, a
  `stale_server` key on dict results) and registers itself in
  `~/.moolmesh/run/mcp-<pid>.json`, which `mool doctor` reads (#70). That file is
  MoolMesh's own state, like the daemon pidfile — the read-only invariant covers
  provider data and `events.db`, not this directory.

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
- **Privacy (public repo):** never carry the owner's real client/org/project
  names in examples — tests, fixtures, docstrings, comments, README/ROADMAP/
  CHANGELOG. Use the neutral vocabulary instead: orgs `acme` / `globex` /
  `initech`, gitless client folder `_acme`, shared workspace
  `SHARED-WORKSPACE`, hyphenated project `widget-services`, reference container
  `/Users/u/work/reference-clones`, root `/Users/u/work/...`. The owner's
  personal-org handle in test data is `ownerhandle` (real handles stay only in
  the repo's own URLs). The local term list lives OUTSIDE the repo at
  `~/.moolmesh/privacy-denylist.txt` (one term per line; never committed, never
  pasted anywhere). `tests/test_privacy_denylist.py` enforces this when
  `MOOLMESH_PRIVACY_DENYLIST` points at that file; without the variable it
  skips. Run
  `MOOLMESH_PRIVACY_DENYLIST=~/.moolmesh/privacy-denylist.txt pytest tests/test_privacy_denylist.py`
  before any release.

---

## 5. Dev & test workflow

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # dev extra = pytest
pytest -q                         # full suite — keep it green (see the baseline below)
mool --help                       # run the CLI
```

- Every change keeps the suite green. New behavior ships with tests. Baseline
  at v1.36.1: 2409 passed / 8 skipped with the privacy denylist, 2408 / 9
  without it. The count only grows; the badge and the README line are synced
  by CI on each release.
- **Isolate HOME.** Tests and manual runs of the branch code use
  `HOME="<tmp>/home"`: a run against the real `~/.moolmesh` can migrate the live
  DBs. The suite is safe with the real HOME today, but anything you run by hand
  is not.
- **Privacy test:** run the suite twice before handing work over — with
  `MOOLMESH_PRIVACY_DENYLIST=~/.moolmesh/privacy-denylist.txt` (one more test
  runs) and without it.
- **Verify in a worktree**, never by checking out a branch in a shared checkout
  someone else is using: `git worktree add --detach <tmp>/wt <branch>`, run the
  suite there, then `git worktree remove`.
- **Real data is read-only and local.** Provider DBs (`opencode.db`, Cursor's
  `state.vscdb`, …) are opened `mode=ro` (`hub.sqlite_ro`); validation on real
  data reports **counts and structure only**, never content, prompts, paths or
  project names. A machine without real agent history validates on synthetic
  fixtures; real-data checks go to a machine that has it (see §8).
- **Shell gotchas:**
  - zsh does not word-split `$VAR`: `env $E cmd` does not apply `E`. Write
    every `VAR="value" cmd` explicitly — this once overwrote the real
    `~/.local/bin/mool`.
  - zsh treats a word starting with `=` as a command lookup, so `echo ====` fails.
  - macOS has no `timeout`.
  - `uv run` inside the repo writes `uv.lock` and resyncs `.venv`.
  - Check `readlink ~/.local/bin/mool` before and after anything that installs.
- CI (`publish.yml`) runs on a published release (and `workflow_dispatch` as a
  dry run): preflight runs the suite, then build, publish (PyPI trusted
  publishing, OIDC) and a bot commit that syncs the version and test count.
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

A release happens **only with the owner's explicit authorization**, given for
that specific version.

1. The work lands on a branch, verified (§5) by someone other than its author
   when possible.
2. A release commit on top: bump `version` in `pyproject.toml` (single source;
   `hub/__init__.py` reads it via `importlib.metadata`), add the `CHANGELOG.md`
   entry (with upgrade notes) and update `ROADMAP.md`. Run the
   documentation checklist below first.
3. With the authorization: fast-forward `main` to that commit and push **only**
   `main` (no `--tags`, no `--force`), then **one** `gh release create vX.Y.Z
   --target <sha>`. CI publishes to PyPI and pushes the asset-sync commit.
4. Check the run: every job green, the wheel and sdist on PyPI, the tag count up
   by one, the bot commit on `main`. If `publish` fails: do not re-run, do not
   delete the release or the tag — report.
5. `git pull --ff-only` afterwards (CI commits back the asset sync), delete the
   merged branches with `git branch -d`.
6. Users upgrade with `pipx upgrade moolmesh --pip-args=--no-cache-dir` (pip's
   HTTP cache can serve a stale index), then `mool daemon restart`, then restart
   open agent sessions so their MCP servers load the new code.

### Documentation checklist (every release)

**A user-visible change without its documentation does not ship.** A
user-visible change is any new or changed command, flag, config key, endpoint,
MCP tool, output field, exit code, default, or upgrade step. Before the release
commit, check each one against this list:

- [ ] `README.md` — the feature where a user looks for it, the **CLI Reference**
      block (checked against the real `mool <cmd> --help`), the MCP tools table,
      the **Updating** section if the upgrade needs an action, and a row in the
      **Roadmap** table for a new minor.
- [ ] `README.es.md` — the same change, in the same section. Same structure as
      `README.md`, section by section (parity, not a summary).
- [ ] `CHANGELOG.md` — the entry, with upgrade notes.
- [ ] `ROADMAP.md` — the version line under Delivered; `Last updated`.
- [ ] `docs/PROVIDERS.md` — if a provider's paths, format or limits changed.
- [ ] `docs/schema/` — if an export contract changed (and its `schema_version`).
- [ ] `VISION_ROADMAP.md` / `.es.md` — only if a rung, phase or owner decision
      changed state.
- [ ] `AGENTS.md` §9 and `CLAUDE.md` — no pinned version or test count that
      goes stale (point to `pyproject.toml`, or state the baseline with the
      version it belongs to).
- [ ] Every command the docs mention exists: `PYTHONPATH=. python -m hub.cli
      <cmd> --help` with a throwaway `HOME`.

---

## 8. Git & collaboration

- **Commit only when the owner asks.** When authorized to commit/push/release/close
  issues: use the owner's own git signature, and **never** add a `Co-Authored-By`
  trailer.
- **Identity:** before every commit, `git config user.email` must be the owner's
  personal address configured for this repo. An agent session may be logged in
  with a different account; git identity is what counts. Never write any email
  address into code, commits, issues or docs.
- **Per-commit privacy loop** (each must print `0 0`):
  ```bash
  for c in $(git rev-list origin/main..HEAD); do echo "$c $(git show "$c" | grep '^+' | grep -ciFf ~/.moolmesh/privacy-denylist.txt) $(git log -1 --format=%B "$c" | grep -ciFf ~/.moolmesh/privacy-denylist.txt)"; done
  git grep -ciFf ~/.moolmesh/privacy-denylist.txt HEAD | wc -l   # 0
  ```
- Never `amend` a commit already pushed to `main` — always a new commit.
- If starting new work on `main`, branch first when appropriate.
- Discuss provider/feature approach in a GitHub issue before a PR (see
  `CONTRIBUTING.md`).

### Working model (orchestrator · worker · field machine)

The owner runs development with two kinds of agent sessions:

- **Orchestrator:** turns an issue into a self-contained **handoff** (scope,
  rules, tests, Definition of Done, report format), then **verifies the
  result itself** — the real diff plus its own test run in a worktree — never
  the worker's report alone. One unit at a time.
- **Worker:** executes one handoff on a branch, commits, does not push, and
  reports. Releases are a separate mini handoff that carries the owner's
  authorization (§7).
- **Field machine:** the machine with real agent history. Development and CI
  work on synthetic fixtures; anything that needs real data (a reparse, a live
  provider experiment, an autostart after a reboot) is a short handoff run
  there that reports counts only.

Decisions that imply execution say **when and who**. Handoffs and reports with
real data stay outside the repo (`~/.moolmesh/reports/`, chmod 600).

---

## 9. Current priorities (snapshot, 2026-11-05 — v1.36.x)

This section is a dated snapshot; `gh issue list` is the live source.

1. **Operations tracking — [#84](https://github.com/fmicalizzi/moolmesh/issues/84):
   the core is complete.** Every core gap of the design kit
   ([`docs/proposals/mool-ops/`](docs/proposals/mool-ops/DECISIONS.md)) shipped
   between v1.27.2 and v1.36.1: stale-data flag + autostart (#76), local day
   (#81, #79), per-PR stats (#80), board / remote-only repos and clients across
   owners (#93, #94, #95), versioned snapshot export (#82, #146), per-contributor
   breakdown (#83) and per-device effort export (#140).
2. **The engine lives outside core**, in `fmicalizzi/mool-ops` (a separate repo,
   private until its first release). It reads MoolMesh only through
   `mool snapshot export` and `mool effort export` (contracts in
   `docs/schema/`), never its SQLite files. Changes it needs from core arrive
   here as issues; a contract change bumps the schema version.
3. **Open in GitHub:** only the #84 tracking issue at this snapshot. New work
   starts from an issue (see `CONTRIBUTING.md`).

Provider expansion and synchronous-coordination direction are owner decisions
tracked in `VISION_ROADMAP.md` §9.
