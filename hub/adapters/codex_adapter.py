"""Adapter converting Codex entries to unified models."""

from __future__ import annotations

import json
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
from hub.models.codex import CodexEntry


class CodexAdapter(BaseAdapter):

    def to_unified(self, entry: CodexEntry, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None

        tool_calls = self._extract_tool_calls(entry)
        timestamp = self._parse_timestamp(entry.timestamp)
        text = self._extract_text(entry)

        tokens = self._extract_tokens(entry)

        return UnifiedMessage(
            id=entry.session_id or entry.timestamp,
            provider=Provider.CODEX,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=text,
            tool_calls=tool_calls,
            timestamp=timestamp,
            model=entry.model_provider or None,
            tokens=tokens,
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: CodexEntry, project: str) -> UnifiedEvent | None:
        role = self._map_role(entry)
        if role is None:
            return None

        summary = self._summarize(entry)
        tool_name = None
        file_path = None

        if entry.function_call:
            tool_name = entry.function_call.name
            # Try to extract command from arguments
            try:
                args = json.loads(entry.function_call.arguments)
                file_path = (
                    args.get("command", "")[:80]
                    or args.get("file_path", "")[:80]
                )
            except (json.JSONDecodeError, TypeError):
                file_path = entry.function_call.arguments[:80]

        tokens_dict = None
        if entry.event_type == "token_count" and entry.token_total > 0:
            tokens_dict = {
                "input": entry.token_input,
                "output": entry.token_output,
                "cached_input": entry.token_cached_input,
                "reasoning": entry.token_reasoning,
            }

        full_text = self._extract_full_text(entry)

        return UnifiedEvent(
            provider=Provider.CODEX,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id or None,
            tokens=tokens_dict,
            tool_name=tool_name,
            file_path=file_path if file_path else None,
            cwd=entry.cwd or None,
            full_text=full_text,
        )

    def to_session_meta(self, entry: CodexEntry, project: str) -> SessionMeta | None:
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.CODEX,
            project=project,
            cwd=entry.cwd or "",
            model=entry.model_provider or "",
            cli_version=entry.cli_version or "",
            source=entry.source or "",
        )

    def _map_role(self, entry: CodexEntry) -> MessageRole | None:
        match entry.event_type:
            case "session_meta":
                return MessageRole.SYSTEM
            case "event_msg":
                # Some event_msg entries contain system prompts, not user input
                text = entry.event_msg_text
                if text and self._is_system_prompt(text):
                    return MessageRole.SYSTEM
                return MessageRole.USER
            case "token_count":
                return MessageRole.SUMMARY
            case "response_item":
                match entry.payload_type:
                    case "message":
                        # "developer" role = system instructions for the model
                        if entry.role == "developer":
                            return MessageRole.SYSTEM
                        if entry.role == "user":
                            # Check if user message is actually a system prompt
                            if entry.text and self._is_system_prompt(entry.text):
                                return MessageRole.SYSTEM
                            return MessageRole.USER
                        return MessageRole.ASSISTANT
                    case "function_call":
                        return MessageRole.TOOL_USE
                    case "function_call_output":
                        return MessageRole.TOOL_RESULT
                    case "reasoning":
                        return MessageRole.THINKING
                    case _:
                        return None
            case _:
                return None

    @staticmethod
    def _is_system_prompt(text: str) -> bool:
        """Detect system/developer prompts that are not real user input.

        These patterns appear in Codex sessions as event_msg or response_item
        with role 'user' but are actually system-injected instructions.
        """
        stripped = text.strip()
        # XML-style system blocks
        if stripped.startswith("<") and any(
            stripped.startswith(f"<{tag}")
            for tag in ("permissions", "skills_instructions", "environment_context",
                        "system", "instructions", "tool_instructions")
        ):
            return True
        # Very long messages (>2000 chars) starting with common system patterns
        if len(stripped) > 2000 and any(
            stripped.startswith(prefix)
            for prefix in ("You are ", "You have ", "The following ", "## ")
        ):
            return True
        return False

    @staticmethod
    def _extract_tokens(entry: CodexEntry) -> TokenUsage | None:
        if entry.event_type != "token_count" or entry.token_total == 0:
            return None
        return TokenUsage(
            input_tokens=entry.token_input,
            output_tokens=entry.token_output + entry.token_reasoning,
            cache_creation=0,
            cache_read=entry.token_cached_input,
        )

    def _extract_text(self, entry: CodexEntry) -> str:
        if entry.event_msg_text:
            return entry.event_msg_text
        if entry.text:
            return entry.text
        if entry.reasoning_text:
            return entry.reasoning_text
        if entry.function_call:
            return f"{entry.function_call.name}({entry.function_call.arguments[:200]})"
        if entry.function_output:
            return entry.function_output.output
        return ""

    def _extract_tool_calls(self, entry: CodexEntry) -> list[ToolCall]:
        if not entry.function_call:
            return []
        try:
            args = json.loads(entry.function_call.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {"raw": entry.function_call.arguments}
        return [
            ToolCall(
                name=entry.function_call.name,
                input_data=args,
                tool_id=entry.function_call.call_id,
                operation_type="exec",
            )
        ]

    def _extract_full_text(self, entry: CodexEntry) -> str | None:
        match entry.event_type:
            case "event_msg":
                text = entry.event_msg_text.strip() if entry.event_msg_text else ""
                return text if text else None
            case "response_item":
                match entry.payload_type:
                    case "message":
                        text = entry.text.strip() if entry.text else ""
                        return text if text else None
                    case "function_call_output":
                        if entry.function_output and entry.function_output.output:
                            return entry.function_output.output
                        return None
                    case "reasoning":
                        if entry.reasoning_text:
                            return entry.reasoning_text.strip()
                        return None
                    case _:
                        return None
            case _:
                return None

    def _summarize(self, entry: CodexEntry) -> str:
        match entry.event_type:
            case "session_meta":
                return f"[session start] cwd={entry.cwd} v{entry.cli_version}"
            case "token_count":
                return f"[tokens] in={entry.token_input:,} out={entry.token_output:,} cached={entry.token_cached_input:,} reasoning={entry.token_reasoning:,}"
            case "event_msg":
                text = entry.event_msg_text.strip().replace("\n", " ")
                return text[:120] if text else "[user input]"
            case "response_item":
                match entry.payload_type:
                    case "message":
                        text = entry.text.strip().replace("\n", " ")
                        return text[:120] if text else "[message]"
                    case "function_call":
                        if entry.function_call:
                            name = entry.function_call.name
                            args_brief = entry.function_call.arguments[:80]
                            return f"{name}: {args_brief}"
                        return "[function call]"
                    case "function_call_output":
                        if entry.function_output:
                            return f"[output] {entry.function_output.output[:100]}"
                        return "[function output]"
                    case "reasoning":
                        return f"[reasoning] {entry.reasoning_text[:100]}"
                    case _:
                        return f"[{entry.payload_type}]"
            case _:
                return f"[{entry.event_type}]"

    @staticmethod
    def _parse_timestamp(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
