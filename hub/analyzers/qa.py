"""Enhanced Q&A analyzer — pairs user questions with assistant answers and tool activity."""

from __future__ import annotations

from typing import Any

from hub.models.base import MessageRole, UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class QAAnalyzer(BaseAnalyzer):
    name = "05_qa_mejorado_con_operaciones"
    title = "Preguntas y Respuestas con Operaciones"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        # Group messages by session
        by_session: dict[str, list[UnifiedMessage]] = {}
        for msg in messages:
            key = f"{msg.provider.value}:{msg.session_id}"
            by_session.setdefault(key, []).append(msg)

        qa_pairs: list[dict] = []

        for session_key, session_msgs in by_session.items():
            session_msgs.sort(key=lambda m: str(m.timestamp or ""))

            current_q: UnifiedMessage | None = None
            current_tools: list[UnifiedMessage] = []
            current_answers: list[UnifiedMessage] = []

            for msg in session_msgs:
                if msg.role == MessageRole.USER:
                    if current_q is not None:
                        qa_pairs.append(self._make_pair(
                            current_q, current_answers, current_tools
                        ))
                    current_q = msg
                    current_tools = []
                    current_answers = []
                elif msg.role in (MessageRole.TOOL_USE, MessageRole.TOOL_RESULT):
                    current_tools.append(msg)
                elif msg.role == MessageRole.ASSISTANT and msg.text.strip():
                    current_answers.append(msg)

            if current_q is not None:
                qa_pairs.append(self._make_pair(
                    current_q, current_answers, current_tools
                ))

        return {"qa_pairs": qa_pairs, "total": len(qa_pairs)}

    def _make_pair(
        self,
        question: UnifiedMessage,
        answers: list[UnifiedMessage],
        tools: list[UnifiedMessage],
    ) -> dict:
        answer = answers[-1] if answers else None
        # Build tool summary grouped by tool name
        tool_summary: list[dict] = []
        files_touched: set[str] = set()
        for t in tools:
            for tc in t.tool_calls:
                tool_summary.append({
                    "tool": tc.name,
                    "file": tc.file_path,
                    "op": tc.operation_type,
                    "args_brief": self._brief_args(tc),
                })
                if tc.file_path:
                    files_touched.add(tc.file_path)

        # Group tools by name for summary
        tool_counts: dict[str, int] = {}
        for ts in tool_summary:
            tool_counts[ts["tool"]] = tool_counts.get(ts["tool"], 0) + 1

        # Calculate duration if both timestamps exist
        duration = ""
        if question.timestamp and answer and answer.timestamp:
            try:
                delta = answer.timestamp - question.timestamp
                secs = int(delta.total_seconds())
                if secs >= 0:
                    mins, s = divmod(secs, 60)
                    duration = f"{mins}m{s:02d}s" if mins else f"{s}s"
            except (TypeError, ValueError):
                pass

        return {
            "provider": question.provider.value,
            "project": question.project,
            "session_id": question.session_id,
            "timestamp": str(question.timestamp or ""),
            "end_timestamp": str(answer.timestamp or "") if answer else "",
            "duration": duration,
            "question": question.text,
            "answer": (answer.text if answer else ""),
            "answer_all": [a.text for a in answers],
            "answer_model": answer.model if answer else None,
            "answer_tokens": {
                "input": answer.tokens.input_tokens if answer and answer.tokens else 0,
                "output": answer.tokens.output_tokens if answer and answer.tokens else 0,
            },
            "tools_used": tool_summary,
            "tool_count": len(tool_summary),
            "tool_counts": tool_counts,
            "files_touched": sorted(files_touched),
        }

    @staticmethod
    def _brief_args(tc) -> str:
        if not tc.input_data:
            return ""
        cmd = tc.input_data.get("command", "")
        if cmd:
            return cmd
        fp = tc.input_data.get("file_path", tc.input_data.get("path", ""))
        if fp:
            return fp
        pat = tc.input_data.get("pattern", "")
        if pat:
            return pat
        return str(list(tc.input_data.keys()))

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Total Q&A pairs:** {results['total']}\n")

        for i, qa in enumerate(results["qa_pairs"], 1):
            lines.append(f"\n---\n\n### Q{i} [{qa['provider']}] {qa['project']} — {qa['timestamp'][:19]}")

            # Duration and timing
            if qa.get("duration"):
                lines.append(f"**Duración:** {qa['duration']}")

            # Full question
            lines.append("\n**Pregunta:**\n")
            lines.append(f"```\n{qa['question']}\n```\n")

            # Tool operations summary
            if qa["tools_used"]:
                tool_counts = qa.get("tool_counts", {})
                tools_str = ", ".join(f"{name}({count})" for name, count in
                                     sorted(tool_counts.items(), key=lambda x: -x[1]))
                files_touched = qa.get("files_touched", [])

                lines.append(f"**Operaciones ({qa['tool_count']}):** {tools_str}")
                if files_touched:
                    file_limit = None if self.complete else 15
                    files_show = files_touched if self.complete else files_touched[:file_limit]
                    lines.append(f"**Archivos tocados ({len(files_touched)}):**")
                    for fp in files_show:
                        lines.append(f"- `{fp}`")
                    remaining_files = len(files_touched) - len(files_show)
                    if remaining_files > 0:
                        lines.append(f"- ... y {remaining_files} más")

                tool_limit = None if self.complete else 30
                tools_show = qa["tools_used"] if self.complete else qa["tools_used"][:tool_limit]
                lines.append("\n<details><summary>Detalle herramientas</summary>\n")
                for t in tools_show:
                    file_info = f" `{t['file']}`" if t.get("file") else ""
                    args = f" — `{t['args_brief']}`" if t.get("args_brief") else ""
                    lines.append(f"- **{t['tool']}**{file_info}{args}")
                remaining_tools = len(qa["tools_used"]) - len(tools_show)
                if remaining_tools > 0:
                    lines.append(f"- ... y {remaining_tools} operaciones más")
                lines.append("\n</details>\n")

            # Answer(s)
            all_answers = qa.get("answer_all", [])
            if self.complete and len(all_answers) > 1:
                model = qa['answer_model'] or 'unknown'
                tok_in = qa['answer_tokens']['input']
                tok_out = qa['answer_tokens']['output']
                lines.append(f"**Respuestas ({len(all_answers)} partes)** ({model}, tokens: {tok_in:,}in + {tok_out:,}out):\n")
                for j, ans_text in enumerate(all_answers, 1):
                    lines.append(f"**[Parte {j}]**\n")
                    lines.append(f"{ans_text}\n")
            elif qa["answer"]:
                model = qa['answer_model'] or 'unknown'
                tok_in = qa['answer_tokens']['input']
                tok_out = qa['answer_tokens']['output']
                lines.append(f"**Respuesta** ({model}, tokens: {tok_in:,}in + {tok_out:,}out):\n")
                lines.append(f"{qa['answer']}\n")

        return "\n".join(lines) + "\n"
