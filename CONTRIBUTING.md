# Contributing to MoolMesh

Thank you for your interest in contributing to MoolMesh. This guide covers everything you need to get started.

MoolMesh unifies telemetry from AI coding agents into a local hub. It gives developers a single pane of glass over sessions from Claude Code, Codex, Qwen, OpenCode, and any other provider — all running locally, all stored in SQLite.

Before contributing, please read [PHILOSOPHY.md](PHILOSOPHY.md) to understand the principles that guide every design decision.

## Prerequisites

- Python 3.11 or later
- Git
- A Unix-like shell (macOS, Linux, or WSL on Windows)

## Development Setup

1. **Fork and clone** the repository:

   ```bash
   git clone https://github.com/<your-username>/moolmesh.git
   cd moolmesh
   ```

2. **Create a virtual environment**:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install in editable mode** with dev dependencies:

   ```bash
   pip install -e ".[dev]"
   ```

4. **Run the test suite** to confirm everything works:

   ```bash
   pytest tests/
   ```

   The suite currently contains 509 tests. All of them should pass before you start making changes.

5. **Try the CLI** to get a feel for the tool:

   ```bash
   mool dashboard
   mool report
   ```

## Project Structure

- `hub/` — The core Python package. All runtime code lives here.
- `tests/` — Test suite. Mirror the `hub/` structure when adding new test files.
- `PHILOSOPHY.md` — Design principles and architectural rationale.
- `CHANGELOG.md` — Release history.

## Making Changes

### Branching

Create a feature branch from `main`:

```bash
git checkout -b feat/your-feature-name
```

### Coding Standards

- **Zero external dependencies.** MoolMesh is built on Python's standard library and SQLite. Do not introduce third-party packages. If you think an exception is warranted, open an issue first to discuss it.
- **SQLite backward compatibility.** Schema migrations must be additive. Never drop columns or rename tables without a migration path that preserves existing data.
- **Keep it local.** MoolMesh runs entirely on the user's machine. No network calls, no cloud services, no phoning home.

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add timeline view for session events
fix: correct token count aggregation for multi-turn sessions
docs: clarify watcher configuration in README
test: add coverage for OpenCode adapter edge cases
```

### Tests

Every PR must include tests for the changes it introduces.

```bash
# Run the full suite
pytest tests/

# Run a specific test file
pytest tests/test_discovery.py

# Run with verbose output
pytest tests/ -v
```

Aim for meaningful coverage — test behavior, not implementation details.

## Submitting a Pull Request

1. **One feature or fix per PR.** Keep changes focused. A PR that does three unrelated things is hard to review and harder to revert.
2. **Write a clear description.** Explain what the PR does, why it is needed, and how you tested it.
3. **Include tests.** PRs without tests for new functionality will be asked to add them.
4. **Follow the PR template.** It will guide you through the necessary checklists.
5. **Be patient.** Reviews may take a few days. If something needs changes, update the PR in place rather than opening a new one.

## Reporting Bugs and Requesting Features

Use the issue templates in `.github/ISSUE_TEMPLATE/`. They help ensure we have enough context to act on your report.

## Human Accountability

MoolMesh monitors autonomous agents — it does not replace human judgment. Every contribution must show that a person reviewed and understands the changes:

- **Commits must be authored by a human.** The Git `author` field must be a real person. `Co-Authored-By` tags for AI assistants are welcome and encouraged (they help us track AI-assisted development), but the commit itself must come from a human account.
- **PRs require human authorship.** Automated PRs generated entirely by bots without human review will be closed. If an AI agent helped write the code, the human who reviewed it opens the PR.
- **Explain what you understood, not what the AI wrote.** PR descriptions should reflect the contributor's understanding of the change — not a copy-paste of an AI's summary.

This isn't anti-AI — MoolMesh is literally built to work with AI agents. But a project about agent observability should demonstrate that humans remain in the loop. The agents work *for* us; the commits come *from* us.

---

## Notes for AI Agents Contributing to MoolMesh

If you are an AI coding agent generating a PR or patch for MoolMesh, keep these rules in mind:

- **Stick to the standard library.** Do not import packages outside of Python's stdlib. There are no exceptions.
- **Do not hallucinate dependencies.** If you are unsure whether a module is part of stdlib, verify before using it.
- **Ensure SQLite backward compatibility.** Your changes must not break existing databases. Add migration logic if you alter the schema.
- **Run the full test suite.** Confirm that `pytest tests/` passes with zero failures before submitting.
- **Read PHILOSOPHY.md.** Your contribution should align with the project's design principles.

## Code of Conduct

Be respectful, be constructive, and assume good intent. We are building something useful together.

## Questions

If something in this guide is unclear or you need help getting started, open a discussion or reach out in an issue. We are happy to help.
