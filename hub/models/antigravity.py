"""Antigravity CLI (``agy``) specific entry models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AntigravityToolCall:
    """One call from a step's ``tool_calls``.

    The transcript stores every ``args`` value as a JSON-encoded *string*
    (a path arrives as ``"\\"/a/b.py\\""``); the parser decodes each value, so
    ``args`` holds plain strings / numbers / booleans / containers.
    """

    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AntigravitySummary:
    """Session metadata from one ``conversation_summaries.db`` row (CLI surface)."""

    conversation_id: str = ""
    title: str = ""
    preview: str = ""
    cwd: str = ""  # first ``workspace_uris`` entry, decoded to a filesystem path
    parent_conversation_id: str = ""  # set for subagent conversations
    nesting_depth: int = 0
    last_modified: str = ""


@dataclass(slots=True)
class AntigravityEntry:
    """One parsed step of an Antigravity CLI ``transcript.jsonl`` (plus context).

    A transcript is an append-only log of steps keyed by ``step_index``. The
    index is *not* guaranteed unique or ordered (copies of a conversation
    restart at 0, steps can land out of order, indices have gaps), so the
    parser dedupes by ``step_index`` per conversation and the entry carries it
    verbatim. Tool calls live on ``PLANNER_RESPONSE`` steps; the results of
    those calls are separate steps with their own ``type`` (``GENERIC``,
    ``RUN_COMMAND``, ``VIEW_FILE``, ...).
    """

    step_index: int
    step_type: str  # transcript ``type``: USER_INPUT, PLANNER_RESPONSE, GENERIC, ...
    source: str = ""  # USER_EXPLICIT | MODEL | SYSTEM
    status: str = ""  # DONE | RUNNING | ERROR — informational, never a transition
    timestamp: str = ""  # ``created_at`` (ISO-8601 UTC)

    content: str = ""
    thinking: str = ""
    error: str = ""
    tool_calls: list[AntigravityToolCall] = field(default_factory=list)
    truncated_fields: list[str] = field(default_factory=list)

    # Context stamped by the parser on every entry (a chunk read from
    # offset > 0 carries the same facts as a full parse).
    session_id: str = ""  # conversation uuid (``brain/<uuid>/...``)
    cwd: str = ""  # first ``run_command`` Cwd of the conversation
    session_title: str = ""  # first line of the first user prompt
    initial_prompt: str = ""
    # Earliest / latest ``created_at`` of the whole conversation so far (not of
    # this chunk): steps can arrive out of order, so a late-written older step
    # must never move the session's last event back.
    first_event_at: str = ""
    last_event_at: str = ""

    # Stamped by the watcher from ``conversation_summaries.db`` (optional).
    parent_session: str = ""
    nesting_depth: int = 0
    summary_title: str = ""
    summary_preview: str = ""
    summary_last_modified: str = ""

    raw: dict[str, Any] | None = None
