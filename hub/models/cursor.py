"""Cursor intermediate models — one entry per chat bubble."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CursorComposer:
    """A Cursor agent/composer conversation's metadata (attribution context)."""

    composer_id: str
    name: str = ""
    project: str = ""
    cwd: str = ""
    model: str = ""
    created_at: int = 0  # unix ms
    last_updated_at: int = 0  # unix ms
    total_lines_added: int = 0
    total_lines_removed: int = 0
    files_changed_count: int = 0
    unified_mode: str = ""


@dataclass(slots=True)
class CursorBubble:
    """A single chat message ("bubble") from Cursor's global cursorDiskKV."""

    composer_id: str = ""
    bubble_id: str = ""
    bubble_type: int = 0  # 1 = user, 2 = assistant
    text: str = ""
    token_count: int = 0
    tool_name: str = ""
    file_path: str = ""
    is_agentic: bool = False
    rowid: int = 0
    composer: CursorComposer | None = None
    raw: dict[str, Any] = field(default_factory=dict)
