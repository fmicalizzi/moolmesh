# Per-contributor breakdown (opt-in, off by default)

## Context
The portfolio outcome is contributor-agnostic by design, and team visibility is deferred to v2.x. A team coordinator needs per-person production (PRs, issues, lines) inside their own organization. The data is already cached (author, assignees); only the surface is missing.

## Proposal
`[team] per_contributor = false` by default. When enabled:
- per-login breakdown in outcome and digests;
- optional login → alias map;
- bots excluded (`dependabot`, `*[bot]`, `app/*`, configurable list);
- a clear indicator in the dashboard that the view is active.
Nothing changes with the flag off.

## Acceptance
With the flag off, output is byte-for-byte identical to today; with it on, the per-person sum equals the total minus bots.
