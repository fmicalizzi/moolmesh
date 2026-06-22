# MoolMesh Philosophy

## On the Name

**Mool** comes from Mayan languages, meaning *to congregate* -- to gather what is
scattered. **Mesh** is an interwoven net of connections.

MoolMesh is the gathering of scattered signals into a coherent web of meaning.

---

## The Problem

Modern software development increasingly relies on AI coding agents. A single
developer may run Claude Code, Codex, Qwen, and OpenCode in the same afternoon,
across multiple repositories. Each agent generates its own stream of events --
tool calls, token consumption, file modifications, reasoning traces -- but none
of them talk to each other, and none of them present a unified picture to the
human holding the reins.

The result is a blind spot at the center of the workflow. The developer cannot
answer basic questions: *How many tokens did I burn today? Which agent touched
this file last? Did the refactor agent undo what the bug-fix agent just did?*

MoolMesh exists to close that blind spot.

---

## The Dual Axiom

MoolMesh serves two masters simultaneously, and refuses to sacrifice either.

### Agent-First

MoolMesh is a central nervous system for agents. It exposes runtime state
through the Model Context Protocol (MCP), so an AI can query, audit, and
supervise other AI. An orchestrator agent can inspect what an executing agent
has done, what it spent, and whether its actions conflict with other work in
progress.

### Human-First

For the human, the agent must never be a black box. MoolMesh provides
crystal-clear, real-time observability. It correlates token spend and tool
calls with real-world impact: git commits, kanban tickets, modified files.
The human sees cause and effect, not just a stream of completions.

Neither axiom overrides the other. A system that serves agents but obscures
their work from humans is dangerous. A system that serves humans but cannot
be queried by agents is a bottleneck. MoolMesh holds both in tension.

---

## The Interaction Matrix

MoolMesh recognizes four interaction surfaces, each with distinct requirements.

**Agent to Agent (A2A) -- Supervised Autonomy.**
An orchestrator agent monitors executing agents through the MCP interface.
Conflicts are detected early. Resources are shared, not duplicated.

**Human to Agent (H2A) -- Trust Through Visibility.**
Deep telemetry empowers the developer. When you can see exactly what an agent
did, why it did it, and what it cost, trust becomes a function of evidence
rather than faith.

**Human to Human (H2H) -- Project Pulse.**
A unified timeline of who did what, when -- human or AI. The team gets a single
narrative of the project's evolution, regardless of whether a commit came from
a person or an agent.

**Team to Swarm -- Scalable Workflows.**
As teams adopt multiple concurrent agents, MoolMesh provides a central,
auditable registry. The transition from one-developer-one-agent to
hyper-productive multi-agent architectures requires infrastructure that
scales without losing accountability.

---

## Architecture Principles

### 1. State Is the Single Source of Truth

Critical data lives in SQLite, not in volatile caches or ephemeral memory.
If it matters, it is persisted. If it is persisted, it is queryable.

### 2. Radical Agnosticism

MoolMesh has no corporate loyalties. It unifies telemetry from any provider
that emits observable events. Adding a new agent is an adapter, not a
rewrite. The tool serves the developer, not the vendor.

### 3. Zero Cloud Lock-in

MoolMesh runs locally. Python standard library and SQLite -- no accounts to
create, no API keys to manage, no data leaving the machine. Privacy is not
a feature toggle; it is the architecture.

### 4. Zero Friction

Session discovery is automatic. Point MoolMesh at your workspace and it
finds active sessions across providers. No configuration files to author,
no daemons to babysit. If setup takes more than a minute, something is wrong.

---

## The Promise

A developer's work emerges from three streams: the human mind that decides
direction, the repository history that records outcomes, and the AI reasoning
threads that connect intent to action.

Today those streams run in parallel, rarely touching. MoolMesh is the context
mesh where they converge -- a single surface where scattered signals become
shared understanding.
