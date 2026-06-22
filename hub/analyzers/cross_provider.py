"""Cross-provider analyzer — comparison between providers."""

from __future__ import annotations

from typing import Any

from hub.models.base import MessageRole, UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class CrossProviderAnalyzer(BaseAnalyzer):
    name = "10_cross_provider"
    title = "Análisis Cross-Provider"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        by_provider: dict[str, dict[str, Any]] = {}
        for m in messages:
            prov = m.provider.value
            if prov not in by_provider:
                by_provider[prov] = {
                    "messages": 0, "sessions": set(), "tool_calls": 0,
                    "user_messages": 0, "models": set(),
                }
            stats = by_provider[prov]
            stats["messages"] += 1
            if m.session_id:
                stats["sessions"].add(m.session_id)
            if m.role == MessageRole.TOOL_USE:
                stats["tool_calls"] += 1
            if m.role == MessageRole.USER:
                stats["user_messages"] += 1
            if m.model:
                stats["models"].add(m.model)

        for stats in by_provider.values():
            stats["sessions"] = len(stats["sessions"])
            stats["models"] = sorted(stats["models"])

        return {
            "providers": by_provider,
            "total_providers": len(by_provider),
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Providers activos:** {results['total_providers']}\n")

        lines.append("## Comparativa\n")
        lines.append("| Provider | Sesiones | Mensajes | User Msgs | Tool Calls | Modelos |")
        lines.append("|----------|----------|----------|-----------|------------|---------|")
        for prov, stats in sorted(results["providers"].items()):
            models = ", ".join(stats["models"][:5])
            lines.append(
                f"| {prov} | {stats['sessions']} | {stats['messages']} | "
                f"{stats['user_messages']} | {stats['tool_calls']} | {models} |"
            )

        return "\n".join(lines) + "\n"
