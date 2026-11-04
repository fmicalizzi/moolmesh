# 02 · Architecture

```
 ┌──────────────── each person's machine ───────────────────┐
 │  MoolMesh (local, unchanged principles)                    │
 │   sessions (private) · github cache · portfolio · digests │
 │   MCP ◄── the person's coding agents                       │
 └───────────────▲───────────────────────────────────────────┘
                 │ versioned export / MCP  (stable contract)
 ┌───────────────┴───────────────────────────────────────────┐
 │  OSS engine (this kit → a package)                         │
 │   registry · sources (gh today, MoolMesh later)            │
 │   chained + verified snapshots · local day · reports       │
 └───────────────▲───────────────────────────────────────────┘
                 │ reads registry, writes snapshots/reports
 ┌───────────────┴───────────────────────────────────────────┐
 │  PRIVATE data repo — one per logical organization          │
 │   registry.toml · snapshots/<repo-id>/*.json · reports/    │
 │   scheduled run (CI) · generated static dashboard          │
 └────────────────────────────────────────────────────────────┘
```

## Boundaries

- **Sessions never leave the machine.** The engine and the data repo only carry repository outcome (issues, PRs, lines) and reports. Nothing from MoolMesh's session or workspace stores is shared.
- **The engine does not read MoolMesh's SQLite files.** Their schema evolves through migrations. The contract is a versioned export command or MCP tools (see issue 04).
- **Until that contract exists, the engine reads GitHub through `gh`.** Swapping the source must give identical totals on at least two consecutive cuts.
- **One data repo per organization.** Access control is the repo's own permissions; a dashboard published from it gets its own login.
- **Client grouping comes from the registry, not from each machine.** The same account can be "personal" on one machine and "part of a client" on another; a shared registry keeps everyone's view consistent. This is also the natural seed of MoolMesh's planned cross-machine aggregation.

## Who sees what

| Person | Sees | Through |
|---|---|---|
| Machine owner | Own sessions + portfolio + all repos their token reads | Local MoolMesh |
| Coordinator | Own sessions + the organization's snapshots, reports and dashboard | Own MoolMesh + private data repo |
| Anyone else | Nothing | — |
