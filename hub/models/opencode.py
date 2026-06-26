"""OpenCode intermediate models — one entry per part."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class OpenCodeToolCall:
    name: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    tool_id: str = ""


@dataclass(slots=True)
class OpenCodeEntry:
    """Represents a single part from OpenCode's SQLite database."""

    session_id: str = ""
    message_id: str = ""
    part_type: str = ""
    role: str = ""
    text: str = ""
    timestamp: str = ""
    model_id: str = ""
    model_provider: str = ""
    cwd: str = ""
    cost: float = 0.0
    token_input: int = 0
    token_output: int = 0
    token_reasoning: int = 0
    token_cache_read: int = 0
    token_cache_write: int = 0
    tool_call: OpenCodeToolCall | None = None
    files_affected: list[str] = field(default_factory=list)
    project_name: str = ""
    project_dir: str = ""
    session_title: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
