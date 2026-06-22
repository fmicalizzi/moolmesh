"""Codex (GPT-5.x) specific entry models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CodexFunctionCall:
    call_id: str = ""
    name: str = ""
    arguments: str = ""  # JSON string


@dataclass(slots=True)
class CodexFunctionOutput:
    call_id: str = ""
    output: str = ""


@dataclass(slots=True)
class CodexEntry:
    """One parsed event from a Codex rollout JSONL file."""

    event_type: str  # "session_meta", "event_msg", "response_item", "turn_context"
    timestamp: str = ""

    # session_meta fields
    session_id: str = ""
    cwd: str = ""
    cli_version: str = ""
    model_provider: str = ""
    source: str = ""

    # response_item fields
    payload_type: str = ""  # "message", "function_call", "function_call_output", "reasoning"
    role: str = ""  # "user", "assistant", "developer"
    text: str = ""  # extracted text content
    function_call: CodexFunctionCall | None = None
    function_output: CodexFunctionOutput | None = None
    reasoning_text: str = ""

    # event_msg fields (user input)
    event_msg_text: str = ""

    # token_count fields
    token_input: int = 0
    token_output: int = 0
    token_cached_input: int = 0
    token_reasoning: int = 0
    token_total: int = 0

    raw: dict[str, Any] | None = None
