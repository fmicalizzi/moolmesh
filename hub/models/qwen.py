"""Qwen CLI specific entry models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QwenFunctionCall:
    call_id: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QwenFunctionResponse:
    call_id: str = ""
    name: str = ""
    output: str = ""


@dataclass(slots=True)
class QwenUsage:
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class QwenEntry:
    """One parsed line from a Qwen JSONL session file."""

    type: str  # "system", "user", "assistant", "tool_result"
    uuid: str = ""
    parent_uuid: str | None = None
    session_id: str = ""
    timestamp: str = ""
    cwd: str = ""
    version: str = ""
    model: str | None = None

    # Message content
    role: str = ""  # "user", "model", "system"
    text: str = ""  # extracted plain text
    has_thought: bool = False  # thinking block present

    # Function calls / responses
    function_calls: list[QwenFunctionCall] = field(default_factory=list)
    function_responses: list[QwenFunctionResponse] = field(default_factory=list)

    # Token usage
    usage: QwenUsage | None = None

    # System event specifics
    subtype: str | None = None  # "at_command", etc.

    raw: dict[str, Any] | None = None
