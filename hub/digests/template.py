"""L2 Template — Texto pre-armado en español con slots."""
from __future__ import annotations
from typing import Any


def _truncate(s: str, max_len: int) -> str:
    """Trunca con indicador visual. Preserva alineación en bloques monospace."""
    return s if len(s) <= max_len else s[:max_len - 1] + "…"


def render_daily(stats: dict[str, Any], repo_name: str, date: str) -> str:
    """Renderiza digest diario en español.

    Siempre funciona (no requiere LLM). Zero dependencies.
    """
    lines = []

    # Header
    # Convertir "2026-04-16" a "16 de abril de 2026"
    day_str = _format_date_es(date)
    lines.append(f"## Resumen del Día — {day_str}")
    lines.append(f"**Repo:** {repo_name}")
    lines.append("")

    # Commits overview
    n_commits = stats.get("commits", 0)
    n_authors = len(stats.get("authors", []))
    loc_add = stats.get("loc_added", 0)
    loc_del = stats.get("loc_removed", 0)

    # Verificar si hay alguna actividad (commits, PRs, issues)
    has_activity = (
        n_commits > 0 or
        stats.get("prs_merged") or
        stats.get("prs_opened") or
        stats.get("issues_closed") or
        stats.get("issues_opened")
    )

    if not has_activity:
        lines.append("No hubo actividad en este día.")
        return "\n".join(lines)

    if n_commits > 0:
        lines.append(
            f"Se realizaron **{n_commits} commits** por "
            f"**{n_authors} {'autor' if n_authors == 1 else 'autores'}**. "
            f"+{loc_add}/-{loc_del} líneas."
        )
        lines.append("")
    elif has_activity:
        # Hay PRs/issues pero no commits
        lines.append("Sin commits, pero con actividad en PRs e issues.")
        lines.append("")

    # PRs merged
    prs_merged = stats.get("prs_merged", [])
    if prs_merged:
        lines.append(f"**PRs mergeados ({len(prs_merged)}):**")
        for pr in prs_merged:
            lines.append(f'  • PR #{pr["number"]} "{pr["title"]}" — @{pr.get("author", "?")}')
        lines.append("")

    # PRs opened
    prs_opened = stats.get("prs_opened", [])
    if prs_opened:
        lines.append(f"**PRs nuevos ({len(prs_opened)}):**")
        for pr in prs_opened:
            lines.append(f'  • PR #{pr["number"]} "{pr["title"]}" — @{pr.get("author", "?")}')
        lines.append("")

    # Issues closed
    issues_closed = stats.get("issues_closed", [])
    if issues_closed:
        nums = ", ".join(f'#{i["number"]}' for i in issues_closed)
        lines.append(f"**Issues cerrados ({len(issues_closed)}):** {nums}")
        lines.append("")

    # Issues opened
    issues_opened = stats.get("issues_opened", [])
    if issues_opened:
        nums = ", ".join(f'#{i["number"]}' for i in issues_opened)
        lines.append(f"**Issues nuevos ({len(issues_opened)}):** {nums}")
        lines.append("")

    # Top authors
    authors = stats.get("authors", [])
    if len(authors) > 1:
        lines.append("**Autores más activos:**")
        for a in authors[:5]:
            lines.append(
                f'  • {a["author_name"]}: {a["commits"]} commits '
                f'(+{a.get("insertions", 0)}/-{a.get("deletions", 0)})'
            )
        lines.append("")

    # Hot files
    hot = stats.get("hot_files", [])
    if hot:
        lines.append("**Archivos más modificados:**")
        for f in hot[:5]:
            lines.append(f'  • `{f["file_path"]}` — {f["changes"]} cambios')
        lines.append("")

    return "\n".join(lines)


def render_weekly(stats: dict[str, Any], repo_name: str,
                  week_start: str) -> str:
    """Renderiza digest semanal en español."""
    from datetime import datetime, timedelta

    start = datetime.strptime(week_start, "%Y-%m-%d")
    end = start + timedelta(days=6)
    start_str = _format_date_es(week_start)
    end_str = _format_date_es(end.strftime("%Y-%m-%d"))

    lines = []
    lines.append(f"## Resumen Semanal — {start_str} al {end_str}")
    lines.append(f"**Repo:** {repo_name}")
    lines.append("")

    n_commits = stats.get("commits", 0)
    n_authors = len(stats.get("authors", []))
    loc_add = stats.get("loc_added", 0)
    loc_del = stats.get("loc_removed", 0)

    if n_commits == 0:
        lines.append("No hubo actividad de commits esta semana.")
        return "\n".join(lines)

    lines.append(
        f"**Balance de la semana:** {n_commits} commits, "
        f"{n_authors} {'autor' if n_authors == 1 else 'autores'}, "
        f"+{loc_add}/-{loc_del} líneas."
    )
    lines.append("")

    # PRs summary - limitar a top 5
    prs_merged = stats.get("prs_merged", [])
    prs_opened = stats.get("prs_opened", [])
    MAX_PRS_LIST = 5
    if prs_merged or prs_opened:
        lines.append("**Pull Requests:**")
        if prs_merged:
            lines.append(f"  • {len(prs_merged)} mergeados")
            for pr in prs_merged[:MAX_PRS_LIST]:
                lines.append(f'    – #{pr["number"]} "{pr["title"]}"')
            if len(prs_merged) > MAX_PRS_LIST:
                lines.append(f'    – y {len(prs_merged) - MAX_PRS_LIST} más...')
        if prs_opened:
            lines.append(f"  • {len(prs_opened)} nuevos")
        lines.append("")

    # Issues summary
    issues_closed = stats.get("issues_closed", [])
    issues_opened = stats.get("issues_opened", [])
    if issues_closed or issues_opened:
        lines.append("**Issues:**")
        if issues_closed:
            lines.append(f"  • {len(issues_closed)} cerrados")
        if issues_opened:
            lines.append(f"  • {len(issues_opened)} abiertos")
        lines.append("")

    # Authors
    authors = stats.get("authors", [])
    if authors:
        lines.append("**Contribuidores:**")
        for a in authors[:10]:
            lines.append(
                f'  • {a["author_name"]}: {a["commits"]} commits '
                f'(+{a.get("insertions", 0)}/-{a.get("deletions", 0)})'
            )
        lines.append("")

    # Hot files
    hot = stats.get("hot_files", [])
    if hot:
        lines.append("**Archivos calientes:**")
        for f in hot[:10]:
            lines.append(f'  • `{f["file_path"]}` — {f["changes"]} cambios')
        lines.append("")

    return "\n".join(lines)


def render_technical_summary(stats: dict, repo_name: str, date: str) -> str:
    """Resumen técnico programático — datos duros sin LLM.

    Se muestra debajo de la narrativa L3 como complemento factual.
    """
    lines = []
    lines.append("─── Resumen Técnico ───")
    lines.append("")

    # Métricas clave
    n_commits = stats.get("commits", 0)
    n_authors = len(stats.get("authors", []))
    loc_add = stats.get("loc_added", 0)
    loc_del = stats.get("loc_removed", 0)
    ratio = round(loc_add / max(loc_del, 1), 1)

    lines.append(f"Commits: {n_commits}  |  Autores: {n_authors}  |  +{loc_add}/-{loc_del} ({ratio}:1)")
    lines.append("")

    # PRs
    prs_merged = stats.get("prs_merged", [])
    prs_opened = stats.get("prs_opened", [])
    if prs_merged:
        lines.append(f"PRs mergeados ({len(prs_merged)}):")
        for pr in prs_merged:
            lines.append(f"  #{pr['number']} {pr['title']}  — @{pr.get('author', '?')}")
    if prs_opened:
        opened_not_merged = [p for p in prs_opened
                             if p["number"] not in {m["number"] for m in prs_merged}]
        if opened_not_merged:
            lines.append(f"PRs abiertos ({len(opened_not_merged)}):")
            for pr in opened_not_merged:
                lines.append(f"  #{pr['number']} {pr['title']}  — @{pr.get('author', '?')}")
    lines.append("")

    # Issues
    issues_closed = stats.get("issues_closed", [])
    issues_opened = stats.get("issues_opened", [])
    if issues_closed:
        lines.append(f"Issues cerrados ({len(issues_closed)}):")
        for i in issues_closed:
            lines.append(f"  #{i['number']} {i['title']}")
    if issues_opened:
        lines.append(f"Issues abiertos ({len(issues_opened)}):")
        for i in issues_opened:
            lines.append(f"  #{i['number']} {i['title']}")
    lines.append("")

    # Autores
    authors = stats.get("authors", [])
    if authors:
        lines.append("Contribuidores:")
        for a in authors:
            pct = round(a["commits"] / max(n_commits, 1) * 100)
            ins = a.get('insertions', 0)
            dels = a.get('deletions', 0)
            lines.append(
                f"  {_truncate(a['author_name'], 25):<25} {a['commits']:>3} commits  {pct:>5.1f}%  +{ins}/-{dels}"
            )
    lines.append("")

    # Hot files (top 5)
    hot = stats.get("hot_files", [])
    if hot:
        lines.append("Archivos calientes:")
        for f in hot[:5]:
            ins = f.get('insertions', 0)
            dels = f.get('deletions', 0)
            lines.append(
                f"  {_truncate(f['file_path'], 55):<55} {f['changes']:>2}x  +{ins}/-{dels}"
            )
    lines.append("")

    # Branches
    branches = stats.get("branches", [])
    if branches:
        lines.append("Branches activas:")
        for b in branches[:6]:
            lines.append(f"  {_truncate(b['name'], 40):<40} {b['commits']:>3} commits")

    return "\n".join(lines)


_MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]


def _format_date_es(date_str: str) -> str:
    """Convierte '2026-04-16' a '16 de abril de 2026'."""
    try:
        parts = date_str.split("-")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{day} de {_MONTHS_ES[month]} de {year}"
    except (IndexError, ValueError):
        return date_str
