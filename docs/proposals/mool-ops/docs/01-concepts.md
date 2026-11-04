# 01 · Concepts

## Logical organization

The unit the tracker reports on: a company, a client, a team — whoever a coordinator answers to. **It is not a GitHub organization.** It is described by one registry file and owns one private data repo (its *tenant*).

## Owners

A GitHub account that owns repos of the organization. Two types:

| Type | Example | Notes |
|---|---|---|
| `org` | `acme-dev` | A GitHub organization |
| `user` | `jdoe`, `acme-coord` | A personal account |

And a **relation** to the organization:

| Relation | Meaning | Tracker behavior |
|---|---|---|
| `core` | The organization's own GitHub org | Tracked |
| `affiliated` | Personal or third-party repos that work as part of the organization (a developer's scripts, a contractor's account) | Tracked; shown as affiliated |
| `confidential` | Repos kept **outside the GitHub org on purpose** because they hold sensitive material (coordination notes, HR, contracts) | **Never read.** Listed so nobody adds them by mistake |

Real organizations mix all three. A repo moving between owners is common (a project starts in a personal account and is transferred to the org later), so identity cannot depend on the owner name.

## Repo kinds

| Kind | What it is | What the tracker measures |
|---|---|---|
| `code` | Source repo with PRs | PRs merged, issues opened/closed, lines, per person (opt-in) |
| `board` | Issues-only backlog | Issues opened/closed by label, assignee and age |
| `docs` | Documents | Not measured by default |

## Snapshot

An immutable JSON record of one repo for one period (`start`–`end`, local days), with totals at the cut. Snapshots are **chained**: each period starts the day after the previous one ended, and its totals must equal the previous totals plus the period's changes.

## Round report

A human-readable report built from the snapshots of a period, with sections that come from data and sections that need analysis (written by a person or an agent).

## Coordinator

The person who reads the tracker to steer work. They may use their own AI coding agents, and therefore their own local MoolMesh.
