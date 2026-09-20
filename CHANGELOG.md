# Changelog

All notable changes to MoolMesh are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.19.0] — 2026-09-20

Part B of the `/portfolio` UX redesign (#37): the "new space" on top of the
now-clean Part-A layout — a KPI tile row and hand-rolled charts. **Presentation
only** — the resolver / derived-state / outcome computation (#27/#28/#29), the SSE
schema (read-on-load), and `hide_project_names` masking are all untouched. No new
dependencies (zero-dep: every chart is inline hand-rolled SVG/`<div>`, no chart
library or CDN).

### Added
- **#37 — portfolio KPIs + charts (Part B).**
  - **KPI tile row** — headline numbers over the list: proyectos activos, PR
    mergeados (labeled **all-time**, since outcome counts are all-time totals — not
    windowed like the effort columns), entregados, estancados, clientes activos.
    No series color (number + label + a status-colored top accent).
  - **State distribution** — a single stacked bar plus a legend that carries
    **icon + label + count**, so color is never the sole channel (the status
    palette is exempt from the categorical rule precisely because it ships with a
    label).
  - **Recency histogram** in the same card (fills the previously half-empty space):
    projects bucketed by last-activity age (`state.age_days`, real clocks across
    session/fs/git, **not window-bounded**) — hoy / 1–3d / 4–7d / 8–30d / >30d,
    with an honest `sin dato` bucket for stateless projects. Single measure → one
    hue + direct labels.
  - **Top by activity** (sessions) and **top by delivery** (merged PRs) — horizontal
    bars, **one hue per measure + direct labels** (not a categorical rainbow).
  - **Tri-state honesty in the delivery chart** — a hatched `sin repo` track plus a
    note keep the outcome gap visible in-chart (real-0 ≠ not-measurable), driven by
    the same `isMeasurable()` predicate the Part-A column uses, so they can never
    disagree.
  - **Palette discipline** — the provider trio fails as a color-only identity
    channel (cyan↔purple ΔE 14.2, below the 15 floor), so nothing here distinguishes
    series by color alone; validated, not eyeballed.
  - KPIs/charts re-render on the window toggle (window labeled); labels honor
    masking (`cleanName` over `project_label`, never `project_key`).

Follow-up: **#36** (data-accuracy audit of the portfolio signals) remains open.

---

## [1.18.0] — 2026-09-20

Part A of the `/portfolio` UX redesign (#35): a layout + hierarchy pass that
reclaims the wasted horizontal space and makes the view scannable. **Presentation
only** — the resolver / derived-state / outcome computation (#27/#28/#29), the SSE
schema (read-on-load), and `hide_project_names` masking are all untouched. No new
dependencies (zero-dep: the production strip is hand-rolled `<div>` cells, no chart
library).

### Changed
- **#35 — `/portfolio` layout redesign (Part A).**
  - **Production strip fills its column.** The per-project strip was an inline SVG
    with `preserveAspectRatio="xMinYMid meet"` that rendered at its ~65px natural
    width, left-anchored, leaving a ~1200px horizontal void before the stats. It is
    now a flex row of equal-flex cells that spans the full column width. Same
    semantics (cell color = the day's dominant provider, opacity = session volume).
  - **Dense, single-line rows** (~2–3× more projects per screen) replace the
    two-line rows where the name wrapped and the status chip sat below it.
  - **Clean names:** the `github.com/` prefix is dropped (the org already reads as
    the client), names render on one line with an ellipsis, and the full (masked)
    label is in the tooltip.
  - **Outcome is a first-class column** with an honest tri-state: colored
    merged-PR / closed / open counts (merged PRs weighted as the real delivery),
    a real `0 entregado` for a repo that has merged nothing, and `— sin repo` for
    the not-measurable case — no longer conflated as a single grey "— entreg."
  - **Column headers** (proyecto · ses · d · entrega · producción) on a shared grid,
    and the **derived state as a leading color-dot column** that anchors the
    vertical scan (in both the production card and the grouped list).

Follow-ups: **#35 Part B** (KPI tiles + charts) and **#36** (data-accuracy audit of
the portfolio signals) remain open — both out of scope for this layout-only pass.

---

## [1.17.2] — 2026-09-19

Honest failure reporting for `mool repo sync`. Follow-up to #32 (the "0 commits"
caller-message UX tracked in v1.17.1). No new features, no new dependencies.

### Fixed
- **#34 — `mool repo sync` reported "0 commits" on a real git failure.** The
  `git_log_*` helpers now return `None` on failure (rc≠0 or exception) instead
  of `""`, so a genuine git error is no longer indistinguishable from "0 new
  commits". `cmd_repo_sync` reports an error and exits non-zero; `cmd_repo_add`
  warns without failing (the add itself succeeded) and points at `mool repo
  sync` to retry.
- **Data-loss prevention in the daemon.** `GitHarvester` no longer advances the
  stored cursor of a ref whose `git log` failed — it excludes that ref from
  `update_refs` and retries it next cycle. Previously those commits were skipped
  silently (§4).

---

## [1.17.1] — 2026-09-19

Robustness patch for Windows users under the default console codepage
(cp1252). No new features, no new dependencies — `pyproject.dependencies`
stays empty and the MCP server is **not** migrated to the `mcp` 2.x API (the
version pin is a stopgap; UX of the "0 commits" caller message is tracked as
follow-up #34).

### Fixed
- **#33 — MCP server broken by an unbounded inline dependency.** The PEP 723
  inline dep `mcp>=1.2.0` had no upper bound, so `uv run hub/mcp_server.py`
  resolved `mcp` 2.x (which renamed `FastMCP` → `MCPServer`), and the real
  `ImportError` was swallowed and misreported as "not installed". Pinned to
  `mcp>=1.2.0,<2`; the `except ImportError` now logs with context and the
  startup message **distinguishes "no instalado" from "versión incompatible"**.
- **#31 — CLI crashed with `UnicodeEncodeError` on Windows.** `mool --help`
  (and any non-ASCII output, e.g. the `→` in help text) crashed on a cp1252
  console. The CLI now reconfigures `stdout`/`stderr` to UTF-8 with
  `errors="replace"` on Windows, early in the entrypoint.
- **#32 — `mool repo sync` crashed (and silently reported "0 commits") on
  non-ASCII commit messages on Windows.** The `git log` subprocess calls fell
  back to the locale encoding (cp1252) and failed to decode accented commit
  messages. All `git_utils` subprocess calls now pin `encoding="utf-8"`,
  `errors="replace"`; a git failure is **logged with context** instead of
  masquerading as an empty history.

---

## [1.17.0] — 2026-09-19

Third and final unit of the **Project Intelligence** epic (#26): the portfolio
grows a third tier — **client/org → project → materials** — so the work reads
by *who it's for*, not just as a flat list of repos. **Closes epic #26**
(Observe → Correlate, from effort to outcome, is now whole).

### Added
- **Unified client/org hierarchy (#29)** — a read-layer projection
  (`hub/cache/portfolio_clients.py`) that hangs a client tier over the Stage-1
  project grouping. A **client attribution ladder** decides, per project, who it
  belongs to (first match wins):
  - **Manual override** (`client_overrides`: `project_key → client`) → **git-remote
    owner** (primary: `github.com/<org>/<repo>` → org) → **parent-folder
    convention** (gitless fallback: `~/Downloads/Claude/<client>/<project>`).
  - Orgs are matched **case- and underscore-insensitively** (`_eventsmx` /
    `EventsMX` → `eventsmx`), so a non-git materials folder **reconciles onto the
    same client node** as the git products under it — two feeds (filesystem +
    GitHub org) of the one tree.
- **Client classification** — a known client org (`[workspace] client_orgs`,
  curatable) becomes a **client node** grouping its projects; the owner's own org
  (`personal_orgs`) shows its projects **loose** (no client node); an unknown git
  org lands in the **externos / referencia** drawer (cloned deps/repos); a shared
  workspace of the owner's own (e.g. `PRODUCCIONES`) stays a **top-level node**,
  never forced under a client.
- **Auto-seed for `client_orgs`** — computed from the two stores (github.db repo
  owners ∪ workspace.db orgs with ≥2 projects), firing **only when the owner
  identity is known** (`personal_orgs` / `[user] github_handle`). Owner-curatable.
- **Client-level rollup (contributor-agnostic)** — effort (session/fs/git touches;
  `active_days` **UNIONed** over real day sets, never summed), **outcome**
  (merged-PR / closed-issue / open-issue, all authors summed, none surfaced), and
  the **hottest** project state (activo ≻ enfriándose ≻ estancado ≻ entregado ≻
  pausado) all roll up to the client node, each carrying its evidence.
- **Config `[workspace]`** — `client_orgs`, `personal_orgs`, `client_overrides`
  (all optional; serialize/parse alongside `hide_project_names`).
- **Dashboard `/portfolio`** — renders the collapsible client tier + externos
  drawer, read-on-load; `hide_project_names` masks client and project labels
  (join keys untouched).

### Notes
- **Starts invisible** — with no owner identity configured the projection is a
  **flat no-op**, byte-for-byte the pre-#29 portfolio. One config line activates
  it: `[workspace] personal_orgs = ["<your-org>"]` (or `[user] github_handle`),
  and the auto-seed does the rest.
- **Invariants held** — `delivery_candidate` (#22), the outcome layer (#27) and
  derived state (#28) are **consumed, not changed**; resolver (#20), watcher
  (#21), the `project` field, `events.db`/`github.db` (read **read-only**) and
  the SSE stream are untouched; `author` is never surfaced (team latent, deferred
  to v2.x Org-Scale). Zero new dependencies.
- **Follow-up [#30](https://github.com/fmicalizzi/moolmesh/issues/30)** —
  container split (e.g. `PRODUCCIONES` decomposed into its distinct
  projects/clients) needs re-anchoring at the resolver layer and is deferred; the
  shared workspace renders as its own node until then.

---

## [1.16.0] — 2026-09-19

Second unit of the **Project Intelligence** epic (#26): the portfolio stops
showing effort and outcome as two separate figures and **fuses them into one
honest per-project state** — the integrator of the epic.

### Added
- **Derived project state — the activity ↔ outcome fusion (#28)** — every
  canonical project now carries a single **state** chip in the `/portfolio` view
  (both the production strip and the grouped project list), derived by fusing
  **local activity** (session ingest / filesystem / git) with the **GitHub
  outcome** layer (#27, merged PRs / closed & open issues):
  - **🟢 activo · 🟡 enfriándose · 🔵 entregado · 🟠 estancado · ⚪ pausado.**
    Recent activity reads *activo*; tapering reads *enfriándose*; a quiet project
    is split by its outcome — a merged-PR/closed-issue that **closed the burst**
    is *entregado* (a git **fact**; for a gitless project the `delivery_candidate`
    heuristic (#22) stands in, never conflated), a repo with **open issues still
    hanging** is *estancado*, and quiet-with-nothing-open is *pausado*.
  - **Honest clocks only.** Quiescence age is measured from the *real* last
    activity — session ingest epoch (`events.created_at`, honest even on resumed
    sessions), `path_touches.last_seen`, and `git_commits.timestamp`, all
    normalized to aware UTC via `_parse_ts` — **never** the backfill `first_seen`
    artifact, never the unreliable session `duration`. A project with no real
    activity gets **no state** (evidence-first, never fabricated).
  - **`outcome_measurable` (has-repo vs not).** Taken from the *repos* side of
    `github.db`, so a repo-backed project with **0 PRs is still measurable** (it
    can be *estancado* when issues hang), while a **gitless** project's outcome is
    *not measurable* — it falls to `delivery_candidate`/activity, never to
    *estancado* for lacking PRs it could never have.
  - **A read of evidence, surfaced with its basis** — each state carries the
    signal that determined it plus its last-activity age, exactly the
    `delivery_candidate` discipline: never a bare flag asserted as truth.
  - **Absorbs the cold-projects view (#25).** The quiet states
    (*pausado/enfriándose/estancado*) are the "cold" surface, each shown with its
    last-activity age — #25 folds into this unit.

### Unchanged (invariants held)
- `delivery_candidate` detection (#22), the workspace resolver (#20), the
  filesystem watcher (#21), the `project` layer, and the SSE hot path are all
  untouched — this unit only **consumes** existing data. `events.db` and
  `github.db` are read **read-only** (`mode=ro`); the shared events.db ingest
  scan is handed from the production view into the state layer so the hot path
  reads it once. `author` is never surfaced (contributor-agnostic, team latent).
  Zero new dependencies; zero-cloud; separate stores held; the state chip carries
  only tokens/ints (no labels) so `hide_project_names` still holds.

## [1.15.0] — 2026-09-19

First step of the **Project Intelligence** epic (#26): the portfolio moves from
measuring *effort* to also surfacing *outcome*.

### Added
- **Outcome layer — authoritative delivery in the production view (#27)** — the
  `/portfolio` production view now shows, next to the effort columns (sessions ·
  active days), the **delivery already recorded in `github.db`**: **merged PRs,
  closed issues, and open issues** per **canonical project** (the Stage-1
  `workspace_classification.project_key`, so agent-harness folders fold into
  their real project). e.g. *fiestados: 39 ses · 10d · 180 PR · 101 cerr · 52
  abiertos*.
  - **Authoritative fact, not heuristic.** A merged PR / closed issue is a
    **fact** for git-backed projects — a different signal from
    `delivery_candidate` (#22, which stays the heuristic for gitless projects).
    The two are never conflated.
  - **Contributor-agnostic (team latent).** All authors are summed at project
    level (owner + collaborators + agents); no per-person breakdown, and the
    `author` column is never surfaced — the model carries the team dimension
    latently for a future org-scale unit.
  - **All-time totals**, labelled as such in the UI (a tooltip) so they never
    read as window-scoped like the effort columns; they attach only to projects
    already visible in the window (no phantom rows).
  - **`github.db` read-only.** The join goes one direction only — the portfolio
    reads `github.db` (`mode=ro`); nothing writes back. `workspace.db` and
    `events.db` are untouched by the read.
- **Explicit folder-monitoring opt-in — `[workspace] filesystem_monitoring` flag
  (#27)** — folder monitoring (the Phase B filesystem watcher) was already opt-in
  (it runs only when a root is marked); this makes it an **explicit, discoverable
  toggle**. Default `true` (preserves current behavior). The flag gates **only**
  the filesystem watcher — agents, GitHub ingestion, and the whole portfolio
  (including the new outcome layer) stay on regardless. The `/portfolio`
  dashboard shows the folder-monitoring state (on/off) and how to enable it
  (read-on-load; no SSE change).

### Unchanged (invariants held)
- `delivery_candidate` (#22), the workspace resolver, the filesystem watcher
  logic, `events.db`, the `project` layer, and the SSE hot path are all
  untouched — this unit only **consumes** existing data. Zero new dependencies;
  zero-cloud; separate stores held; `hide_project_names` masks every new label.

## [1.14.0] — 2026-09-18

### Added
- **Portfolio Stage 2 — production-over-time view (#24)** — the `/portfolio` view now
  leads with an honest production chart: one **contribution strip per canonical project**,
  a cell per day of the window, shaded by the number of sessions that day.
  - **Effort, not duration.** Each session is a unit of work; duration is never used
    (resumed sessions carry original timestamps spanning months, and formats are
    inconsistent across providers). Per project the strip reports **session count + active
    days**.
  - **Honest ingestion dating.** Every session is dated by `MAX(events.created_at)` — the
    ingestion epoch, honest even for resumed sessions (shipped in #18) — bucketed to the
    **local** day (consistent with the #23 rollup timezone fix). `first_event_at` /
    `last_event_at` (original, misleading on resumed sessions) are not used.
  - **Canonical aggregation.** Sessions roll up over the Stage-1 canonical project
    (`workspace_classification.project_key`), so harness/scratchpad folders fold into their
    real project instead of surfacing as projects of their own.
  - **Multi-provider.** A project unites sessions across claude / codex / opencode / …;
    the strip is **colored by the day's dominant agent** (full per-provider breakdown on
    hover). Ordered by most-recent activity (hot on top).
  - **Window toggle** 4 days / week (7d) / month (30d) — read-on-load, re-fetch per range;
    per-row totals are scoped to the window (a session touching N projects counts in each,
    so totals are not additive across rows).
  - **Deliverables** = image/video artifact count (by extension) from `path_touches` (the
    filesystem watcher). It reads **0 until a root is marked** (`mool workspace root add`)
    and is surfaced honestly (never a silent zero).
  - **Surface** — new `get_portfolio_production(days)` MCP tool + read-layer helper, new
    `/api/workspace/portfolio/production` route. All reads are strictly read-only over
    `events.db` + `workspace.db`; `hide_project_names` masks chart labels exactly as it
    does the tables. Charts are hand-rolled **inline SVG** — zero dependencies.
  - **Additive** — the resolver (#20), watcher (#21), `events.db`, the `project` field,
    `delivery_candidate`, SSE and the Stage-1 classification are untouched (consumed only).
    **No client attribution (Stage 3)** — that stays in epic #24. A follow-up for a
    "cold projects" view (projects with no activity in the window) is tracked as **#25**.

### Fixed
- **Portfolio caret on leaf projects** — the expand caret (and its click/hover affordance
  and empty "Sin subdirectorios anidados" placeholder) no longer appear on projects with
  no nested children. Leaf rows keep an empty caret gutter so names stay column-aligned;
  only projects with children are expandable.

---

## [1.13.0] — 2026-09-18

### Added
- **Portfolio Stage 1 — collapse harness noise + hierarchical grouping (#24)** — the
  `/portfolio` view identified real *projects* instead of listing 376 flat folders. On a
  real machine ~49% of workspaces are agent-harness folders (scratchpad
  `/private/tmp/claude-*`, session storage `~/.claude/projects/`) whose names encode a
  real project. A new **read-layer classifier** (`hub/cache/portfolio_classifier.py`)
  maps every workspace into the epic's 4-category taxonomy and materializes it into an
  additive, **fully-rebuildable** `workspace_classification` table (contrast the durable
  `workspace_rollup`):

  - **A. Harness → collapse** onto the real project. Primary: the scratchpad embeds the
    session uuid, so the real `cwd` is read from `events.db` (read-only) — no decode.
    Fallbacks: exact match of the encoded segment against known real dirs
    (`encode_match`), then a filesystem-validated decode. The naive `replace('-','/')`
    decode is **lossy** (Claude encodes `/`, `_` and `-` all to `-`), so `coep-services`
    is never split into `coep/services`.
  - **B. Deep subdirs** and **C. materials/reports/exports** nest under their nearest real
    ancestor (the project anchor).
  - **D. Config dotfolders** de-prioritized (D1 nested config) or orphaned (D2 home-level
    dotfolders + degenerate/system roots → a collapsed "sin clasificar / herramientas"
    section).

  On the owner's real DB: **376 flat workspaces → 72 project groups; 181 harness folders
  collapsed** (125 via session `cwd`, 55 via encode-match, 1 unresolved), 27 unclassified.

  - **Surface:** new `get_portfolio_grouped` MCP tool + store method (harness activity is
    folded **at read time** — the rollup is never re-keyed), a `collapsed_harness` count
    per project (nothing is deleted — it is grouped), `hide_project_names` masking recurses
    into project labels and nested children.
  - **Dashboard:** `portfolio.html` renders projects with expandable nested children and a
    collapsible "sin clasificar" section (read-on-load; SSE untouched).
  - **CLI:** `mool workspace classify`, and `mool workspace portfolio --grouped`;
    classification is refreshed by `rollup`/`backfill`.

  Invariants honored: zero-dep, `events.db` read-only (verified zero writes), `workspace.db`
  additive; resolver (#20), watcher (#21), `project`, `delivery_candidate` and SSE untouched.
  No charts (Stage 2) or client attribution (Stage 3).

---

## [1.12.1] — 2026-09-18

### Fixed
- **Rollup day-bucketing now UTC for git (#23)** — the portfolio rollup folds three
  signals per `(workspace, day)`, but git commit times are stored **naive-local**
  (`git_store` migration 3) while session/filesystem days are UTC, so `build_rollup`'s
  raw `substr(timestamp,1,10)` dated each commit by its **local** calendar day — a commit
  near local midnight landed ±1 day off the filesystem/session activity in the same
  workspace. `_read_git_commits` now buckets each commit on its **`_parse_ts`-normalized
  aware-UTC** timestamp (`dt.date().isoformat()`), so all three signals share one clock.
  Session/filesystem bucketing, `git_store`, and `delivery_candidate` are untouched
  (quiescence already normalized via `_parse_ts`).
- **Note:** git `last_activity` is now emitted in **UTC-aware** form
  (`…T16:00:00+00:00`) instead of the old naive `…T10:00:00`, unifying all three signals
  on one clock. A dashboard column that renders it shifts visibly by the UTC offset —
  this is expected. Existing `workspace.db` rollups carry stale git rows from the
  local→UTC day remap and need a one-time surgical reconcile (zero `git_touches`, prune
  empty rows, re-run `mool workspace rollup`); see the `workspace_rollup` schema note.

---

## [1.12.0] — 2026-09-17

### Added
- **Workspace axis — Phase C: portfolio rollup + `delivery_candidate` (#22)** — the
  final phase of the Workspace axis (VISION §6). Turns the attributed path-touches into
  a **machine-wide portfolio** and adds a **local delivery signal** — completing the
  axis (A + B + C); only the deferred cross-machine Phase D (v2.x) remains.

  - **Signal-agnostic portfolio rollup** — a materialized projection over the workspace
    tree (`workspace_rollup` in the separate `workspace.db`, keyed by `(workspace, day)`,
    additive via `CREATE TABLE IF NOT EXISTS`). A node lights up whether the activity
    came from a **session** (`path_attributions`), the **filesystem** (`path_touches`),
    or **git** (`github.db` commits) — a genuine UNION over three differently-shaped
    sources, folded in at build time so the read surface stays a single-table query.
    Git is resolved via the same identity ladder as Phase A, so a repo root and a deep
    session/fs path collide on **one node**. Keyed `INSERT OR REPLACE`, never
    wipe-and-rebuild (the rollup is the only durable per-day filesystem record).
    Per-signal counts are reported separately (never summed — incommensurable units);
    each node reports which `sources` lit it.
  - **`delivery_candidate` (correlate)** — a **local, structural** guess that a
    workspace's work was likely delivered, surfaced as a **candidate with confidence,
    never as a bare "done" flag**. Quiescence is a **precondition only** (indistinguishable
    from a break), measured against the **real clocks** (`path_touches.last_seen`, git
    commit times, session `ended_at`/`last_event_at` from #16 — read-only, never the
    backfill-pinned `path_attributions.first_seen`). A row is written only when a
    **second co-occurring signal closed the burst**, and the firing signal is **recorded
    per row** (auditable): `session_close` (real `ended_at`), `git_commit`, or
    `root_artifact` (a new file at a git workspace's root with an extension outside the
    working set). **No LLM** — a structural signal, not an inference. Three timestamp
    formats (git stores naive-local, sessions use `Z`, filesystem uses UTC offset) are
    normalized to aware UTC before any comparison.
  - **Dashboard — first frontend surface of the axis** — a **read-on-load** portfolio
    view (`/portfolio` + `/api/workspace/portfolio` and `/api/workspace/delivery`) that
    queries `workspace.db` on load. **The SSE schema is untouched** (no API-version bump)
    and the events hot path is not affected.
  - **CLI** — `mool workspace rollup` (rebuild rollup + run the detector),
    `mool workspace portfolio`, `mool workspace delivery`.
  - **MCP** — new read-only tools `get_portfolio`, `get_workspace_activity`,
    `get_delivery_candidates`. All new read surfaces pass through
    `_mask_workspace_rows` (and `root_artifact` paths are additionally masked), so
    `hide_project_names` keeps working on the new views.
  - **Untouched by construction:** the `project` field, the MCP session contract,
    `linker.py`, and `events.db` (opened read-only) — no changes. Zero new dependencies.

### Known limitations
- **Rollup day-bucketing timezone edge (#23)** — the per-day rollup buckets each source
  on its raw ISO date string, so git days remain in local time while session/filesystem
  days are in UTC. This produces a ±1-day edge effect for commits near local midnight.
  Quiescence detection is unaffected (it normalizes all timestamps to UTC first).
  **Resolved in [1.12.1]** — git is now bucketed on UTC via `_parse_ts`.

---

## [1.11.0] — 2026-09-17

### Added
- **Workspace axis — Phase B: filesystem watcher (#21)** — makes a project visible even
  when **no agent and no git** ever touched it: a materials-gathering folder, a non-CLI
  tool whose output is only observable by path (e.g. Pencil `.pen` files), or
  non-software work. This is the *portfolio half* of the Workspace axis (VISION §6) — it
  observes the **work**, not just the sessions — completing what Phase A began.

  - **Filesystem watcher** (`hub/watchers/workspace_watcher.py`) — a pure-stdlib,
    standalone watcher (it never imports `EventStore` or the SSE hot path) that mirrors
    the session-watcher `discover → cursor → emit → sleep` loop. **Bounded recursive
    scan + per-root mtime cursor** — no watch-per-file, so there is **no
    file-descriptor ceiling**. The cursor is anchored to `scan_start − 1s` (not
    `max(mtime)`), so a file written mid-scan is re-seen next cycle instead of being
    dropped. First cycle reads everything under the root — that *is* the backfill.
  - **Opt-in marked roots (privacy by design)** — with no roots configured the watcher
    observes **nothing**. Root `/` is valid (autonomous-agent server) but never a
    default. **Containment = correctness:** built-in default excludes (VCS internals,
    dependency dirs, build outputs, OS/cloud sync + cache folders) are pruned in-place
    during the walk, plus per-root extra excludes and a `max_depth` bound. Symlinked
    directories are not followed (loop-safe).
  - **`path_touches` + `fs_cursors` in the separate `workspace.db`** — additive tables
    (created via `CREATE TABLE IF NOT EXISTS`, so they land on existing v1.10.0
    databases with no migration). Each touch is resolved to its owning workspace via the
    Phase A resolver (#20) and upserted on `path` (self-healing: if a directory later
    gains a `.git`, the same path re-attributes to the git workspace). **Zero new writes
    to `events.db`** — the events hot path / SSE stay untouched by construction.
  - **CLI** — `mool workspace root {add,list,remove}` (manage marked roots) and
    `mool workspace touches <workspace_key>` (filesystem touches per workspace).
  - **MCP** — new read-only tool `get_workspace_touches`; `list_workspaces` and
    `get_session_workspaces` gain a filesystem-`touches` count. All honor the new
    `hide_project_names` privacy flag (masks the visible label while keeping the
    `workspace_key` join handle stable).

  **No change to what shipped:** the existing `project` field, the existing MCP contract,
  `events.project`, `linker.py`, and the Phase A resolver (#20) are all **unchanged** —
  Phase B *consumes* the resolver, it does not modify it.

## [1.10.0] — 2026-09-17

### Added
- **Workspace axis — Phase A: `path → workspace` resolver (#20)** — recovers correct
  multi-project (M:N) attribution of agent work by mapping each absolute
  `events.file_path` to the project that *owns the file*, rather than to the session's
  directory name. A session in `~/work` editing `~/work/repo-a/x.py` and
  `~/work/repo-b/y.py` now lights up **two** workspaces — impossible before, when one
  event carried exactly one `project`. This is the *recovery half* of the Workspace
  axis (VISION §6): correct attribution of *agent* work over data MoolMesh already
  persists — **not** the full portfolio, which awaits the Phase B filesystem watcher.

  - **Identity ladder `git-remote → git-root → path-hash`** (`workspace_resolver.py`) —
    resolves each path to its closest project marker with a stable identity that
    survives rename, move, or clone, and that still exists when there is no git at all.
    `.git/config` is parsed by hand (Python's `configparser` mis-reads git's
    tab-indented keys); **no `git` subprocess is ever spawned**.
  - **Separate `workspace.db`** (`workspace_store.py`, `~/.moolmesh/workspace.db`) — a
    distinct store per invariant §2.4, with additive run-once migrations and
    self-healing upserts. `backfill_from_events` opens `events.db` **read-only**
    (`mode=ro`) and **never writes to it**; the hot path / SSE stay untouched.
    **Absolute paths only:** the backfill attributes absolute `file_path` rows and
    **skips non-absolute ones** (for those, `events.file_path` holds Bash command
    strings, not paths), reporting the skipped count rather than guessing.
  - **CLI** — `mool workspace {backfill,list,session,sessions}`.
  - **MCP (3 new read-only tools)** — `get_session_workspaces`,
    `get_workspace_sessions`, `list_workspaces`. Each is guarded to return `[]` when
    `workspace.db` is absent, so the tools are safe to call before any backfill.

  **No change to what shipped:** the existing `project` field (derived from the session
  directory and consumed by agents over MCP), the existing MCP contract,
  `events.project`, and `linker.py` are all **unchanged**. The resolver is purely
  additive and reads `events.db` read-only.

## [1.9.0] — 2026-09-17

### Added
- **Ingest-based `last_activity_at` on session export** — `get_session_detail` now
  returns `last_activity_at`, the ISO-8601 UTC time of the most recent ingest for
  the session (`MAX(events.created_at)`). Unlike `last_event_at` — which carries the
  original message timestamp and can be days old in a *resumed* session
  (`claude --resume`) — this field is monotonic and reliably populated, so age-based
  tooling (e.g. stall monitors) no longer misclassifies a freshly-resumed session as
  abandoned. `last_event_at` is unchanged; the new field sits beside it.
- **Per-event ingest epoch on session export** — `get_session_events` now includes
  `created_at` (the raw ingest epoch, `events.created_at`) on each event, letting
  consumers reason about ingest recency independent of the original message timestamp.

  Note the deliberate type asymmetry: the session-level `last_activity_at` is a
  formatted ISO-8601 string (consistent with the other session timestamps), while the
  per-event `created_at` is the raw epoch float. For large sessions, the export's event
  list is capped (default 500), so `last_activity_at` may be more recent than the max
  `created_at` among the exported events — this is correct: it reflects the whole
  session's latest ingest.

### Changed
- **`session.is_active` now means "no end observed", not "alive" (#16)** — the field flips to `false` only on a terminal signal *observed in the session file*, never inferred from event recency (per #16 ↔ #22). Today the sole terminal signal is a Claude `/exit` local-command. Consumers building a stall-monitor should read `is_active: false` as "session end was observed"; a `true` value means only that no end has been seen yet. The four other providers (Codex, Qwen, OpenCode, Cursor) are unchanged and continue to report `is_active: true`. Two additive columns record *why/when* a session ended — `ended_at` and `ended_reason` (e.g. `"exit_command"`) — both exposed via `mool export --format json` and MCP `get_session_detail`.

### Fixed
- **`session.first_event_at` frozen empty (#16)** — when a session's first observed entry carried an empty timestamp (common on Claude summary/meta lines), `first_event_at` stayed `""` forever because `ON CONFLICT` only refreshed `last_event_at`. Empty timestamps are no longer persisted, and `first_event_at` now backfills to (and keeps) the earliest valid timestamp once one arrives.
- **Tool results no longer classified as user messages (Claude)** — on disk, tool
  outputs ride inside `role="user"` entries as `tool_result` content blocks. The
  Claude adapter's `_map_role` only detected `tool_result` under the `assistant`
  branch, so these entries were ingested as `event_type="user"`, conflating "a human
  typed something" with "a tool returned output". The `user` branch now mirrors the
  assistant predicate: a `user` entry whose content is a `tool_result` block with no
  text maps to `tool_result`; a `user` entry with real text still maps to `user`.
  This makes the user-message analyzer, the CLI digest, and MCP `search_events`
  with `event_type="user"` return a smaller, honest set. Tool-result events carry
  `tool_name=None`, so they remain excluded from tool stats (`tool_name IS NOT NULL`).
  **Not retroactive:** the fix applies to new ingestion only; already-stored events
  are not reclassified (no backfill). Claude-only; other providers unchanged.

---

## [1.8.5] — 2026-09-14

### Fixed
- **Codex watcher crash on None fields** — `event_msg_text`, `text`, and `reasoning_text` could be `None` in certain Codex events, causing `AttributeError` on `.strip()` that killed the watcher thread permanently.
- **Codex `function_call_output` with list payload** — Codex can emit `output` as a list of content blocks instead of a string. The parser now normalizes it to string, fixing `sqlite3.ProgrammingError: type 'list' is not supported`.
- **Ghost `[user input]` events from Codex** — internal protocol signals (`item_completed`, `thread_settings_applied`) and empty user messages were being stored as `[user input]` with no useful content. Now filtered out.
- **Watcher thread resilience** — `_harvest_file` only caught `OSError`; any other exception (e.g., `AttributeError`, `ProgrammingError`) killed the watcher thread for the rest of the session. Now catches all exceptions, logs them, and continues.

---

## [1.8.4] — 2026-07-24

### Added
- **MCP pagination: `offset` parameter** — `get_session_events`, `get_recent_events`, and `search_events` now accept `offset` for cursor-based pagination. Previously, agents hitting sessions with 100+ events had no way to reach events beyond the first page.
- **MCP event ordering: `order` parameter** — `get_session_events` now accepts `order="desc"` to return newest events first. Agents reviewing long sessions can get the latest activity in a single call instead of paginating from the start.

---

## [1.8.3] — 2026-07-13

### Fixed
- **CI sync-assets failing on release** — `pip install -e .` didn't install dev dependencies, so `pytest` was missing in the runner. Changed to `pip install -e ".[dev]"`.

---

## [1.8.2] — 2026-07-13

### Fixed
- **`IncompleteRead` crash loop on large repos** — `GitHubClient._request()` now catches `http.client.HTTPException` (covers `IncompleteRead`, `RemoteDisconnected`, `BadStatusLine`). Previously only caught `URLError`/`OSError`/`TimeoutError`, causing unhandled exceptions every 15 seconds for repos with large API responses.
- **Same except gap in `OpenAICompatClient`** — `chat()` and `is_available()` now also catch `HTTPException`.
- **`json.loads` on truncated body** — `rest_get()` and `graphql()` now handle `JSONDecodeError` from partial/corrupt response bodies.
- **`resp.read()` failure after successful `urlopen()`** — separated into its own try/except so a truncated body returns `(0, {}, b"")` instead of an unhandled exception.
- **Log spam on persistent GitHub API errors** — `GitHubHarvester` now uses per-repo error counters with backoff: full traceback on first failure, one-line summary on consecutive identical errors, reset on success.

### Added
- **Retry with backoff in `_request()`** — up to 2 retries with 1s/2s backoff for transient network errors. HTTP 4xx/5xx are not retried.
- **Pagination in `list_issues()`** — follows GitHub's `Link` header for up to 10 pages (1000 issues). ETag conditional applies only to page 1 (sort=updated desc: if page 1 unchanged, nothing changed).

---

## [1.8.1] — 2026-07-06

### Fixed
- **Daemon crash on Windows due to cp1252 encoding** (#14) — ASCII art banner uses Unicode block characters that `cp1252` can't encode. Daemon subprocess now runs with `PYTHONIOENCODING=utf-8`.
- **Stale PID file blocking all daemon commands on Windows** (#15) — `read_pid()` now catches `OSError` (raised by `os.kill(pid, 0)` on Windows for certain stale PIDs).

---

## [1.8.0] — 2026-07-06

### Added
- **Windows daemon support** — `mool daemon start` now works on Windows via `subprocess.Popen` with `CREATE_NO_WINDOW`. Same commands on all platforms: `start`, `stop`, `status`, `restart`.
- **`uv` as recommended Windows installer** — `uv tool install moolmesh` handles PATH and MCP deps automatically.

### Changed
- Quick Start updated to recommend `mool daemon start` over `mool dashboard`.
- Windows section in README now lists `uv` as primary install option.

---

## [1.7.4] — 2026-07-06

### Fixed
- **`mool mcp setup` failing on `uv` environments** — when `uv` is available, skip the `mcp` package check entirely since `uv run` resolves PEP 723 inline deps automatically. Previously demanded `pip install mcp` which doesn't exist in uv-managed venvs.

---

## [1.7.3] — 2026-07-06

### Fixed
- **`mool --version` showing stale version** — now reads from `importlib.metadata` instead of a hardcoded string in `hub/__init__.py`.

---

## [1.7.2] — 2026-07-05

### Fixed
- **Codex parser field mismatch** — reading `payload.content` instead of `payload.message`, causing blank user messages in reports.
- **Codex event subtype classification** — all 11 `event_msg` subtypes now classified correctly (were all treated as USER).
- **Cursor sessions silently discarded** — added Cursor parser/adapter to batch reporter.
- **Windows path handling** — centralized with `_normalize_path_str()` and `_split_path()`, fixing 8 locations that assumed Unix `/` separators. Strips `\\?\` Extended-Length Path prefix.
- **Daemon graceful degradation on Windows** — uses `taskkill` instead of `signal.SIGKILL`, catches `ConnectionAbortedError` in SSE handler.
- **PID file encoding** — added `encoding="utf-8"` to read/write.
- **WAL pragma defensiveness** — try/except on `PRAGMA journal_mode=WAL` for exotic filesystems.

### Changed
- README: `pipx install` as primary method, PEP 668 note, Windows section with PATH troubleshooting, dual-OS venv instructions.
- Added Python 3.14 classifier to `pyproject.toml`.

### Removed
- Dead code: `KqueueWatcher`, `PollingWatcher`, and orphaned test.

---

## [1.7.1] — 2026-06-29

### Added
- **`mool mcp setup` — universal MCP client configuration** for all major AI agents: Cursor (`mool mcp setup cursor`), Codex (`mool mcp setup codex`), Qwen (`mool mcp setup qwen`), OpenCode (`mool mcp setup opencode`). Each uses its native config format.

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

## [1.6.3] — 2026-06-29

### Fixed
- **`mool mcp setup` idempotency** — handles existing registrations by removing before re-adding instead of failing. Detection reads `~/.claude.json` directly (user scope only).

---

## [1.6.2] — 2026-06-29

### Added
- **`mool mcp setup`** — cross-platform MCP server configuration command. Auto-detects install method (pipx/pip/source) and OS. Supports `claude-code`, `claude-desktop`, and `json` targets. `--install-deps` auto-installs `mcp` package, `--dry-run` previews changes.

---

## [1.6.1] — 2026-06-26

### Fixed
- Dynamic version display in dashboard and landing page.
- Token-conscious limits on MCP server tools (`get_sessions`, `get_active_sessions`, `get_session_events`).
- `text_mode` parameter for `get_session_events`: `none` | `snippet` | `full` (replaces `include_full_text` boolean).

### Changed
- SVG banner replaces static PNG.

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

## [1.4.3] — 2026-06-24

### Fixed
- **systemd compatibility** — `mool daemon start` auto-detects process supervisors (`$INVOCATION_ID`, `$NOTIFY_SOCKET`) and stays in foreground instead of double-forking. `Type=simple` systemd services now work correctly. (#1)
- **Global install docs** — added "Production Install" section with `pipx install moolmesh` as recommended method and systemd unit file example. (#2)

---

## [1.4.2] — 2026-06-23

### Added
- **`mool query`** — 6 JSON-output subcommands (`events`, `sessions`, `tokens`, `tools`, `search`, `project`) for agents without MCP.
- **`--json` flag** on `status` and `discover` for machine-parseable output.
- **Enhanced `mool status`** — shows live port, events count, and monitored repos from running daemon.

### Fixed
- **Auto port fallback** — if the default port is busy, auto-increments up to 10 tries.
- **Running instance detection** — checks `/health` before trying another port; informs user if MoolMesh is already running.

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
