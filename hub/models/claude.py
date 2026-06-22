"""Claude Code specific entry models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ClaudeContentBlock:
    """One block within message.content array."""

    type: str  # "text", "tool_use", "tool_result", "thinking"
    text: str | None = None
    thinking: str | None = None
    # tool_use fields
    tool_name: str | None = None  # name
    tool_id: str | None = None  # id
    tool_input: dict[str, Any] | None = None  # input
    # tool_result fields
    tool_use_id: str | None = None
    tool_content: Any = None  # string or list


@dataclass(slots=True)
class ClaudeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(slots=True)
class ClaudeEntry:
    """One parsed line from a Claude JSONL session file."""

    type: str  # "user", "assistant", "system", "summary", "file-history-snapshot"
    uuid: str = ""
    parent_uuid: str | None = None
    session_id: str = ""
    timestamp: str = ""
    cwd: str = ""
    version: str = ""
    is_sidechain: bool = False
    git_branch: str | None = None
    # Message fields (flattened)
    role: str = ""  # "user", "assistant"
    content_blocks: list[ClaudeContentBlock] = field(default_factory=list)
    content_text: str = ""  # extracted plain text for convenience
    model: str | None = None
    message_id: str | None = None  # message.id for dedup
    usage: ClaudeUsage | None = None
    stop_reason: str | None = None
    # Subtype for system messages
    subtype: str | None = None
    # Raw dict for fallback
    raw: dict[str, Any] | None = None
