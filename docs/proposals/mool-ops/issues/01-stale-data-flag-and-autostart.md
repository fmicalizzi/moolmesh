# Daemon: stale-data flag in MCP/dashboard, on top of autostart (extends #76)

> Suggested as a comment on #76 plus, if preferred, a separate issue for the stale-data flag.

## Context
In real use the daemon stopped and nobody noticed for seven days. The portfolio and the GitHub cache froze, and the MCP tools kept answering with old data and no warning. A coordinator reading those numbers takes them as current. #76 covers autostart; this adds the read-side guard.

## Proposal
- Keep #76's `mool daemon install` (launchd / systemd `--user` / Task Scheduler).
- MCP tools that read the portfolio or the GitHub cache add `data_age_seconds` and `stale: true` when the last successful sync is older than a configurable threshold (`[daemon] stale_after_minutes`, default 60).
- `mool doctor` and every dashboard page show a visible banner when the daemon is not running.

## Acceptance
- After a reboot, the daemon is back with no manual step (where installed).
- With the daemon stopped, a portfolio MCP call returns `stale: true` and the data age.
