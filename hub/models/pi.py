"""Pi (``@earendil-works/pi-coding-agent``) specific entry models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PiToolCall:
    """One tool call block inside an assistant message."""

    call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PiEntry:
    """One parsed line from a Pi session JSONL file (plus propagated context).

    Pi sessions are a *tree*: every entry carries ``id`` + ``parentId``
    (editing or forking a message appends a new branch). The parser ingests
    every entry exactly once, in file order, and stamps each one with the
    session context (id, cwd, model, first prompt) so a chunked incremental
    read from offset > 0 carries the same facts as a full parse.
    """

    event_type: str  # "session", "message", "model_change", "thinking_level_change", "compaction", "branch_summary", "usage", "session_info"
    entry_id: str = ""
    parent_id: str = ""
    timestamp: str = ""

    # Session header / propagated context
    session_id: str = ""
    cwd: str = ""
    version: int = 0
    parent_session: str = ""  # header.parentSession (forked sessions)

    # message fields
    role: str = ""  # "user", "assistant", "toolResult", "system"
    text: str = ""  # concatenated "text" blocks
    thinking: str = ""  # concatenated "thinking" blocks
    tool_calls: list[PiToolCall] = field(default_factory=list)

    # toolResult fields
    tool_call_id: str = ""
    tool_name: str = ""
    tool_is_error: bool = False

    # model context (assistant message or last model_change)
    model: str = ""
    model_provider: str = ""

    # assistant usage (Pi's Usage: output already includes reasoning)
    token_input: int = 0
    token_output: int = 0
    token_cache_read: int = 0
    token_cache_write: int = 0
    token_reasoning: int = 0
    token_total: int = 0
    cost_total: float = 0.0

    # misc entry payloads
    thinking_level: str = ""
    summary: str = ""  # compaction / branch_summary text

    # Context stamped by the parser on every entry
    session_title: str = ""
    initial_prompt: str = ""
    leaf_id: str = ""  # last entry id seen at parse time (active branch tip)

    raw: dict[str, Any] | None = None
