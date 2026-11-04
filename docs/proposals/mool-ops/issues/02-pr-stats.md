# Per-PR stats: additions, deletions, changed files and closing references

## Context
PRs are cached without `additions`, `deletions`, `changed_files` or the issues they close. Lines exist only per commit, which does not match what lands with a squash or merge commit, so a report cannot say what each merged PR contributed.

## Proposal
Populate those fields (or an additive `github_pr_stats` table) from the pulls API and `closingIssuesReferences`. Store closing references qualified as `owner/repo#n`, since a PR can close issues in other repos. Optional backfill command.

## Acceptance
For a merged PR, the cache holds its +/-, files and closed issues, equal to what GitHub shows, including a cross-repo closing reference.
