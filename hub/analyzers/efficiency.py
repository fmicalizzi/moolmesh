"""Token efficiency analyzer — consumption, cache rates, cost estimation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from hub.models.base import UnifiedMessage
from hub.analyzers.base import BaseAnalyzer

# Approximate costs per 1M tokens (USD) — Claude Sonnet 4 pricing
_COSTS = {
    "input": 3.0,
    "output": 15.0,
    "cache_write": 3.75,
    "cache_read": 0.30,
}


class EfficiencyAnalyzer(BaseAnalyzer):
    name = "09_eficiencia_tokens"
    title = "Eficiencia y Consumo de Tokens"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
        by_model: dict[str, dict] = defaultdict(
            lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "messages": 0}
        )
        by_provider: dict[str, dict] = defaultdict(
            lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "messages": 0}
        )
        by_session: dict[str, dict] = defaultdict(
            lambda: {"input": 0, "output": 0, "total": 0, "messages": 0, "provider": "", "project": ""}
        )

        msg_count = 0
        for msg in messages:
            if not msg.tokens:
                continue
            t = msg.tokens
            totals["input"] += t.input_tokens
            totals["output"] += t.output_tokens
            totals["cache_creation"] += t.cache_creation
            totals["cache_read"] += t.cache_read
            msg_count += 1

            model = msg.model or "unknown"
            m = by_model[model]
            m["input"] += t.input_tokens
            m["output"] += t.output_tokens
            m["cache_creation"] += t.cache_creation
            m["cache_read"] += t.cache_read
            m["messages"] += 1

            prov = msg.provider.value
            p = by_provider[prov]
            p["input"] += t.input_tokens
            p["output"] += t.output_tokens
            p["cache_creation"] += t.cache_creation
            p["cache_read"] += t.cache_read
            p["messages"] += 1

            sid = f"{prov}:{msg.session_id}"
            s = by_session[sid]
            s["input"] += t.input_tokens
            s["output"] += t.output_tokens
            s["total"] += t.input_tokens + t.output_tokens
            s["messages"] += 1
            s["provider"] = prov
            s["project"] = msg.project

        grand_total = totals["input"] + totals["output"]
        cache_total = totals["cache_creation"] + totals["cache_read"]
        cache_hit_rate = (totals["cache_read"] / max(1, cache_total)) * 100

        # Estimated cost
        cost = (
            (totals["input"] / 1e6) * _COSTS["input"]
            + (totals["output"] / 1e6) * _COSTS["output"]
            + (totals["cache_creation"] / 1e6) * _COSTS["cache_write"]
            + (totals["cache_read"] / 1e6) * _COSTS["cache_read"]
        )

        # Session ranking by total tokens
        session_ranking = sorted(
            [{"session": k, **v} for k, v in by_session.items()],
            key=lambda x: x["total"],
            reverse=True,
        )

        return {
            "totals": totals,
            "grand_total": grand_total,
            "cache_hit_rate": round(cache_hit_rate, 1),
            "estimated_cost_usd": round(cost, 4),
            "messages_with_tokens": msg_count,
            "by_model": dict(by_model),
            "by_provider": dict(by_provider),
            "session_ranking": session_ranking,
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        t = results["totals"]
        lines = [f"# {self.title}\n"]

        lines.append("## Totales\n")
        lines.append("| Metrica | Valor |")
        lines.append("|---------|-------|")
        lines.append(f"| Input tokens | {t['input']:,} |")
        lines.append(f"| Output tokens | {t['output']:,} |")
        lines.append(f"| Cache creation | {t['cache_creation']:,} |")
        lines.append(f"| Cache read | {t['cache_read']:,} |")
        lines.append(f"| **Total tokens** | **{results['grand_total']:,}** |")
        lines.append(f"| Cache hit rate | {results['cache_hit_rate']}% |")
        lines.append(f"| Costo estimado | ${results['estimated_cost_usd']:.4f} USD |")
        lines.append(f"| Mensajes con tokens | {results['messages_with_tokens']} |")

        lines.append("\n## Por Modelo\n")
        lines.append("| Modelo | Msgs | Input | Output | Cache Create | Cache Read |")
        lines.append("|--------|------|-------|--------|--------------|------------|")
        for model, stats in sorted(results["by_model"].items(), key=lambda x: x[1]["input"], reverse=True):
            lines.append(
                f"| {model} | {stats['messages']} | {stats['input']:,} | "
                f"{stats['output']:,} | {stats['cache_creation']:,} | {stats['cache_read']:,} |"
            )

        lines.append("\n## Por Provider\n")
        lines.append("| Provider | Msgs | Input | Output | Total |")
        lines.append("|----------|------|-------|--------|-------|")
        for prov, stats in sorted(results["by_provider"].items()):
            total = stats["input"] + stats["output"]
            lines.append(f"| {prov} | {stats['messages']} | {stats['input']:,} | {stats['output']:,} | {total:,} |")

        lines.append("\n## Top Sesiones por Consumo\n")
        lines.append("| # | Provider | Proyecto | Msgs | Total Tokens |")
        lines.append("|---|----------|----------|------|-------------|")
        ranking = results["session_ranking"] if self.complete else results["session_ranking"][:15]
        for i, s in enumerate(ranking, 1):
            lines.append(f"| {i} | {s['provider']} | {s['project']} | {s['messages']} | {s['total']:,} |")

        return "\n".join(lines) + "\n"
