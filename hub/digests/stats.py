"""L1 Stats — Agregación SQL para digests."""
from __future__ import annotations
from typing import Any
from hub.cache.git_store import GitStore


def compute_daily_stats(git_store: GitStore, repo_id: int,
                        date: str) -> dict[str, Any]:
    """Computa estadísticas del día para un repo.

    Args:
        date: formato "YYYY-MM-DD"

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
    """
    since = f"{date}T00:00:00"
    until = f"{date}T23:59:59"

    # Git stats (ya existentes en git_store)
    commits = git_store.count_commits(repo_id, since, until)
    authors = git_store.get_author_stats(repo_id, since, until)
    hot_files = git_store.get_hot_files(repo_id, since, until, limit=10)
    branches = git_store.get_branch_stats(repo_id, since, until)
    loc_added = sum(a.get("insertions", 0) for a in authors)
    loc_removed = sum(a.get("deletions", 0) for a in authors)

    # GitHub stats — filtrar por fecha
    all_issues = git_store.get_issues(repo_id)

    prs_merged = []
    prs_opened = []
    issues_closed = []
    issues_opened = []

    for issue in all_issues:
        created = issue.get("created_at", "")
        closed = issue.get("closed_at", "")
        merged = issue.get("pr_merged_at", "")
        is_pr = issue.get("is_pull_request", 0)

        if is_pr:
            if merged and merged.startswith(date):
                prs_merged.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                    "pr_merged_at": merged,
                })
            if created.startswith(date):
                prs_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                })
        else:
            if closed and closed.startswith(date):
                issues_closed.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })
            if created.startswith(date):
                issues_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })

    return {
        "commits": commits,
        "authors": authors,
        "branches": branches,
        "hot_files": hot_files,
        "loc_added": loc_added,
        "loc_removed": loc_removed,
        "prs_merged": prs_merged,
        "prs_opened": prs_opened,
        "issues_closed": issues_closed,
        "issues_opened": issues_opened,
    }


def compute_weekly_stats(git_store: GitStore, repo_id: int,
                         week_start: str) -> dict[str, Any]:
    """Computa estadísticas de la semana (lunes a domingo).

    Args:
        week_start: "YYYY-MM-DD" del lunes de la semana

    Misma estructura que daily pero con rango extendido.
    """
    from datetime import datetime, timedelta

    start = datetime.strptime(week_start, "%Y-%m-%d")
    end = start + timedelta(days=6)
    since = start.strftime("%Y-%m-%dT00:00:00")
    until = end.strftime("%Y-%m-%dT23:59:59")

    commits = git_store.count_commits(repo_id, since, until)
    authors = git_store.get_author_stats(repo_id, since, until)
    hot_files = git_store.get_hot_files(repo_id, since, until, limit=15)
    branches = git_store.get_branch_stats(repo_id, since, until)
    loc_added = sum(a.get("insertions", 0) for a in authors)
    loc_removed = sum(a.get("deletions", 0) for a in authors)

    # GitHub stats — filtrar por rango de semana
    all_issues = git_store.get_issues(repo_id)

    prs_merged = []
    prs_opened = []
    issues_closed = []
    issues_opened = []

    since_date = week_start
    until_date = end.strftime("%Y-%m-%d")

    for issue in all_issues:
        created = issue.get("created_at", "")[:10]  # "YYYY-MM-DD"
        closed = (issue.get("closed_at") or "")[:10]
        merged = (issue.get("pr_merged_at") or "")[:10]
        is_pr = issue.get("is_pull_request", 0)

        if is_pr:
            if merged and since_date <= merged <= until_date:
                prs_merged.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                })
            if since_date <= created <= until_date:
                prs_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("author", ""),
                })
        else:
            if closed and since_date <= closed <= until_date:
                issues_closed.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })
            if since_date <= created <= until_date:
                issues_opened.append({
                    "number": issue["number"],
                    "title": issue["title"],
                })

    return {
        "commits": commits,
        "authors": authors,
        "branches": branches,
        "hot_files": hot_files,
        "loc_added": loc_added,
        "loc_removed": loc_removed,
        "prs_merged": prs_merged,
        "prs_opened": prs_opened,
        "issues_closed": issues_closed,
        "issues_opened": issues_opened,
    }
