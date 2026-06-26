"""Adapter converting Qwen entries to unified models."""

from __future__ import annotations

from datetime import datetime

from hub.adapters.base import BaseAdapter
from hub.models.base import (
    MessageRole,
    Provider,
    SessionMeta,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)
from hub.models.qwen import QwenEntry


class QwenAdapter(BaseAdapter):

    def to_unified(self, entry: QwenEntry, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None

        tool_calls = self._extract_tool_calls(entry)
        tokens = self._map_usage(entry)
        timestamp = self._parse_timestamp(entry.timestamp)

        return UnifiedMessage(
            id=entry.uuid,
            provider=Provider.QWEN,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=entry.text,
            tool_calls=tool_calls,
            timestamp=timestamp,
            model=entry.model,
            tokens=tokens,
            parent_id=entry.parent_uuid,
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: QwenEntry, project: str) -> UnifiedEvent | None:
        role = self._map_role(entry)
        if role is None:
            return None

        summary = self._summarize(entry)
        tokens = None
        if entry.usage:
            tokens = {
                "input": entry.usage.prompt_tokens,
                "output": entry.usage.candidates_tokens,
            }

        tool_name = None
        file_path = None
        if entry.function_calls:
            fc = entry.function_calls[0]
            tool_name = fc.name
            # Extract command or path from args
            file_path = (
                fc.args.get("command", "")[:80]
                or fc.args.get("file_path", "")[:80]
                or str(fc.args)[:80]
            )

        return UnifiedEvent(
            provider=Provider.QWEN,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id or None,
            tokens=tokens,
            tool_name=tool_name,
            file_path=file_path if file_path else None,
            model=entry.model,
            cwd=entry.cwd or None,
        )

    def to_session_meta(self, entry: QwenEntry, project: str) -> SessionMeta | None:
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.QWEN,
            project=project,
            cwd=entry.cwd or "",
            model=entry.model or "",
        )

    def _map_role(self, entry: QwenEntry) -> MessageRole | None:
        match entry.type:
            case "user":
                return MessageRole.USER
            case "assistant":
                if entry.function_calls and not entry.text.strip():
                    return MessageRole.TOOL_USE
                if entry.has_thought and not entry.text.strip() and not entry.function_calls:
                    return MessageRole.THINKING
                return MessageRole.ASSISTANT
            case "system":
                return MessageRole.SYSTEM
            case "tool_result":
                return MessageRole.TOOL_RESULT
            case _:
                return None

    def _extract_tool_calls(self, entry: QwenEntry) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for fc in entry.function_calls:
            fp = fc.args.get("file_path") or fc.args.get("path")
            op_type = self._infer_operation_type(fc.name, fc.args)
            calls.append(
                ToolCall(
                    name=fc.name,
                    input_data=fc.args,
                    tool_id=fc.call_id,
                    file_path=fp,
                    operation_type=op_type,
                )
            )
        return calls

    def _map_usage(self, entry: QwenEntry) -> TokenUsage | None:
        if not entry.usage:
            return None
        return TokenUsage(
            input_tokens=entry.usage.prompt_tokens,
            output_tokens=entry.usage.candidates_tokens,
        )

    def _summarize(self, entry: QwenEntry) -> str:
        match entry.type:
            case "user":
                text = entry.text.strip().replace("\n", " ")
                return text[:120] if text else "[user message]"
            case "assistant":
                # Prioritize function calls
                if entry.function_calls:
                    fc = entry.function_calls[0]
                    brief = self._brief_args(fc.name, fc.args)
                    return f"{fc.name}: {brief}"
                if entry.has_thought:
                    # Extract non-thought text
                    text = entry.text.strip().replace("\n", " ")
                    if text:
                        return text[:120]
                    return "[thinking]"
                text = entry.text.strip().replace("\n", " ")
                return text[:120] if text else "[assistant response]"
            case "system":
                sub = f" ({entry.subtype})" if entry.subtype else ""
                text = entry.text.strip().replace("\n", " ")[:60]
                return f"[system{sub}] {text}" if text else f"[system{sub}]"
            case "tool_result":
                if entry.function_responses:
                    fr = entry.function_responses[0]
                    return f"[result] {fr.name}: {fr.output[:80]}"
                return "[tool result]"
            case _:
                return f"[{entry.type}]"

    @staticmethod
    def _brief_args(tool_name: str, args: dict) -> str:
        if not args:
            return ""
        match tool_name:
            case "run_shell_command":
                return args.get("command", "")[:80]
            case "read_file":
                return args.get("file_path", "")[:80]
            case "write_file" | "edit_file":
                return args.get("file_path", "")[:80]
            case "todo_write":
                return f"{len(args.get('todos', []))} items"
            case _:
                for k, v in args.items():
                    return f"{k}={str(v)[:60]}"
                return ""

    @staticmethod
    def _infer_operation_type(tool_name: str, args: dict) -> str | None:
        match tool_name:
            case "run_shell_command":
                cmd = args.get("command", "")
                if any(k in cmd for k in ("rm ", "rmdir")):
                    return "delete"
                if any(k in cmd for k in ("mkdir", "touch", "cp ")):
                    return "create"
                return "exec"
            case "read_file":
                return "read"
            case "write_file":
                return "create"
            case "edit_file":
                return "modify"
            case _:
                return None

    @staticmethod
    def _parse_timestamp(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
