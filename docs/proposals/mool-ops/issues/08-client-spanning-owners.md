# One client spanning several owners (org + personal accounts)

## Context
The client ladder maps one client per remote owner (`client_orgs`) or per-project overrides. Real organizations keep repos in their GitHub org **and** in personal accounts that work as part of it, and some repos stay outside the org on purpose. Today each such repo needs its own override, and a new repo in that account falls outside the client. The same account can also be "personal" on one machine and "a client's" on another.

## Proposal
- `[workspace] client_owner_aliases = {"some-account" = "client"}` to map a whole owner to a client.
- Optional external registry file for client attribution, so several machines group the same way (a step toward Phase D).
- An explicit `exclude_owners` / `exclude_repos` list for confidential repos that must never be read.

## Acceptance
A new repo in a mapped account appears under the client with no override; two machines using the same registry show the same grouping; an excluded repo is never synced.
