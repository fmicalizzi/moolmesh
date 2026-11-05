"""Per-contributor breakdown, opt-in (#83).

``[team] per_contributor`` is off by default and, off, nothing here runs: the
portfolio outcome and the digests stay contributor-agnostic, byte for byte.
On, the outcome already cached in ``github.db`` is also split by GitHub login
(or by an alias): no new GitHub call, no new table.

Who is credited:

- a merged PR → its author; its lines (+/-, from the #80 stats when cached)
  go with it;
- an issue (closed or open) → its first assignee that is not a bot, else its
  author.

Each item is credited to exactly ONE contributor, so per person the counts
add up to the total minus what bots (and items with no login) produced —
bots count in totals, never as a person (:func:`hub.snapshot.is_bot`, plus
``[team] bot_logins``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from hub.snapshot import is_bot


@dataclass(frozen=True)
class TeamConfig:
    per_contributor: bool = False
    # login (lowercased) -> alias; several logins may share an alias.
    aliases: dict[str, str] = field(default_factory=dict)
    # Extra bot logins (lowercased), on top of is_bot's rule.
    bot_logins: frozenset[str] = frozenset()

    def is_bot(self, login: str | None) -> bool:
        return is_bot(login) or str(login).strip().lower() in self.bot_logins

    def name_of(self, login: str) -> str:
        """The alias of ``login`` (case-insensitive), else the login itself."""
        return self.aliases.get(login.strip().lower(), login.strip())


def team_of(config: Any) -> TeamConfig:
    """The ``[team]`` settings of a loaded ``HubConfig`` (defaults if absent)."""
    if config is None or not getattr(config, "per_contributor", False):
        return TeamConfig()
    return TeamConfig(
        per_contributor=True,
        aliases={k.strip().lower(): v for k, v in
                 (getattr(config, "contributor_aliases", None) or {}).items()},
        bot_logins=frozenset(b.strip().lower() for b in
                             (getattr(config, "bot_logins", None) or [])),
    )


def configured_team() -> TeamConfig:
    """``[team]`` from config.toml; any read failure means OFF (the default)."""
    try:
        from hub.config import load_config
        return team_of(load_config())
    except Exception:  # noqa: BLE001 — opt-in: a broken read keeps it off
        return TeamConfig()


def _assignees(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(a) for a in raw if a]
    try:
        vals = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(a) for a in vals if a] if isinstance(vals, list) else []


def credited(team: TeamConfig, *, is_pr: bool, author: str | None,
             assignees: Any = None) -> str | None:
    """The contributor an item is credited to, or ``None`` for a bot."""
    if not is_pr:
        for login in _assignees(assignees):
            if not team.is_bot(login):
                return team.name_of(login)
    if team.is_bot(author):
        return None
    return team.name_of(str(author))


COUNT_KEYS = ("merged_prs", "closed_issues", "open_issues",
              "merged_prs_window", "closed_issues_window",
              "additions", "deletions", "additions_window", "deletions_window")


def empty_counts() -> dict[str, int]:
    return {k: 0 for k in COUNT_KEYS}


def add_item(cell: dict[str, int], *, is_pr: bool, state: str,
             merged: bool, in_window: bool,
             additions: int | None, deletions: int | None) -> None:
    """Fold one ``github_issues`` row into a per-contributor cell."""
    if is_pr:
        if not merged:
            return
        cell["merged_prs"] += 1
        cell["additions"] += additions or 0
        cell["deletions"] += deletions or 0
        if in_window:
            cell["merged_prs_window"] += 1
            cell["additions_window"] += additions or 0
            cell["deletions_window"] += deletions or 0
    elif state == "closed":
        cell["closed_issues"] += 1
        if in_window:
            cell["closed_issues_window"] += 1
    elif state == "open":
        cell["open_issues"] += 1


def digest_breakdown(team: TeamConfig, issues: list[dict],
                     activity: dict[str, list[dict]],
                     pr_stats: dict[int, dict]) -> dict[str, dict[str, int]]:
    """Per contributor for a digest period: merged PRs (+lines), closed issues.

    ``activity`` is the period's ``prs_merged`` / ``issues_closed`` lists;
    ``issues`` the cached rows (for issue assignees). Bots are left out.
    """
    by_number = {i["number"]: i for i in issues}
    out: dict[str, dict[str, int]] = {}

    def _cell(name: str) -> dict[str, int]:
        return out.setdefault(name, {"prs_merged": 0, "issues_closed": 0,
                                     "additions": 0, "deletions": 0})
    for pr in activity.get("prs_merged", []):
        name = credited(team, is_pr=True, author=pr.get("author"))
        if name is None:
            continue
        cell = _cell(name)
        cell["prs_merged"] += 1
        st = pr_stats.get(pr["number"]) or {}
        cell["additions"] += st.get("additions") or 0
        cell["deletions"] += st.get("deletions") or 0
    for issue in activity.get("issues_closed", []):
        row = by_number.get(issue["number"], {})
        name = credited(team, is_pr=False, author=row.get("author"),
                        assignees=row.get("assignees"))
        if name is None:
            continue
        _cell(name)["issues_closed"] += 1
    return dict(sorted(out.items(), key=lambda kv: (
        -kv[1]["prs_merged"], -kv[1]["issues_closed"], kv[0].lower())))
