"""L3 LLM — Narrativa semántica via Ollama Cloud."""
from __future__ import annotations
from typing import Any


SYSTEM_PROMPT_DAILY = """Eres un analista senior de proyectos de software. Genera un resumen narrativo
detallado de la actividad de desarrollo del día. Responde siempre en español.

Estructura tu análisis en estas secciones (sin usar headers markdown, solo prosa con separación de párrafos):

1. **Panorama del día:** Qué se logró, quiénes lideraron, volumen de cambios. Menciona branches si hay datos.
2. **Análisis de cambios:** Qué significan los PRs mergeados para el producto. Relación entre issues cerrados y features entregados. Si hay archivos calientes, analiza si indican inestabilidad o desarrollo activo.
3. **Riesgos y señales:** Bugs nuevos, issues stale, archivos con muchos cambios concurrentes. Si hay contexto del día anterior, conecta la narrativa (ej: "ayer se abrió el bug #X y hoy se cerró").
4. **Perspectiva:** Qué queda pendiente, qué debería priorizarse mañana.

Sé específico: nombra PRs, issues, archivos y autores. No uses frases genéricas. Máximo 500 palabras."""

SYSTEM_PROMPT_WEEKLY = """Eres un analista senior de proyectos de software. Genera un balance semanal narrativo detallado de la
actividad de desarrollo. Responde siempre en español.

Estructura tu análisis en estas secciones (sin usar headers markdown, solo prosa con separación de párrafos):

1. **Panorama general:** Volumen total, tendencia vs semanas anteriores, quiénes fueron los principales contribuidores.
2. **Logros entregados:** Features completados, bugs críticos resueltos. Conecta PRs mergeados con issues cerrados.
3. **Análisis técnico:** Hot spots — archivos con más cambios y su implicación para la estabilidad. Identifica áreas de deuda técnica.
4. **Distribución del trabajo:** Por autor y por área funcional (frontend/backend si es inferible de los paths). Detecta silos o dependencias.
5. **Calidad y riesgos:** Balance bugs vs features. Velocidad de review. Issues que permanecieron abiertos toda la semana.
6. **Perspectiva:** Qué debería priorizarse la próxima semana basado en el estado actual.

Sé específico: nombra PRs, issues, archivos y autores. No uses frases genéricas. Máximo 800 palabras."""


def generate_daily_narrative(stats: dict[str, Any], repo_name: str,
                             date: str, ollama_client, context=None) -> str | None:
    """Genera narrativa L3 diaria usando Ollama Cloud.

    Returns None si Ollama no está disponible o falla.
    El caller debe hacer fallback a L2.
    """
    if not ollama_client:
        return None

    # Construir prompt con los datos del día
    user_content = _build_daily_prompt(stats, repo_name, date, context=context)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_DAILY},
        {"role": "user", "content": user_content},
    ]

    return ollama_client.chat(messages, max_tokens=1000)


def generate_weekly_narrative(stats: dict[str, Any], repo_name: str,
                              week_start: str, ollama_client) -> str | None:
    """Genera narrativa L3 semanal usando Ollama Cloud."""
    if not ollama_client:
        return None

    user_content = _build_weekly_prompt(stats, repo_name, week_start)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_WEEKLY},
        {"role": "user", "content": user_content},
    ]

    return ollama_client.chat(messages, max_tokens=1200)


def _build_daily_prompt(stats: dict, repo_name: str, date: str,
                        context: dict | None = None) -> str:
    """Construye el prompt con datos del día para el LLM."""
    parts = [f"Repo: {repo_name}, Fecha: {date}"]
    parts.append(f"Commits: {stats.get('commits', 0)}")
    parts.append(f"Líneas: +{stats.get('loc_added', 0)}/-{stats.get('loc_removed', 0)}")

    # Branches activas
    branches = stats.get("branches", [])
    if branches:
        branch_strs = [f"{b['name']} ({b['commits']} commits)" for b in branches[:8]]
        parts.append(f"Branches activas: {', '.join(branch_strs)}")

    authors = stats.get("authors", [])
    if authors:
        author_strs = [f"{a['author_name']} ({a['commits']} commits)" for a in authors[:5]]
        parts.append(f"Autores: {', '.join(author_strs)}")

    prs_merged = stats.get("prs_merged", [])
    if prs_merged:
        pr_strs = [f'#{pr["number"]} "{pr["title"]}"' for pr in prs_merged]
        parts.append(f"PRs mergeados: {', '.join(pr_strs)}")

    prs_opened = stats.get("prs_opened", [])
    if prs_opened:
        pr_strs = [f'#{pr["number"]} "{pr["title"]}"' for pr in prs_opened]
        parts.append(f"PRs nuevos: {', '.join(pr_strs)}")

    issues_closed = stats.get("issues_closed", [])
    if issues_closed:
        closed_strs = [f'#{i["number"]}' for i in issues_closed]
        parts.append(f"Issues cerrados: {', '.join(closed_strs)}")

    issues_opened = stats.get("issues_opened", [])
    if issues_opened:
        opened_strs = [f'#{i["number"]} {i["title"]}' for i in issues_opened]
        parts.append(f"Issues nuevos: {', '.join(opened_strs)}")

    hot = stats.get("hot_files", [])
    if hot:
        file_strs = [f"{f['file_path']} ({f['changes']}x)" for f in hot[:5]]
        parts.append(f"Archivos calientes: {', '.join(file_strs)}")

    # Contexto de continuidad (Bug 6)
    if context:
        if context.get("yesterday_summary"):
            parts.append("")
            parts.append(f"Contexto del día anterior ({context['yesterday_date']}):")
            parts.append(context["yesterday_summary"])

        if context.get("stale_issues"):
            parts.append("")
            stale_strs = [f"#{i['number']}" for i in context["stale_issues"][:5]]
            parts.append(f"Issues abiertos >3 días: {', '.join(stale_strs)}")

    return "\n".join(parts)


def _build_weekly_prompt(stats: dict, repo_name: str, week_start: str) -> str:
    """Construye el prompt con datos de la semana."""
    from datetime import datetime, timedelta
    end = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)

    parts = [f"Repo: {repo_name}, Semana: {week_start} al {end.strftime('%Y-%m-%d')}"]
    parts.append(f"Commits totales: {stats.get('commits', 0)}")
    parts.append(f"Líneas: +{stats.get('loc_added', 0)}/-{stats.get('loc_removed', 0)}")

    # Autores con detalle
    authors = stats.get("authors", [])
    parts.append(f"Autores activos: {len(authors)}")
    if authors:
        for a in authors[:8]:
            parts.append(f"  – {a['author_name']}: {a['commits']}c (+{a.get('insertions', 0)}/-{a.get('deletions', 0)})")

    # Branches activas
    branches = stats.get("branches", [])
    if branches:
        parts.append(f"Branches activas: {len(branches)}")
        for b in branches[:6]:
            parts.append(f"  – {b['name']}: {b['commits']} commits")

    # PRs con conteos
    prs_merged = stats.get("prs_merged", [])
    prs_opened = stats.get("prs_opened", [])
    parts.append(f"PRs mergeados: {len(prs_merged)}")
    parts.append(f"PRs nuevos: {len(prs_opened)}")
    if prs_merged:
        parts.append("PRs destacados:")
        for pr in prs_merged[:5]:
            parts.append(f'  – #{pr["number"]}: "{pr["title"]}" (@{pr.get("author", "?")})')

    # Issues con balance
    issues_closed = stats.get("issues_closed", [])
    issues_opened = stats.get("issues_opened", [])
    parts.append(f"Issues cerrados: {len(issues_closed)}")
    parts.append(f"Issues nuevos: {len(issues_opened)}")

    # Categorizar issues por título (simple heuristic)
    bugs_closed = [i for i in issues_closed if any(kw in i['title'].lower() for kw in ['bug', 'fix', 'error', 'crash', 'issue'])]
    features_closed = [i for i in issues_closed if any(kw in i['title'].lower() for kw in ['feature', 'add', 'new', 'implement'])]
    if bugs_closed or features_closed:
        parts.append(f"  – Bugs resueltos: ~{len(bugs_closed)}")
        parts.append(f"  – Features entregados: ~{len(features_closed)}")

    # Hot files con detalle
    hot = stats.get("hot_files", [])
    if hot:
        parts.append("Archivos más modificados:")
        for f in hot[:8]:
            parts.append(f"  – {f['file_path']}: {f['changes']} cambios (+{f.get('insertions', 0)}/-{f.get('deletions', 0)})")

    return "\n".join(parts)
