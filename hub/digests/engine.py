"""DigestEngine — Orquesta L1 Stats → L2 Template → L3 LLM con cache."""
from __future__ import annotations

from datetime import datetime

from hub.cache.git_store import GitStore
from hub.digests.stats import compute_daily_stats, compute_weekly_stats
from hub.digests.template import render_daily, render_weekly, render_technical_summary
from hub.digests.llm import generate_daily_narrative, generate_weekly_narrative


class DigestEngine:
    """Motor de digests con cache en SQLite.

    Pipeline:
    1. Check cache en github.db (daily_digests table)
    2. Compute L1 stats (SQL puro, siempre funciona)
    3. Render L2 template (texto en español, siempre funciona)
    4. Try L3 via Ollama (fallback a L2 si falla)
    5. Cache resultado

    El digest siempre retorna al menos L2. L3 es un upgrade.
    """

    def __init__(self, git_store: GitStore, ollama_client=None):
        self._store = git_store
        self._ollama = ollama_client

    def get_daily_digest(self, repo_id: int, date: str,
                         repo_name: str = "",
                         force_refresh: bool = False,
                         allow_llm: bool = True) -> dict:
        """Obtiene digest diario. Usa cache si existe.

        Args:
            repo_id: ID en tabla repos
            date: "YYYY-MM-DD"
            repo_name: "owner/repo" para display
            force_refresh: ignorar cache y regenerar
            allow_llm: si False, nunca llama Ollama aunque esté disponible.
                      Usar False para fechas históricas (semanas anteriores)
                      salvo regeneración explícita. force_refresh=True
                      siempre implica allow_llm=True.

        Returns:
            {
                "level": 1|2|3,
                "period": "daily",
                "date": "YYYY-MM-DD",
                "stats": {...},     # L1 siempre presente
                "text": "...",      # L2 template o L3 narrativa
                "narrative": "...", # L3 narrativa o "" si no disponible
            }
        """
        # Check cache
        if not force_refresh:
            cached = self._load_cached(repo_id, date, "daily")
            if cached:
                return cached

        # L1: Stats
        stats = compute_daily_stats(self._store, repo_id, date)

        # L2: Template
        text = render_daily(stats, repo_name, date)
        level = 2

        # Computar contexto de continuidad
        context = self._build_continuity_context(repo_id, date, repo_name)

        # L3: LLM narrative (optional upgrade)
        # Solo llamar Ollama si está permitido Y disponible
        narrative = ""
        ollama_status = "not_configured"
        if self._ollama and (allow_llm or force_refresh):
            ollama_status = "attempted"
            result = generate_daily_narrative(stats, repo_name, date, self._ollama, context=context)
            if result:
                narrative = result
                level = 3
                ollama_status = "success"
            else:
                ollama_status = "failed"
        elif not (allow_llm or force_refresh):
            ollama_status = "skipped_historical"

        # Resumen técnico programático (siempre presente)
        technical = render_technical_summary(stats, repo_name, date)

        digest = {
            "level": level,
            "period": "daily",
            "date": date,
            "stats": stats,
            "text": narrative if narrative else text,
            "narrative": narrative,
            "technical_summary": technical,
            "ollama_status": ollama_status,
        }

        # NUEVO: borrar cache existente antes de guardar cuando es force_refresh
        if force_refresh:
            self._store.delete_cached(repo_id, date, "daily")

        # Cache
        self._save_cached(repo_id, date, "daily", level, digest)

        return digest

    def get_weekly_digest(self, repo_id: int, week_start: str,
                          repo_name: str = "",
                          force_refresh: bool = False,
                          allow_llm: bool = True) -> dict:
        """Obtiene digest semanal. week_start debe ser un lunes ("YYYY-MM-DD").

        Args:
            repo_id: ID en tabla repos
            week_start: "YYYY-MM-DD" (lunes de la semana)
            repo_name: "owner/repo" para display
            force_refresh: ignorar cache y regenerar
            allow_llm: si False, nunca llama Ollama aunque esté disponible.
                      Usar False para semanas históricas salvo regeneración
                      explícita. force_refresh=True siempre implica allow_llm=True.
        """
        if not force_refresh:
            cached = self._load_cached(repo_id, week_start, "weekly")
            if cached:
                return cached

        stats = compute_weekly_stats(self._store, repo_id, week_start)
        text = render_weekly(stats, repo_name, week_start)
        level = 2

        # L3: LLM narrative (optional upgrade)
        # Solo llamar Ollama si está permitido Y disponible
        narrative = ""
        ollama_status = "not_configured"
        if self._ollama and (allow_llm or force_refresh):
            ollama_status = "attempted"
            result = generate_weekly_narrative(stats, repo_name, week_start, self._ollama)
            if result:
                narrative = result
                level = 3
                ollama_status = "success"
            else:
                ollama_status = "failed"
        elif not (allow_llm or force_refresh):
            ollama_status = "skipped_historical"

        # Resumen técnico programático (siempre presente)
        technical = render_technical_summary(stats, repo_name, week_start)

        digest = {
            "level": level,
            "period": "weekly",
            "date": week_start,
            "stats": stats,
            "text": narrative if narrative else text,
            "narrative": narrative,
            "technical_summary": technical,
            "ollama_status": ollama_status,
        }

        # NUEVO: borrar cache existente antes de guardar cuando es force_refresh
        if force_refresh:
            self._store.delete_cached(repo_id, week_start, "weekly")

        self._save_cached(repo_id, week_start, "weekly", level, digest)

        return digest

    def _build_continuity_context(self, repo_id: int, date: str, repo_name: str) -> dict:
        """Construye contexto del día anterior + issues stale para continuidad."""
        from datetime import timedelta

        context = {}

        # 1. Resumen del día anterior
        prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_since = f"{prev_date}T00:00:00"
        prev_until = f"{prev_date}T23:59:59"

        prev_commits = self._store.count_commits(repo_id, prev_since, prev_until)

        # Issues abiertos/cerrados ayer
        all_issues = self._store.get_issues(repo_id)
        prev_issues_opened = [i["number"] for i in all_issues
                              if i.get("created_at", "").startswith(prev_date)
                              and not i.get("is_pull_request")]
        prev_issues_closed = [i["number"] for i in all_issues
                              if (i.get("closed_at") or "").startswith(prev_date)
                              and not i.get("is_pull_request")]
        prev_prs_merged = [i["number"] for i in all_issues
                           if (i.get("pr_merged_at") or "").startswith(prev_date)
                           and i.get("is_pull_request")]

        if prev_commits or prev_issues_opened or prev_issues_closed or prev_prs_merged:
            summary_parts = []
            if prev_commits:
                summary_parts.append(f"{prev_commits} commits")
            if prev_prs_merged:
                summary_parts.append(f"PRs mergeados: {', '.join(f'#{n}' for n in prev_prs_merged)}")
            if prev_issues_opened:
                summary_parts.append(f"issues abiertos: {', '.join(f'#{n}' for n in prev_issues_opened)}")
            if prev_issues_closed:
                summary_parts.append(f"issues cerrados: {', '.join(f'#{n}' for n in prev_issues_closed)}")

            context["yesterday_date"] = prev_date
            context["yesterday_summary"] = "; ".join(summary_parts)

        # 2. Issues stale (>3 días abiertos)
        stale = []
        for issue in all_issues:
            if issue["state"] == "open" and not issue.get("is_pull_request"):
                created = issue.get("created_at", "")[:10]
                if created:
                    try:
                        days_open = (datetime.strptime(date, "%Y-%m-%d") -
                                    datetime.strptime(created, "%Y-%m-%d")).days
                        if days_open > 3:
                            stale.append({
                                "number": issue["number"],
                                "title": issue["title"],
                                "days_open": days_open,
                            })
                    except ValueError:
                        pass

        if stale:
            context["stale_issues"] = sorted(stale, key=lambda x: -x["days_open"])[:5]

        return context

    def _load_cached(self, repo_id: int, date: str, period: str) -> dict | None:
        """Intenta cargar del cache. Prioriza L3 > L2."""
        for level in (3, 2):
            cached = self._store.get_digest(repo_id, date, period, level)
            if cached:
                return cached
        return None

    def _save_cached(self, repo_id: int, date: str, period: str,
                     level: int, digest: dict) -> None:
        """Guarda en cache."""
        self._store.store_digest(repo_id, date, period, level, digest)
