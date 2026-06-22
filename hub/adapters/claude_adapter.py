"""Adapter converting Claude entries to unified models."""

from __future__ import annotations

from datetime import datetime

from hub.adapters.base import BaseAdapter
from hub.models.base import (
    MessageRole,
    Provider,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)
from hub.models.claude import ClaudeContentBlock, ClaudeEntry, ClaudeUsage

# Types we skip during adaptation
_SKIP_TYPES = frozenset({"file-history-snapshot"})


class ClaudeAdapter(BaseAdapter):

    def to_unified(self, entry: ClaudeEntry, project: str) -> UnifiedMessage | None:
        if entry.type in _SKIP_TYPES:
            return None

        role = self._map_role(entry)
        if role is None:
            return None

        tool_calls = self._extract_tool_calls(entry.content_blocks)
        tokens = self._map_usage(entry.usage)
        timestamp = self._parse_timestamp(entry.timestamp)

        return UnifiedMessage(
            id=entry.uuid,
            provider=Provider.CLAUDE,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=entry.content_text,
            tool_calls=tool_calls,
            timestamp=timestamp,
            model=entry.model,
            tokens=tokens,
            parent_id=entry.parent_uuid,
            is_sidechain=entry.is_sidechain,
            cwd=entry.cwd,
            raw=entry.raw,
        )

    def to_event(self, entry: ClaudeEntry, project: str) -> UnifiedEvent | None:
        if entry.type in _SKIP_TYPES:
            return None

        role = self._map_role(entry)
        if role is None:
            return None

        summary = self._summarize(entry)
        tokens = None
        if entry.usage:
            tokens = {
                "input": entry.usage.input_tokens,
                "output": entry.usage.output_tokens,
            }

        tool_name = None
        file_path = None
        for block in entry.content_blocks:
            if block.type == "tool_use" and block.tool_name:
                tool_name = block.tool_name
                if block.tool_input:
                    file_path = (
                        block.tool_input.get("file_path")
                        or block.tool_input.get("path")
                        or block.tool_input.get("command", "")[:80]
                    )
                break

        return UnifiedEvent(
            provider=Provider.CLAUDE,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id,
            tokens=tokens,
            tool_name=tool_name,
            file_path=str(file_path) if file_path else None,
            model=entry.model,
            cwd=entry.cwd or None,
        )

    def _map_role(self, entry: ClaudeEntry) -> MessageRole | None:
        match entry.type:
            case "user":
                return MessageRole.USER
            case "assistant":
                # Check if this is primarily a tool_use or tool_result message
                has_tool_use = any(
                    b.type == "tool_use" for b in entry.content_blocks
                )
                has_tool_result = any(
                    b.type == "tool_result" for b in entry.content_blocks
                )
                has_thinking = any(
                    b.type == "thinking" for b in entry.content_blocks
                )
                has_text = bool(entry.content_text.strip())

                if has_tool_result and not has_text:
                    return MessageRole.TOOL_RESULT
                if has_tool_use and not has_text:
                    return MessageRole.TOOL_USE
                if has_thinking and not has_text and not has_tool_use:
                    return MessageRole.THINKING
                return MessageRole.ASSISTANT
            case "system":
                return MessageRole.SYSTEM
            case "summary":
                return MessageRole.SUMMARY
            case _:
                return None

    def _extract_tool_calls(
        self, blocks: list[ClaudeContentBlock]
    ) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for block in blocks:
            if block.type != "tool_use" or not block.tool_name:
                continue
            input_data = block.tool_input or {}
            fp = input_data.get("file_path") or input_data.get("path")
            op_type = self._infer_operation_type(block.tool_name, input_data)
            calls.append(
                ToolCall(
                    name=block.tool_name,
                    input_data=input_data,
                    tool_id=block.tool_id,
                    file_path=fp,
                    operation_type=op_type,
                )
            )
        return calls

    def _map_usage(self, usage: ClaudeUsage | None) -> TokenUsage | None:
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation=usage.cache_creation_input_tokens,
            cache_read=usage.cache_read_input_tokens,
        )

    def _summarize(self, entry: ClaudeEntry) -> str:
        """Produce a human-readable one-liner for the event feed."""
        match entry.type:
            case "user":
                text = entry.content_text.strip().replace("\n", " ")
                return text[:120] if text else "[empty user message]"
            case "assistant":
                # Prioritize tool_use summaries
                for block in entry.content_blocks:
                    if block.type == "tool_use" and block.tool_name:
                        brief = self._brief_tool_args(
                            block.tool_name, block.tool_input
                        )
                        return f"{block.tool_name}: {brief}"
                    if block.type == "thinking" and block.thinking:
                        return f"[thinking] {block.thinking[:100]}"
                text = entry.content_text.strip().replace("\n", " ")
                return text[:120] if text else "[assistant response]"
            case "system":
                sub = f" ({entry.subtype})" if entry.subtype else ""
                return f"[system{sub}]"
            case "summary":
                return "[context summary]"
            case _:
                return f"[{entry.type}]"

    @staticmethod
    def _brief_tool_args(tool_name: str, input_data: dict | None) -> str:
        if not input_data:
            return ""
        match tool_name:
            case "Bash":
                cmd = input_data.get("command", "")
                return cmd[:80]
            case "Read":
                return input_data.get("file_path", "")[:80]
            case "Write" | "Edit" | "MultiEdit":
                return input_data.get("file_path", "")[:80]
            case "Glob":
                return input_data.get("pattern", "")[:80]
            case "Grep":
                return input_data.get("pattern", "")[:80]
            case "Agent":
                return input_data.get("description", "")[:80]
            case "WebSearch" | "WebFetch":
                return input_data.get("query", input_data.get("url", ""))[:80]
            case _:
                # Generic: show first key=value
                for k, v in input_data.items():
                    return f"{k}={str(v)[:60]}"
                return ""

    @staticmethod
    def _infer_operation_type(tool_name: str, input_data: dict) -> str | None:
        match tool_name:
            case "Read" | "Glob" | "Grep":
                return "read"
            case "Write":
                return "create"
            case "Edit" | "MultiEdit":
                return "modify"
            case "Bash":
                cmd = input_data.get("command", "")
                if any(k in cmd for k in ("rm ", "rm\t", "rmdir")):
                    return "delete"
                if any(k in cmd for k in ("mkdir", "touch", "cp ", "mv ")):
                    return "create"
                return "exec"
            case "Agent":
                return "exec"
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
