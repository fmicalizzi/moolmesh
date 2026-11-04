"""Adapter converting OpenCode entries to unified models."""

from __future__ import annotations

from datetime import datetime, timezone

from hub.adapters.base import BaseAdapter
from hub.localtime import utc_iso_from_epoch
from hub.models.base import (
    MessageRole, Provider, SessionMeta, TokenUsage, ToolCall, UnifiedEvent, UnifiedMessage,
)
from hub.models.opencode import OpenCodeEntry


class OpenCodeAdapter(BaseAdapter):

    def to_unified(self, entry: OpenCodeEntry, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None

        tool_calls = self._extract_tool_calls(entry)
        timestamp = self._parse_timestamp(entry.timestamp)
        tokens = self._extract_tokens(entry)

        return UnifiedMessage(
            id=entry.message_id or entry.session_id,
            provider=Provider.OPENCODE,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=entry.text,
            tool_calls=tool_calls,
            timestamp=timestamp,
            model=entry.model_id or None,
            tokens=tokens,
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: OpenCodeEntry, project: str) -> UnifiedEvent | None:
        role = self._map_role(entry)
        if role is None:
            return None

        tool_name = None
        file_path = None
        if entry.tool_call:
            tool_name = entry.tool_call.name
            # OpenCode's read/edit/write tools name their file ``filePath``
            # (#75). A path is kept whole — cutting it breaks workspace
            # attribution; only a shell command keeps its short preview.
            data = entry.tool_call.input_data
            path = data.get("filePath") or data.get("path")
            if isinstance(path, str) and path:
                file_path = path
            else:
                command = data.get("command", "")
                file_path = command[:80] if isinstance(command, str) else ""

        raw_text = entry.text.strip()
        text = raw_text.replace("\n", " ")
        summary = text[:120] if text else f"[{entry.part_type}]"
        full_text = raw_text if raw_text else None

        ts_str = self.iso_timestamp(entry.timestamp) or str(entry.timestamp)

        return UnifiedEvent(
            provider=Provider.OPENCODE,
            project=project,
            event_type=role.value,
            timestamp=ts_str,
            summary=summary,
            session_id=entry.session_id or None,
            tool_name=tool_name,
            file_path=file_path if file_path else None,
            cwd=entry.cwd or None,
            full_text=full_text,
        )

    def to_session_meta(self, entry: OpenCodeEntry, project: str) -> SessionMeta | None:
        # ISO, never the raw ms-epoch (#79): sessions.first_event_at /
        # last_event_at are compared as text against ISO event timestamps.
        at = self.iso_timestamp(entry.timestamp) or ""
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.OPENCODE,
            project=project,
            title=entry.session_title or "",
            cwd=entry.cwd or "",
            model=entry.model_id or "",
            cost=entry.cost or 0.0,
            first_event_at=at,
            last_event_at=at,
        )

    def _map_role(self, entry: OpenCodeEntry) -> MessageRole | None:
        match entry.part_type:
            case "text":
                if entry.role == "user":
                    return MessageRole.USER
                return MessageRole.ASSISTANT
            case "reasoning":
                return MessageRole.THINKING
            case "tool" | "file" | "patch":
                return MessageRole.TOOL_USE
            case "step-finish":
                return MessageRole.SUMMARY
            case "compaction":
                return MessageRole.SUMMARY
            case _:
                return None

    @staticmethod
    def _extract_tokens(entry: OpenCodeEntry) -> TokenUsage | None:
        if entry.part_type != "step-finish":
            return None
        total = entry.token_input + entry.token_output + entry.token_reasoning
        if total == 0:
            return None
        return TokenUsage(
            input_tokens=entry.token_input,
            output_tokens=entry.token_output + entry.token_reasoning,
            cache_creation=entry.token_cache_write,
            cache_read=entry.token_cache_read,
        )

    def _extract_tool_calls(self, entry: OpenCodeEntry) -> list[ToolCall]:
        if not entry.tool_call:
            return []
        return [
            ToolCall(
                name=entry.tool_call.name,
                input_data=entry.tool_call.input_data,
                tool_id=entry.tool_call.tool_id,
                output_data=entry.tool_call.output_data or None,
                file_path=entry.files_affected[0] if entry.files_affected else None,
                operation_type=self._classify_operation(entry.tool_call.name),
            )
        ]

    @staticmethod
    def _classify_operation(tool_name: str) -> str:
        match tool_name:
            case "read" | "file_read":
                return "read"
            case "write" | "file_edit" | "edit":
                return "write"
            case "bash":
                return "exec"
            case "glob" | "list" | "grep":
                return "search"
            case _:
                return "other"

    @staticmethod
    def iso_timestamp(ts: str | int | float) -> str | None:
        """An OpenCode clock as the ISO string stored in ``events.timestamp``.

        OpenCode's ``time_created`` is a ms epoch: it becomes fixed-width UTC
        (``utc_iso_from_epoch``), never the machine's local offset — that ISO
        string feeds the event fingerprint, which must not change with DST or
        a zone change (#79). An ISO input keeps its own offset.
        """
        if not ts or isinstance(ts, bool):
            return None
        iso = utc_iso_from_epoch(ts)
        if iso:
            return iso
        parsed = OpenCodeAdapter._parse_timestamp(ts)
        return parsed.isoformat() if parsed else None

    @staticmethod
    def _parse_timestamp(ts: str | int | float) -> datetime | None:
        if not ts:
            return None
        if isinstance(ts, (int, float)):
            try:
                if ts > 1e12:
                    ts = ts / 1000
                # The instant in UTC — never the machine's *current* offset (#79).
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError, OSError, OverflowError):
                return None
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
