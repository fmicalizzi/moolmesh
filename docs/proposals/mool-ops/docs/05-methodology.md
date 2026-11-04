# 05 · Methodology

The method was validated over months of daily and per-period cuts on a real multi-repo organization. The reference implementation is [reference/ops_snapshot.py](../reference/ops_snapshot.py).

## 1. Incremental, never regenerate

- Each run starts at `period.end + 1` of the repo's last snapshot.
- An existing snapshot file is **never overwritten**. Corrections go into the next snapshot's `notes`, or into a deliberate, reviewed rewrite.
- A period with no PRs merged and no issues opened or closed produces **no snapshot** (weekends, quiet repos). Gaps in the file list are expected and are not errors.

> Lesson learned: a "regenerate everything from a start date" script with a hard-coded end date silently overwrote curated snapshots and reports. Incremental-only avoids that class of bug.

## 2. Local day

Every timestamp is converted to the organization's IANA time zone before choosing its day. With UTC-6, a PR merged at `03:30Z` on the 3rd belongs to the **2nd**. Grouping by UTC date prefix moves evening work to the next day.

## 3. Chained and verified totals

```
chain_closed = previous.total_closed + closed_this_period
chain_open   = previous.total_open   - closed_this_period + opened_this_period
live_*       = counted directly from the source at the cut
```

If `chain ≠ live`, the snapshot keeps the **live** value and writes the difference in `notes` (`"chain 120/14 adjusted to live 120/15"`). In practice this catches an earlier cut that was off by one, reopened issues, or transferred issues. Silent adjustment is not allowed.

Fetch enough rows: use limits well above the repo's size (≥ 2000–5000) and paginate. A low default limit silently truncates large repos.

## 4. People and bots

- Bots (`dependabot`, `*[bot]`, `app/*`, registry `bots`) count in totals but are **never credited** to a person.
- An issue closed in the period is credited to its first assignee; if none, to the author of the PR that closed it; otherwise to nobody.
- Duplicates closed without a PR count as closed but credit nobody.
- Per-person data is opt-in per organization.

## 5. Identity

- Repo key = numeric GitHub id. Label = `owner/name`.
- Issue references = `owner/repo#n`. A PR can close an issue in another repo with the same number; bare numbers mis-credit it (covered by a test).

## 6. Round reports

A report covers one or more snapshots. Sections marked **[DATA]** are generated; **[ANALYSIS]** are written by a person or an agent after reading PR bodies and comments.

1. Executive summary [ANALYSIS] — counts, 2–4 work streams, milestone
2. Timeline [DATA] — merged PRs in order, local time
3. Quantitative analysis [DATA] — volume, per person, per PR
4. What was delivered [DATA + ANALYSIS] — one subsection per PR/issue
5. Observations [ANALYSIS] — what worked, attention areas, coordination context
6. Comparative velocity [DATA] — computed from the snapshot series, never kept by hand
7. Cumulative state [DATA] — totals, per person across rounds
8. Work in progress [DATA] — open issues, open PRs, available people
9. Conclusion [ANALYSIS]

Rules: never auto-publish a report; show it, let a person review, then save. Sample: [examples/report.example.md](../examples/report.example.md).

## 7. Operational checks per run

- Source freshness: if the source is a local cache (MoolMesh), refuse to cut when its last sync is older than the cut date.
- Totals cross-check against the source for the last cut.
- Open PR count reported with and without bots.
