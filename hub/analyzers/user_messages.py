"""User messages analyzer — history of user inputs grouped by session."""

from __future__ import annotations

from collections import Counter
from typing import Any

from hub.models.base import MessageRole, UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class UserMessagesAnalyzer(BaseAnalyzer):
    name = "01_historico_mensajes_usuario"
    title = "Histórico de Mensajes del Usuario"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        user_msgs = [m for m in messages if m.role == MessageRole.USER and m.text.strip()]

        session_limit = None if self.complete else 20
        by_session = dict(
            Counter(m.session_id for m in user_msgs if m.session_id).most_common(session_limit)
        )

        return {
            "total": len(user_msgs),
            "messages": [
                {
                    "timestamp": str(m.timestamp) if m.timestamp else "",
                    "session_id": m.session_id or "",
                    "provider": m.provider.value,
                    "text": m.text,
                    "project": m.project or "",
                    "cwd": m.cwd or "",
                }
                for m in user_msgs
            ],
            "avg_length": sum(len(m.text) for m in user_msgs) // max(len(user_msgs), 1),
            "by_session": by_session,
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Total mensajes del usuario:** {results['total']}")
        lines.append(f"**Longitud promedio:** {results['avg_length']} caracteres\n")

        if results["by_session"]:
            label = "Mensajes por Sesión" if self.complete else "Mensajes por Sesión (top 20)"
            lines.append(f"## {label}\n")
            lines.append("| Sesión | Mensajes |")
            lines.append("|--------|----------|")
            for sid, count in results["by_session"].items():
                sid_display = sid if self.complete else f"{sid[:20]}..."
                lines.append(f"| {sid_display} | {count} |")

        msg_list = results["messages"] if self.complete else results["messages"][:100]
        lines.append(f"\n## Mensajes ({results['total']})\n")

        if self.complete:
            for msg in msg_list:
                ts = msg["timestamp"][:19] if msg["timestamp"] else "?"
                lines.append(f"### [{ts}] `{msg['provider']}` — {msg['session_id']}")
                if msg.get("cwd"):
                    lines.append(f"**CWD:** `{msg['cwd']}`")
                lines.append(f"\n```\n{msg['text']}\n```\n")
        else:
            for msg in msg_list:
                ts = msg["timestamp"][:19] if msg["timestamp"] else "?"
                text = msg["text"].replace("\n", " ")[:120]
                lines.append(f"- **[{ts}]** `{msg['provider']}` {text}")

        return "\n".join(lines) + "\n"
