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
3. **Per-contributor data (draft 05) is an open decision**
   ([#83](https://github.com/fmicalizzi/moolmesh/issues/83)). The maintainers
   lean to keeping it in the engine, inside each organization's private data,
   so MoolMesh core stays contributor-agnostic.
4. **The reference implementation is a method, not a spec.** Parity is measured
   on `examples/`, plus edge-case tests of our own (for example a closed issue
   with no `closedAt`).

## Draft → issue map

| Draft | Status |
|---|---|
| 01 stale-data flag + autostart | Scope added to [#76](https://github.com/fmicalizzi/moolmesh/issues/76) — target v1.27.2 |
| 02 per-PR stats | [#80](https://github.com/fmicalizzi/moolmesh/issues/80) |
| 03 local time zone + windows | [#81](https://github.com/fmicalizzi/moolmesh/issues/81) (pair with #79) |
| 04 versioned snapshot export | [#82](https://github.com/fmicalizzi/moolmesh/issues/82) |
| 05 per-contributor breakdown | Decision: [#83](https://github.com/fmicalizzi/moolmesh/issues/83) |
| 06 board repos | Draft — to file later |
| 07 remote-only repos | Draft — to file later |
| 08 client spanning several owners | Draft — to file later |

## Indicative order

v1.27.2 (#76 + 01) → v1.28.0 (#81 + #79) → v1.28.x robustness (#52, #54, #62,
#67) → v1.29.0 (#80) → v1.30.0 (06–08) → v1.31.0 (#82) → `mool-ops` engine
(first with `gh`, then on the export with identical totals on two consecutive
cuts).

The kit's own tests run standalone (`python3 -m unittest discover -s tests` from
this folder); MoolMesh's suite does not collect them (`testpaths = ["tests"]`).
