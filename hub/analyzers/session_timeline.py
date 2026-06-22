"""Session timeline analyzer — sessions with duration, messages, tool calls."""

from __future__ import annotations

from typing import Any

from hub.models.base import MessageRole, UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class SessionTimelineAnalyzer(BaseAnalyzer):
    name = "02_timeline_sesiones"
    title = "Timeline de Sesiones"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        sessions: dict[str, list[UnifiedMessage]] = {}
        for m in messages:
            sid = m.session_id or "unknown"
            sessions.setdefault(sid, []).append(m)

        timeline = []
        for sid, msgs in sessions.items():
            msgs.sort(key=lambda m: str(m.timestamp or ""))
            first = msgs[0]
            last = msgs[-1]
            user_count = sum(1 for m in msgs if m.role == MessageRole.USER)
            tool_count = sum(1 for m in msgs if m.role == MessageRole.TOOL_USE)
            providers = sorted(set(m.provider.value for m in msgs))

            duration_min = 0
            if first.timestamp and last.timestamp:
                delta = (last.timestamp - first.timestamp).total_seconds()
                duration_min = int(delta / 60) if delta > 0 else 0

            timeline.append({
                "session_id": sid,
                "provider": providers[0] if len(providers) == 1 else "+".join(providers),
                "start": str(first.timestamp) if first.timestamp else "",
                "end": str(last.timestamp) if last.timestamp else "",
                "duration_min": duration_min,
                "messages": len(msgs),
                "user_messages": user_count,
                "tool_calls": tool_count,
                "model": first.model or "",
                "project": first.project or "",
                "cwd": first.cwd or "",
            })

        timeline.sort(key=lambda s: s["start"], reverse=True)

        return {
            "total_sessions": len(timeline),
            "total_duration_min": sum(s["duration_min"] for s in timeline),
            "sessions": timeline,
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Total sesiones:** {results['total_sessions']}")
        lines.append(f"**Duración total:** {results['total_duration_min']} minutos\n")

        lines.append("## Sesiones\n")
        lines.append("| Inicio | Duración | Provider | Msgs | User | Tools | Modelo | Proyecto |")
        lines.append("|--------|----------|----------|------|------|-------|--------|----------|")
        for s in results["sessions"]:
            start = s["start"][:19] if s["start"] else "?"
            lines.append(
                f"| {start} | {s['duration_min']}m | {s['provider']} | "
                f"{s['messages']} | {s['user_messages']} | {s['tool_calls']} | "
                f"{s['model']} | {s['project']} |"
            )

        return "\n".join(lines) + "\n"
