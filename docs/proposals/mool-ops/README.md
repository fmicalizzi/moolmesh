# mool-ops-kit

> **Where this went (2026-11-06):** the maintainers' decisions are in [DECISIONS.md](DECISIONS.md). All eight drafts shipped in MoolMesh core, and the engine was built as a separate package in `fmicalizzi/mool-ops`, **private until its first release** (github.com/fmicalizzi/mool-ops answers 404 without access). Its contract with MoolMesh is [`snapshot.v1`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/snapshot.v1.md) and [`effort.v1`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/effort.v1.md). This kit is kept as it was received, as the design record.

**Design kit for an operations tracker that complements MoolMesh.**
Status: proposal + working reference implementation. Everything here is generic; the examples use a fictional company ("Acme Corp").

> Español: [README.es.md](README.es.md)

## Why

MoolMesh observes AI coding agents and the repos they touch, on each person's machine. A coordinator of a development team needs a different but adjacent view: **what was delivered, by whom, in which period, across all the repos of an organization**. That view must be reproducible, auditable and shareable with people who are not the owner of the machine.

This kit describes that tracker and how it sits next to MoolMesh without breaking its principles (local-first, stdlib, read-only, honest data).

## What is in the kit

| Path | Content |
|---|---|
| [docs/01-concepts.md](docs/01-concepts.md) | Vocabulary: logical organization vs GitHub owner, owner relations, repo kinds, tenant |
| [docs/02-architecture.md](docs/02-architecture.md) | Three layers (MoolMesh local · OSS engine · private data repo) and their boundaries |
| [docs/03-registry.md](docs/03-registry.md) | The registry file that describes one organization |
| [docs/04-snapshot-schema.md](docs/04-snapshot-schema.md) | Snapshot schema v1 ([JSON Schema](schema/snapshot.v1.schema.json)) |
| [docs/05-methodology.md](docs/05-methodology.md) | Incremental, chained and verified snapshots; local day; bots; round reports |
| [docs/06-auth-multi-owner.md](docs/06-auth-multi-owner.md) | Tokens when one organization spans several GitHub owners |
| [docs/07-moolmesh-gap-map.md](docs/07-moolmesh-gap-map.md) | What MoolMesh already provides, what is missing, and the proposed issues |
| [HANDOFF.md](HANDOFF.md) | Start here: handoff to the MoolMesh maintainers |
| [MENSAJE-CONTEXTO.md](MENSAJE-CONTEXTO.md) | Context message for the MoolMesh team (Spanish) |
| [issues/](issues/) | Issue drafts for MoolMesh, ready to file |
| [reference/ops_snapshot.py](reference/ops_snapshot.py) | Reference implementation (stdlib, `gh` or fixtures) |
| [examples/](examples/) | Registry, fixtures, generated snapshots and a sample report |
| [tests/](tests/) | Offline tests for the reference implementation |

## Try it (offline)

```bash
python3 -m unittest discover -s tests -v
python3 reference/ops_snapshot.py --registry examples/registry.example.toml \
        --data /tmp/acme-data --cut 2026-11-03 --start 2026-11-02 \
        --fixtures examples/fixtures
```

Against real repos, drop `--fixtures` (it uses the `gh` CLI, read-only). Optional per-owner tokens: `GH_TOKEN_<OWNER>`.

## Principles

1. **Data repo per organization.** The engine is public; registries, snapshots and reports are private and never mixed across organizations.
2. **Honest numbers.** Totals are chained and verified against the source at every cut; any adjustment is written down, never hidden.
3. **Local day.** Activity is grouped by the organization's time zone, not by UTC date.
4. **Stable identity.** Repos are keyed by GitHub numeric id; issue references are always `owner/repo#n`.
5. **Confidential repos stay out.** A repo marked confidential is listed only to be excluded.
6. **Per-person data is opt-in** and stays inside the organization's private data repo.

## License

MIT (proposed, same as MoolMesh). This kit is a handoff package; it is not meant to be published as its own repo.
