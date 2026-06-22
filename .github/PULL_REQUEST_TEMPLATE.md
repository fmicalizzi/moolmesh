## What does this PR do?

<!-- A concise description of the change and its motivation. Link to related issues if applicable. -->

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## Architectural alignment

- [ ] **Provider agnosticism** — This change does not favor or hard-code behavior for a specific AI coding provider.
- [ ] **State integrity** — SQLite schema changes are additive and backward compatible. Existing databases will continue to work.
- [ ] **Local execution** — No network calls, cloud dependencies, or external services are introduced. Everything runs on the user's machine.
- [ ] **Zero external dependencies** — Only Python standard library modules are used. No new pip packages are required.

## Testing

- [ ] `pytest tests/` passes with zero failures.
- [ ] New or modified behavior is covered by tests.
- [ ] Tested locally with `mool` CLI (if applicable).
- [ ] Tested with an MCP client (if MCP server changes are involved).

## Checklist

- [ ] I have performed a self-review of my code.
- [ ] I have updated documentation where necessary (README, CHANGELOG, docstrings).
- [ ] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
