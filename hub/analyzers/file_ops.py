"""File operations analyzer — tracks reads, writes, edits, commands."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from hub.models.base import UnifiedMessage
from hub.analyzers.base import BaseAnalyzer


class FileOpsAnalyzer(BaseAnalyzer):
    name = "04_operaciones_archivos"
    title = "Operaciones de Archivos"

    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        operations: list[dict] = []
        by_tool: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        files_touched: dict[str, list[str]] = defaultdict(list)  # file -> [ops]

        for msg in messages:
            for tc in msg.tool_calls:
                by_tool[tc.name] += 1
                op_type = tc.operation_type or "unknown"
                by_type[op_type] += 1

                op = {
                    "timestamp": str(msg.timestamp or ""),
                    "provider": msg.provider.value,
                    "project": msg.project,
                    "tool": tc.name,
                    "operation": op_type,
                    "file": tc.file_path or "",
                    "session_id": msg.session_id,
                }

                # Brief description
                if tc.name == "Bash":
                    cmd = tc.input_data.get("command", "")[:120]
                    op["detail"] = cmd
                elif tc.file_path:
                    op["detail"] = tc.file_path
                else:
                    op["detail"] = str(list(tc.input_data.keys()))[:80]

                operations.append(op)

                if tc.file_path:
                    files_touched[tc.file_path].append(op_type)

        # Most touched files
        hot_files = sorted(
            [{"file": f, "ops": len(ops), "types": list(set(ops))} for f, ops in files_touched.items()],
            key=lambda x: x["ops"],
            reverse=True,
        )

        return {
            "total_operations": len(operations),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: x[1], reverse=True)),
            "by_type": dict(sorted(by_type.items(), key=lambda x: x[1], reverse=True)),
            "hot_files": hot_files,
            "operations": operations,
        }

    def render_markdown(self, results: dict[str, Any]) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"**Total operaciones:** {results['total_operations']}\n")

        lines.append("## Por Herramienta\n")
        lines.append("| Herramienta | Usos |")
        lines.append("|-------------|------|")
        for tool, count in results["by_tool"].items():
            lines.append(f"| {tool} | {count} |")

        lines.append("\n## Por Tipo de Operacion\n")
        lines.append("| Tipo | Count |")
        lines.append("|------|-------|")
        for op, count in results["by_type"].items():
            lines.append(f"| {op} | {count} |")

        if results["hot_files"]:
            hot_limit = None if self.complete else 20
            hot_files = results["hot_files"] if self.complete else results["hot_files"][:hot_limit]
            lines.append("\n## Archivos Mas Tocados\n")
            lines.append("| # | Archivo | Operaciones | Tipos |")
            lines.append("|---|---------|-------------|-------|")
            for i, f in enumerate(hot_files, 1):
                display = f["file"] if self.complete else (f["file"].split("/")[-1] if "/" in f["file"] else f["file"])
                lines.append(f"| {i} | `{display}` | {f['ops']} | {', '.join(f['types'])} |")

        ops_list = results["operations"] if self.complete else results["operations"][:100]
        lines.append("\n## Log de Operaciones\n")
        lines.append("| Timestamp | Provider | Tool | Detalle |")
        lines.append("|-----------|----------|------|---------|")
        for op in ops_list:
            ts = op["timestamp"][:19] if op["timestamp"] else ""
            detail = op.get("detail", "") if self.complete else op.get("detail", "")[:80]
            lines.append(f"| {ts} | {op['provider']} | {op['tool']} | {detail} |")

        remaining = len(results["operations"]) - len(ops_list)
        if remaining > 0:
            lines.append(f"\n*... y {remaining} operaciones mas*\n")

        return "\n".join(lines) + "\n"
