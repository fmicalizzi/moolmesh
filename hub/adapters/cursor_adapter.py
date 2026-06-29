"""Adapter converting Cursor bubbles to unified models."""

from __future__ import annotations

from datetime import datetime, timezone

from hub.adapters.base import BaseAdapter
from hub.models.base import (
    MessageRole, Provider, SessionMeta, TokenUsage, ToolCall, UnifiedEvent, UnifiedMessage,
)
from hub.models.cursor import CursorBubble


class CursorAdapter(BaseAdapter):

    def to_unified(self, entry: CursorBubble, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None
        tokens = self._extract_tokens(entry)
        tool_calls = []
        if entry.tool_name:
            tool_calls.append(ToolCall(
                name=entry.tool_name,
                input_data={"file_path": entry.file_path} if entry.file_path else {},
                file_path=entry.file_path or None,
            ))
        ts = self.event_timestamp(entry)
        return UnifiedMessage(
            id=entry.bubble_id or entry.composer_id,
            provider=Provider.CURSOR,
            session_id=entry.composer_id,
            project=project,
            role=role,
            text=entry.text,
            tool_calls=tool_calls,
            timestamp=datetime.fromisoformat(ts) if ts else None,
            model=(entry.composer.model if entry.composer else None) or None,
            tokens=tokens,
            cwd=(entry.composer.cwd if entry.composer else None) or None,
            raw=entry.raw,
        )

    def to_event(self, entry: CursorBubble, project: str) -> UnifiedEvent | None:
        role = self._map_role(entry)
        if role is None:
            return None
        text = entry.text.replace("\n", " ")
        summary = text[:120] if text else f"[{entry.tool_name or 'bubble'}]"
        tokens = self._extract_tokens(entry)
        return UnifiedEvent(
            provider=Provider.CURSOR,
            project=project,
            event_type=role.value,
            timestamp=self.event_timestamp(entry),
            summary=summary,
            session_id=entry.composer_id or None,
            tokens={"input": tokens.input_tokens, "output": tokens.output_tokens} if tokens else None,
            tool_name=entry.tool_name or None,
            file_path=entry.file_path or None,
            model=(entry.composer.model if entry.composer else None) or None,
            cwd=(entry.composer.cwd if entry.composer else None) or None,
            full_text=entry.text or None,
        )

    def to_session_meta(self, entry: CursorBubble, project: str) -> SessionMeta | None:
        c = entry.composer
        return SessionMeta(
            id=entry.composer_id,
            provider=Provider.CURSOR,
            project=project,
            title=(c.name if c else "") or "",
            cwd=(c.cwd if c else "") or "",
            model=(c.model if c else "") or "",
            initial_prompt=entry.text if entry.bubble_type == 1 else "",
            metadata={
                "total_lines_added": c.total_lines_added if c else 0,
                "total_lines_removed": c.total_lines_removed if c else 0,
                "files_changed_count": c.files_changed_count if c else 0,
                "unified_mode": c.unified_mode if c else "",
            },
        )

    def event_timestamp(self, entry: CursorBubble) -> str:
        c = entry.composer
        ms = 0
        if c:
            ms = c.last_updated_at or c.created_at
        if ms:
            try:
                return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                pass
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _map_role(entry: CursorBubble) -> MessageRole | None:
        if entry.bubble_type == 1:
            return MessageRole.USER
        if entry.bubble_type == 2:
            return MessageRole.ASSISTANT
        return None

    @staticmethod
    def _extract_tokens(entry: CursorBubble) -> TokenUsage | None:
        if not entry.token_count:
            return None
        if entry.bubble_type == 2:
            return TokenUsage(output_tokens=entry.token_count)
        return TokenUsage(input_tokens=entry.token_count)
