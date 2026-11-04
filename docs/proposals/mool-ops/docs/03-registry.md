# 03 · Registry

One TOML file per logical organization, stored in its private data repo. Full example: [examples/registry.example.toml](../examples/registry.example.toml). TOML is read with the standard library (`tomllib`), the same format MoolMesh uses for its config.

## Sections

| Section | Fields | Notes |
|---|---|---|
| `[organization]` | `id`, `name`, `timezone` (IANA), `bots` | `timezone` decides the local day |
| `[[owners]]` | `login`, `type` (`org`/`user`), `relation` (`core`/`affiliated`/`confidential`) | Several owners per organization |
| `[[repos]]` | `repo` (`owner/name`), `kind` (`code`/`board`/`docs`), `membership`, `team`, `track`, `extensions` | `track = false` means the repo is never read |
| `[people.aliases]` | `login = "Display name"` | Optional; used for per-person views |

## Rules

- A repo whose owner has `relation = "confidential"` must have `track = false`. Validators should fail otherwise.
- `team` lists the logins expected to work on the repo; people with no activity in a period are reported as *available*.
- `extensions` is free-form per repo (round label, phase, milestone) and is copied into each snapshot untouched.
- Snapshot folders use the numeric repo id, so a transfer or rename keeps the series.
