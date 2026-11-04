# Handoff to the MoolMesh maintainers

**What this is:** a design kit, not a product. It describes an operations-tracking capability that complements MoolMesh and ships a working reference so the method is unambiguous. **The maintainers own the implementation decisions**: where it lives (inside MoolMesh, an optional module, or a companion package named `mool-ops`), its CLI, its storage and its release plan.

**Issues are NOT filed yet.** The 8 drafts live in [issues/](issues/); the maintainers decide which ones to file, merge or drop, and when. Nothing about this kit has been published.

**What is not in scope for this kit:** creating a public repo for it. The repo that matters is the one users will install to activate these capabilities, and that is decided by the maintainers.

## What the requester needs (outcome, not design)

1. A coordinator can see, for one logical organization, **what was delivered, by whom and in which period**, across repos owned by a GitHub org and by personal accounts, with confidential repos never read.
2. Numbers are **reproducible and honest**: local-day grouping, chained and verified totals, bots excluded from credits, stale data flagged.
3. Each person runs it on their own machine (macOS and **Linux** must work); a shared, private place holds the organization's registry, snapshots and reports so two people see the same thing.
4. Per-person data is **opt-in** and never leaves the organization's private data.

## Read in this order

1. [docs/01-concepts.md](docs/01-concepts.md): logical organization vs GitHub owner; `core` / `affiliated` / `confidential`.
2. [docs/07-moolmesh-gap-map.md](docs/07-moolmesh-gap-map.md): what MoolMesh already has and the 8 gaps.
3. [issues/](issues/): one draft per gap, ready to file. 01 extends the existing #76.
4. [docs/05-methodology.md](docs/05-methodology.md) and [reference/ops_snapshot.py](reference/ops_snapshot.py): the method, with tests (`python3 -m unittest discover -s tests`).
5. [docs/04-snapshot-schema.md](docs/04-snapshot-schema.md): a proposed v1 export schema; adopt, change or replace it.

## Acceptance from the requester's side

- The reference examples (`examples/`) give the same totals when produced by the maintainers' implementation.
- A confidential owner or repo in the registry is never synced.
- With per-contributor data off, existing MoolMesh output is unchanged.

## After delivery

The requester will run it in their own environment (registry, private data repo, scheduled run, onboarding of a second user on Linux). Until then they keep their current scripts as the operational source.
