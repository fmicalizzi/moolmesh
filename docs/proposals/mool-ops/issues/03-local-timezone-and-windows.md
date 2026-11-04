# Local time zone and time windows in digests and outcome

## Context
Digests pick the day by UTC date prefix, and the portfolio outcome (merged PRs, closed issues) is all-time. For a team at UTC-6, a PR merged at 19:00 local lands on the next day, and "what was delivered this week" cannot be answered.

## Proposal
- `[user] timezone` (IANA name, default system zone) used to assign local days in digests.
- `get_portfolio_production(days)` also returns `merged_prs_window` and `closed_issues_window` for the window, next to the all-time totals.

## Acceptance
A PR merged at 01:00 UTC appears in the previous local day's digest; windowed outcome equals a manual count.
