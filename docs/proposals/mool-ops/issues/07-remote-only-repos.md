# Register repos by remote only (no local clone), for orgs and personal accounts

## Context
A local path is mandatory per repo. Following many repos of one organization means cloning all of them, even when only issues and PRs matter.

## Proposal
`mool repo add --remote OWNER/REPO` and `mool repo add --owner OWNER [--include ...]`, API-only, no git history. `OWNER` can be an organization **or a personal account**. The portfolio shows them as "no clone". Repo identity uses the numeric GitHub id, so transfers and renames keep their history.

## Acceptance
A remote-only repo has issues and PRs synced and appears in the portfolio; a repo transferred to another owner keeps its series.
