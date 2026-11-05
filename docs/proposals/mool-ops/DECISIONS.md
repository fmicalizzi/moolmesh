# mool-ops — maintainers' decisions and status

The kit in this folder is the requester's proposal, kept as received (minus its
own LICENSE: MoolMesh's MIT license covers the repo). This file records what the
MoolMesh maintainers decided about it. Tracking issue:
[#84](https://github.com/fmicalizzi/moolmesh/issues/84).

## Decisions (2026-11-04)

1. **The engine is a companion package, `mool-ops`, not part of MoolMesh core.**
   It writes chained snapshots and round reports to a private data repo per
   organization, runs on a schedule (CI) and authenticates through a GitHub App.
   Those cross two MoolMesh invariants (zero-cloud, read-only base — see
   `AGENTS.md` §2), so it lives outside and reads MoolMesh only through a
   versioned export, never its SQLite files (or `gh` until that export exists).
2. **MoolMesh core takes only the generic gaps**, each local-first, stdlib-only
   and read-only, one unit per release.
3. **Per-contributor data (draft 05) was an open decision**
   ([#83](https://github.com/fmicalizzi/moolmesh/issues/83)). Resolved as "both
   places" — see the 2026-11-05 decisions below.
4. **The reference implementation is a method, not a spec.** Parity is measured
   on `examples/`, plus edge-case tests of our own (for example a closed issue
   with no `closedAt`).

## Decisions (2026-11-05)

5. **Per-contributor data lives in both places.** Core has an opt-in breakdown
   by GitHub login (`[team] per_contributor`, #83, v1.35.0; off by default, so
   core stays contributor-agnostic unless the user turns it on). The engine
   gets per-device effort from an opt-in device identity and `mool effort
   export` (#140, v1.36.0), contract `docs/schema/effort.v1.md`.
6. **The engine lives in `fmicalizzi/mool-ops`**, a separate repository,
   private until its first release. It reads MoolMesh only through
   `mool snapshot export` and `mool effort export` (`docs/schema/`), never its
   SQLite files. Anything it needs from core is filed here as an issue; a
   contract change bumps the schema version.

## Draft → issue map

All eight drafts shipped in MoolMesh core.

| Draft | Status |
|---|---|
| 01 stale-data flag + autostart | Shipped — [#76](https://github.com/fmicalizzi/moolmesh/issues/76), v1.27.2 |
| 02 per-PR stats | Shipped — [#80](https://github.com/fmicalizzi/moolmesh/issues/80), v1.29.0 |
| 03 local time zone + windows | Shipped — [#81](https://github.com/fmicalizzi/moolmesh/issues/81) with [#79](https://github.com/fmicalizzi/moolmesh/issues/79), v1.28.0 |
| 04 versioned snapshot export | Shipped — [#82](https://github.com/fmicalizzi/moolmesh/issues/82), v1.31.0 (zone always named: [#146](https://github.com/fmicalizzi/moolmesh/issues/146), v1.36.1) |
| 05 per-contributor breakdown | Shipped — [#83](https://github.com/fmicalizzi/moolmesh/issues/83), v1.35.0 (core, opt-in) + [#140](https://github.com/fmicalizzi/moolmesh/issues/140), v1.36.0 (device identity + effort export) |
| 06 board repos | Shipped — [#93](https://github.com/fmicalizzi/moolmesh/issues/93), v1.30.0 |
| 07 remote-only repos | Shipped — [#94](https://github.com/fmicalizzi/moolmesh/issues/94), v1.30.0 |
| 08 client spanning several owners | Shipped — [#95](https://github.com/fmicalizzi/moolmesh/issues/95), v1.30.0 |

## Indicative order — done

The planned order held. What shipped:

v1.27.2 (#76 + 01) → v1.28.0 (#81 + #79) → v1.28.1 robustness (#52, #54, #62,
#67) → v1.29.0 (#80) → v1.30.0 (06–08: #93, #94, #95) → v1.31.0 (#82) →
v1.35.0 (#83) → v1.36.0 (#140) → v1.36.1 (#146).

Next is the `mool-ops` engine, in its own repo: first with `gh` as the source,
then on `mool snapshot export` with identical totals on two consecutive cuts
(checked on v1.36.0, see #84), joining `mool effort export` files per device.

The kit's own tests run standalone (`python3 -m unittest discover -s tests` from
this folder); MoolMesh's suite does not collect them (`testpaths = ["tests"]`).
