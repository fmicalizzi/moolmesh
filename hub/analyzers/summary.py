"""Session summary analyzer — overview of all activity."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from hub.models.base import MessageRole, UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class SummaryAnalyzer(BaseAnalyzer):
    name = "00_resumen_sesiones"
    title = "Resumen de Sesiones"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        by_provider: dict[str, dict] = defaultdict(lambda: {
            "sessions": set(),
            "messages": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_uses": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation": 0,
            "cache_read": 0,
            "models": set(),
            "projects": set(),
        })

        by_session: dict[str, dict] = defaultdict(lambda: {
            "provider": "",
            "project": "",
            "messages": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "first_ts": "",
            "last_ts": "",
            "models": set(),
        })

        total_messages = 0
        for msg in messages:
            p = msg.provider.value
            s = by_provider[p]
            s["sessions"].add(msg.session_id)
            s["messages"] += 1
            s["projects"].add(msg.project)
            if msg.model:
                s["models"].add(msg.model)
            if msg.role == MessageRole.USER:
                s["user_messages"] += 1
            elif msg.role == MessageRole.ASSISTANT:
                s["assistant_messages"] += 1
            elif msg.role == MessageRole.TOOL_USE:
                s["tool_uses"] += 1
            if msg.tokens:
                s["input_tokens"] += msg.tokens.input_tokens
                s["output_tokens"] += msg.tokens.output_tokens
                s["cache_creation"] += msg.tokens.cache_creation
                s["cache_read"] += msg.tokens.cache_read

            # Per-session
            sid = f"{p}:{msg.session_id}"
            ss = by_session[sid]
            ss["provider"] = p
            ss["project"] = msg.project
            ss["messages"] += 1
            if msg.tokens:
                ss["input_tokens"] += msg.tokens.input_tokens
                ss["output_tokens"] += msg.tokens.output_tokens
            ts = str(msg.timestamp or "")
            if not ss["first_ts"] or ts < ss["first_ts"]:
                ss["first_ts"] = ts
            if ts > ss["last_ts"]:
                ss["last_ts"] = ts
            if msg.model:
                ss["models"].add(msg.model)

            total_messages += 1

        # Serialize sets
        for p, s in by_provider.items():
            s["sessions"] = len(s["sessions"])
            s["models"] = sorted(s["models"])
            s["projects"] = sorted(s["projects"])

        sessions_list = []
        for sid, ss in sorted(by_session.items(), key=lambda x: x[1]["first_ts"]):
            sessions_list.append({
                **ss,
                "session_id": sid,
                "models": sorted(ss["models"]),
            })

        return {
            "total_messages": total_messages,
            "by_provider": dict(by_provider),
            "sessions": sessions_list,
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Total mensajes:** {results['total_messages']}\n")

        for prov, stats in results["by_provider"].items():
            lines.append(f"\n## {prov.upper()}")
            lines.append(f"- Sesiones: {stats['sessions']}")
            lines.append(f"- Mensajes: {stats['messages']} (user: {stats['user_messages']}, assistant: {stats['assistant_messages']}, tool_use: {stats['tool_uses']})")
            lines.append(f"- Tokens: input={stats['input_tokens']:,} output={stats['output_tokens']:,} cache_create={stats['cache_creation']:,} cache_read={stats['cache_read']:,}")
            total = stats['input_tokens'] + stats['output_tokens']
            lines.append(f"- Total tokens: {total:,}")
            lines.append(f"- Modelos: {', '.join(stats['models'])}")
            lines.append(f"- Proyectos: {', '.join(stats['projects'])}")

        lines.append(f"\n## Sesiones ({len(results['sessions'])})\n")
        lines.append("| Provider | Proyecto | Mensajes | Input Tok | Output Tok | Inicio | Modelos |")
        lines.append("|----------|----------|----------|-----------|------------|--------|---------|")
        for s in results["sessions"]:
            lines.append(
                f"| {s['provider']} | {s['project']} | {s['messages']} | "
                f"{s['input_tokens']:,} | {s['output_tokens']:,} | "
                f"{s['first_ts'][:19]} | {', '.join(s['models'])} |"
            )

        return "\n".join(lines) + "\n"
