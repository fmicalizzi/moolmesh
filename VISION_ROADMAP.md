# MoolMesh — Vision & Roadmap

> The strategic north for MoolMesh: where the product stands today and the path
> forward. Read [`PHILOSOPHY.md`](PHILOSOPHY.md) first for the *why*; this document
> is the *where to* and *in what order*. [`ROADMAP.md`](ROADMAP.md) remains the
> tactical release log; this document is the layer above it.

---

## 1. What MoolMesh actually is

MoolMesh is **an observer that evolves into a coordinator.** The observation is
not the end — it is the *substrate*. Every session captured becomes shared memory
that an agent (or a human) can read to continue, supervise, or reconcile work that
happened elsewhere.

This is already true in practice, not aspiration:

- An agent calls `get_session_chain` or `search_session_content` to pick up what a
  previous session did and continue from there.
- An orchestrator inspects `get_session_detail` to see what an executing agent
  spent and touched before deciding the next step.

That **is** coordination — asynchronous, memory-mediated coordination. The
`PHILOSOPHY.md` names it directly: the **A2A (Agent-to-Agent) surface** of the
Interaction Matrix, *"an orchestrator agent monitors executing agents through the
MCP interface."* The product simply hasn't been organized around that truth yet.
This document does that.

---

## 2. The maturity ladder

MoolMesh matures along one spine. Each rung is built on the one below; none
replaces the observation base.

```
   OBSERVE      Capture every agent's session as unified events.
      │         Human sees the dashboard; agents query via MCP.
      ▼
   CORRELATE    Connect sessions: chains, branches, shared context.
      │         "What did the other session do? Continue from there."
      ▼         (async / memory-mediated coordination)
   COORDINATE   Active supervision: conflict detection, resource sharing,
                the live mesh. "Did agent A undo what agent B just did?"
```

| Rung | State today | What it means |
|------|-------------|---------------|
| **Observe** | **Built.** 5 providers, live SSE dashboard, MCP query surface, analytics, Project Pulse, Code Timeline. | The blind spot is closed: unified events across agents, persisted in SQLite, queryable by human and machine. |
| **Correlate** | **Partially built & live.** Session metadata, full-text storage/search, cross-session linking (`link`, `detect-links`, `chain`, `get_session_chain`), git-branch correlation. | Async coordination already works through the MCP layer. The signals powering it are still mostly heuristic (see §4). |
| **Coordinate** | **Next horizon.** | Turn passive correlation into active supervision: detect conflicts between concurrent agents, surface "who touched this last", make the mesh legible in real time. |

The strategic goal is not "more dashboards." It is to climb this ladder while
keeping the observation base zero-friction and read-only.

> The Observe base is gaining a **second axis** beyond the agent session: the
> **Workspace** (folders/projects as a first-class unit, beyond what an agent touches).
> See §6. Not to be confused with the dashboard's existing "Project Pulse" view.

---

## 3. Two workstreams feed the ladder

Progress comes from two independent workstreams. Neither is "the plan" alone.

### Breadth — more sources observed
Breadth has **two branches**. The first, *more agents*: every new provider widens the
mesh; adding one is an adapter, not a rewrite (the quartet
`model → parser → adapter → watcher`, ~300–500 LOC, no core changes), with diminishing
return — the 6th provider matters less than making the first 5 coordinate well. The
second, *the Workspace axis* (§6): observing the folder/project as a first-class source,
beyond the session. It honors radical agnosticism more literally than the agent pipeline
—a folder *is* a source of observable events— and its filesystem floor **lowers the cost
of all future breadth**: an unparsed agent still leaves an output trace.

### Depth — richer coordination
Sharper correlation and, eventually, active coordination. This is where the
product's identity lives. Depth is where the differentiated value is, and it is
currently under-invested relative to breadth.

> **Principle for sequencing:** when breadth and depth compete for the same slot,
> prefer depth unless a specific provider unblocks a concrete user. Widening a mesh
> that doesn't yet coordinate well is investing in surface over substance.

---

## 4. Near-term: harden the Observe base (session lifecycle fidelity)

The coordination ladder is only as trustworthy as the events under it. Three
correctness gaps in the Observe/Correlate base are the highest-priority work,
because everything above inherits their errors. They map to the open issues.

### 4.1 Session lifecycle — start, active, close (issue #16)
Today "active" is inferred two ways that disagree:
- `sessions.is_active` is set to `1` on upsert and **never returns to `0`** — so
  every session looks active forever.
- `get_active_sessions` ignores that flag and uses a **time-window heuristic**
  (events in the last N hours).

MoolMesh has no real *end-of-session* signal. It infers liveness from event
recency, not from whether the underlying agent is still running.

**What we borrow from the tmux-bridge pattern (evaluation ongoing, see §7):**
its discipline around *knowing a pane's process is alive* — process/identity
detection and a `doctor`-style connectivity check. Adapted to MoolMesh's
read-only, file-based model, this becomes: derive session close from concrete
signals (session file no longer being appended + provider-specific end markers +
a bounded idle timeout) rather than assuming perpetual activity. A session should
have an honest lifecycle: `starting → active → idle → closed`.

### 4.2 Event fidelity — tool results vs user messages (issue #17)
Distinguish a tool *result* from a genuine user message with a dedicated
`tool_result` event type. Correlation and any future conflict detection depend on
reading the event stream accurately; conflating these corrupts every rung above.
Moreover, a clean `tool_result` is a **prerequisite for the `path → workspace` resolver**
(§6): without reliable file paths there is no honest project attribution.

### 4.3 Timestamp honesty on resumed sessions (issue #18)
Expose ingest / last-activity timestamps distinctly from original event
timestamps. Resumed sessions carry original timestamps, which distorts "what
happened when" — the backbone of correlation and chains.

**These three come first.** They are cheap, they are hygiene, and they make the
whole ladder trustworthy.

---

## 5. Provider pipeline (the Breadth workstream)

A single forward-looking pipeline. Ordering favors providers that are (a) low
effort and (b) unblock real coordination use cases, over exotic ones.

| Provider | Storage / discovery | Effort | Notes |
|----------|--------------------|--------|-------|
| **Aider** | `~/.aider/history/` (text + SQLite metadata) | Low | Best-documented format; lowest-friction next provider. |
| **Pi** | JSONL tree (`~/.pi/agent/sessions/`) | Low–Med | Tree sessions need leaf-to-root linearization in the parser. |
| **Goose** | SQLite (`sessions.db`) + legacy JSONL | Med | Cross-platform paths; `ccusage` is a mapping reference. |
| **Copilot CLI** | Local logs | Med | Format needs confirmation. |
| **Hermes** | SQLite WAL + FTS5 (`~/.hermes/`) | Med–High | Autonomous agent; parent-session chain must be stitched. |
| **Odysseus** | SQLite in Docker (`./data/app.db`) | High + recon | Autonomous agent; **schema undocumented — blocked on external schema extraction.** |
| **Paperclip** | PostgreSQL / PGlite | High, new pattern | Control plane, not a single agent. Would require a REST/SSE harvester — **the first network-based provider, which pressures the zero-dependency principle.** Defer unless demand is real. |

**Owner decision (open):** which of these enter the next wave, and in what order.
The recommendation above is *low-effort first, autonomous agents once their schemas
are confirmed, Paperclip only on real demand*. Autonomous agents (Hermes,
Odysseus) additionally depend on external schema confirmation and must never block
the rest of the pipeline.

**Enabler before scaling breadth:** a provider template + contributor guide +
auto-detection, so a provider can be added by writing only its quartet — never by
editing the core, dashboard, or MCP server. This makes "radical agnosticism"
(PHILOSOPHY §2) verifiable rather than aspirational, and pays for itself at the
next provider.

---

## 6. The Workspace axis — observe the work, not just the sessions

So far the Observe base has *one* spine: the **agent session**. But the root project
MoolMesh grew out of (`ai-session-analyzer`) was already *project-first*: it attributed
every session to its folder/project with a three-rule engine (`cwd → paths → time`).
MoolMesh, reorganizing around the session, demoted the project to a decoration. The
**Workspace axis recovers that root** and adds the one piece that never existed:
observing the folder *directly*, without depending on an agent to self-log.

**The atom: the path-touch.** The unit of observation is `(path, timestamp, source)`.
Three sources emit it and **none is privileged**: sessions (the file paths in their
`tool_use`, already persisted in `events.file_path`), the filesystem (mtime deltas), and
git (each commit's files). A **`path → workspace` resolver** attributes every touch to
its project, with a stable identity ladder (`git-remote → .git root → path hash`) that
survives rename, move, or clone, and that exists even with no git.

**Vocabulary (don't break what shipped).** The existing `project` field — derived from
the session's directory name and already consumed by agents over MCP — **stays
untouched**. The new attribution (file-owner) lives under the name **`workspace`**, in
its own columns and store. Two deliberately distinct notions: a session in `~/work`
editing `~/work/repo-a/x.py` is `project="work"` and `workspace="repo-a"`.

**Why it matters:** with the path-touch, a session touching three subfolders produces
activity in three workspaces (M:N, impossible today: one event = one `project`); a
project with no session still lights up from raw filesystem touches; and the
**portfolio** is the aggregation over the workspace tree, agnostic to which signal lit
it. This is what neither MoolMesh, nor its root, nor the state of the art does today:
seeing the folder *before/without* an agent or git — materials gathering, design work
with opaque files, an agent that isn't a standard CLI.

**Two tiers of observation.** This redefines Breadth (§3): a **universal floor** (the
filesystem, zero parser: "something happened here, in this workspace, now") + a
**per-provider enrichment** (the session parsers: Q&A, tokens, cost). A never-seen agent
gets output visibility for free; the full quartet is invested only when rich metrics are
wanted.

**Phased plan** (a multi-release arc, not a single release):

| Phase | What | Cost / honest scope |
|-------|------|---------------------|
| **A — Resolver** | `path → workspace` over the events we **already** have (`file_path`/`cwd` persisted). Recovers the root's engine, live. **Does not touch `linker.py`.** | Low. Delivers correct multi-project attribution of *agent* work — the *recovery half*, **not** the full portfolio. |
| **B — Filesystem** | The folder watcher (the new piece). Marked roots + bounded scan + excludes. **Its own store (`workspace.db`)** to avoid contending with the `events.db` hot path/SSE; the resolver reads `events.db` read-only. | Medium. Pure stdlib. Closes the "folder with no agent" gap. Only here is the portfolio complete. |
| **C — Portfolio** | Machine-wide rollup projection + `delivery_candidate` (correlate). | Medium. The differentiating depth. |
| **D — Cross-machine** | Opt-in multi-user aggregation (Wakapi-style split: local capture untouched + optional self-hosted server), keyed by `git-remote`. Distributed A2A surface. | Deferred. On the map, not committed. |

**Honesty discipline** (inherits from §4.1): `delivery_candidate` is surfaced as a
*candidate with confidence*, never as fact — it requires a second co-occurring signal (a
new artifact at the root / a tag or commit / a session close). Quiescence alone is
indistinguishable from a break.

**Containment and privacy = correctness:** discovery is **marked parent roots** (opt-in
on a workstation; root `/` valid on an autonomous-agent server, where the whole machine
is the work), with default excludes (VCS internals, deps, build, sync folders) and
`max_depth`. This is whole-machine metadata: opt-in roots and exclusions are part of the
design, not an add-on.

**Dual Axiom, held:** the human sees the portfolio (supervision at the *work* level); the
agent queries over MCP which workspaces are hot and who is touching them (conflict
avoidance). Any piece whose value is human-productivity only — e.g. an ActivityWatch-style
*time/attention* lens — stays **out of the core**: it breaks the Dual Axiom and is the
largest privacy surface.

---

## 7. Open question — synchronous coordination

There is a second mode of agent coordination beyond the async/memory-mediated one
MoolMesh already does: **live, synchronous messaging** — agents talking to each
other in real time while they work (the model exemplified by tmux-bridge-mcp,
which turns tmux panes into an inter-agent message bus with structured envelopes:
`from / to / correlationId`).

**Status: under evaluation, not a committed direction.** Being field-tested
separately. For now we treat it as:

1. **A source of tactical patterns** we can adopt into the Observe/Correlate base
   today — foremost the session-lifecycle detection in §4.1, and later the
   structured message envelope as a *deterministic* signal for cross-session
   linking (replacing today's temporal heuristics with "who actually messaged
   whom").
2. **A possible future observed source** — MoolMesh could ingest a live-bus's
   traffic as events, making synchronous coordination *legible* without MoolMesh
   itself becoming write-capable.

What we are **not** deciding yet: whether MoolMesh should ever *drive* live
coordination (become write-capable). That would trade against the read-only
architecture and needs the field evaluation to conclude first.

---

## 8. Invariants (from PHILOSOPHY §Architecture Principles)

These hold across everything above:

1. **State is the single source of truth** — SQLite, persisted, queryable.
2. **Radical agnosticism** — any provider that emits observable events; adding one
   is an adapter, not a rewrite.
3. **Zero cloud lock-in** — Python stdlib + SQLite, everything local.
4. **Zero friction** — automatic session discovery; setup under a minute.
5. **Read-only observation base** — MoolMesh reads session files, never mutates
   them. Any write/coordination capability is a deliberate, separately-decided
   step, not a drift (see §7).
6. **Human-First & Agent-First held in tension** — never serve one by blinding the
   other.

---

## 9. What needs an owner decision

| # | Decision | Recommendation |
|---|----------|----------------|
| A | Order of the near-term hygiene work (§4) vs first new provider | Hygiene first (#16 → #17 → #18); it de-risks every rung. |
| B | Which providers enter the next breadth wave, and order (§5) | Low-effort first (Aider, Pi, Goose); autonomous agents after schema confirmation; Paperclip only on demand. |
| C | Build the provider enabler (template + auto-detection) before or after the next provider | Before — it pays off at provider #2. |
| D | Synchronous coordination (§7) — pattern-source only, or a declared roadmap rung | Pattern-source only until the field evaluation concludes. |
| E | The Workspace axis (§6): a Breadth branch in MoolMesh or a separate project? | A Breadth branch inside MoolMesh (Phases A–C); reuses dashboard/MCP/SSE, with its own store (`workspace.db`). |
| F | Sequencing the Workspace axis vs the next agent provider | After hygiene (§4); alongside or before the 6th provider (depth > breadth). Start with Phase A — cheap, over already-persisted data. |
| G | Cross-machine dashboard + self-hosted server (Workspace Phase D) | On the map, deferred. Require only that the Workspace schema is born aggregation-ready (`git-remote` identity). |

---

*This is the strategic layer. Tactical release detail lives in `ROADMAP.md` and
`CHANGELOG.md`. When they diverge from this document, this document states the
intent and they state the state.*
