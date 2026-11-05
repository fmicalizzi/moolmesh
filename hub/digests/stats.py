"""L1 Stats — Agregación SQL para digests.

Días LOCALES (issue #81): el día de cada commit, PR e issue es el de la zona
``[user] timezone`` (``hub.localtime``), la misma definición que usan el rollup
de workspaces y el portafolio.
"""
from __future__ import annotations
from typing import Any
from hub.cache.git_store import GitStore
from hub.localtime import CONFIGURED, local_day, shift_day, system_naive_range, user_timezone


def _issue_activity(all_issues: list[dict], first_day: str, last_day: str,
                    tz) -> dict[str, list[dict]]:
    """PRs/issues opened, merged and closed within local days first..last."""
    prs_merged: list[dict] = []
    prs_opened: list[dict] = []
    issues_closed: list[dict] = []
    issues_opened: list[dict] = []

    def _in(ts) -> bool:
        day = local_day(ts, tz) if ts else None
        return day is not None and first_day <= day <= last_day

    for issue in all_issues:
        created = issue.get("created_at") or ""
        closed = issue.get("closed_at") or ""
        merged = issue.get("pr_merged_at") or ""
        if issue.get("is_pull_request", 0):
            if _in(merged):
                prs_merged.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                    "pr_merged_at": merged,
                })
            if _in(created):
                prs_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                })
        else:
            if _in(closed):
                issues_closed.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })
            if _in(created):
                issues_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })
    return {
        "prs_merged": prs_merged,
        "prs_opened": prs_opened,
        "issues_closed": issues_closed,
        "issues_opened": issues_opened,
    }


def _with_contributors(stats: dict[str, Any], team, git_store: GitStore,
                       repo_id: int, issues: list[dict]) -> dict[str, Any]:
    """Add ``contributors`` (#83) only when ``[team] per_contributor`` is on."""
    if team is None or not team.per_contributor:
        return stats
    from hub.team import digest_breakdown
    stats["contributors"] = digest_breakdown(
        team, issues, stats, git_store.get_pr_stats(repo_id))
    return stats


def compute_daily_stats(git_store: GitStore, repo_id: int,
                        date: str, tz=CONFIGURED, team=None) -> dict[str, Any]:
    """Computa estadísticas del día LOCAL para un repo.

    Args:
        date: formato "YYYY-MM-DD" (día local en ``tz``)
        tz: zona del día; default la de ``[user] timezone`` (#81)

    Returns dict con:
        commits: int
        authors: list[dict]  — {author_name, commits, insertions, deletions}
        hot_files: list[dict] — {file_path, changes, insertions, deletions}
        loc_added: int
        loc_removed: int
        prs_merged: list[dict] — [{number, title, author, pr_merged_at}]
        prs_opened: list[dict]
        issues_closed: list[dict] — [{number, title}]
        issues_opened: list[dict]
        contributors: dict — sólo con ``team.per_contributor`` (#83):
            {login o alias: {prs_merged, issues_closed, additions, deletions}}
    """
    if tz is CONFIGURED:
        tz = user_timezone()
    since, until = system_naive_range(date, date, tz)

    # Git stats (ya existentes en git_store)
    commits = git_store.count_commits(repo_id, since, until)
    authors = git_store.get_author_stats(repo_id, since, until)
    hot_files = git_store.get_hot_files(repo_id, since, until, limit=10)
    branches = git_store.get_branch_stats(repo_id, since, until)
    loc_added = sum(a.get("insertions", 0) for a in authors)
    loc_removed = sum(a.get("deletions", 0) for a in authors)

    # GitHub stats — filtrar por día local
    issues = git_store.get_issues(repo_id)
    activity = _issue_activity(issues, date, date, tz)

    return _with_contributors({
        "commits": commits,
        "authors": authors,
        "branches": branches,
        "hot_files": hot_files,
        "loc_added": loc_added,
        "loc_removed": loc_removed,
        **activity,
    }, team, git_store, repo_id, issues)


def compute_weekly_stats(git_store: GitStore, repo_id: int,
                         week_start: str, tz=CONFIGURED, team=None) -> dict[str, Any]:
    """Computa estadísticas de la semana (lunes a domingo, días locales).

    Args:
        week_start: "YYYY-MM-DD" del lunes de la semana
        tz: zona del día; default la de ``[user] timezone`` (#81)

    Misma estructura que daily pero con rango extendido.
    """
    if tz is CONFIGURED:
        tz = user_timezone()
    week_end = shift_day(week_start, 6)
    since, until = system_naive_range(week_start, week_end, tz)

    commits = git_store.count_commits(repo_id, since, until)
    authors = git_store.get_author_stats(repo_id, since, until)
    hot_files = git_store.get_hot_files(repo_id, since, until, limit=15)
    branches = git_store.get_branch_stats(repo_id, since, until)
    loc_added = sum(a.get("insertions", 0) for a in authors)
    loc_removed = sum(a.get("deletions", 0) for a in authors)

    # GitHub stats — filtrar por rango de semana
    issues = git_store.get_issues(repo_id)
    activity = _issue_activity(issues, week_start, week_end, tz)
    # El weekly nunca expuso pr_merged_at (forma estable del digest cacheado).
    for pr in activity["prs_merged"]:
        pr.pop("pr_merged_at", None)

    return _with_contributors({
        "commits": commits,
        "authors": authors,
        "branches": branches,
        "hot_files": hot_files,
        "loc_added": loc_added,
        "loc_removed": loc_removed,
        **activity,
    }, team, git_store, repo_id, issues)
