"""Core models shared across all providers."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Provider(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    QWEN = "qwen"
    OPENCODE = "opencode"
    CURSOR = "cursor"
    PI = "pi"
    ANTIGRAVITY = "antigravity"


# Tool names whose ``file_path`` is a shell command, never a file (#58).
# Single source of truth: adapters must not mint a command as a path, and the
# workspace attribution pass ignores any legacy row that still carries one.
SHELL_TOOLS = frozenset({
    "Bash", "bash", "run_shell_command", "shell", "exec_command",
    "run_command",  # Antigravity CLI
})


# ``git worktree add`` evidence (#161). Removed worktrees fold back into their
# repo only from the FULL command a session ran (#149 read a capped summary,
# which cuts the path). Every adapter keeps that command in the event's
# ``full_text`` — after this marker line, appended to any text the event
# already had — but ONLY for a shell call that mentions a worktree add, so
# nothing else about stored content changes. The workspace layer reads it
# back with :func:`shell_evidence_command`.
SHELL_EVIDENCE_MARKER = "[shell command]\n"
_WORKTREE_ADD_RE = re.compile(r"\bworktree\b.*\badd\b", re.S)


def shell_command_text(command: Any) -> str | None:
    """A shell call's command as one string: a ``["bash", "-lc", cmd]`` argv
    yields ``cmd``; any other argv is joined with shell quoting."""
    if isinstance(command, str):
        return command or None
    if isinstance(command, (list, tuple)) and all(isinstance(a, str) for a in command):
        if len(command) == 3 and command[1] in ("-c", "-lc", "-ic"):
            return command[2] or None
        return shlex.join(command) or None
    return None


def with_shell_evidence(full_text: str | None, command: Any) -> str | None:
    """``full_text`` plus the full shell ``command`` when it runs a
    ``git worktree add`` (#161); ``full_text`` unchanged otherwise."""
    cmd = shell_command_text(command)
    if not cmd or not _WORKTREE_ADD_RE.search(cmd):
        return full_text
    block = SHELL_EVIDENCE_MARKER + cmd
    return f"{full_text}\n\n{block}" if full_text else block


def shell_evidence_command(full_text: str | None) -> str | None:
    """The command :func:`with_shell_evidence` stored in ``full_text``."""
    if not full_text:
        return None
    i = full_text.rfind(SHELL_EVIDENCE_MARKER)
    if i < 0 or (i and full_text[i - 2:i] != "\n\n"):
        return None
    return full_text[i + len(SHELL_EVIDENCE_MARKER):] or None


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    SUMMARY = "summary"


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_with_cache(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation + self.cache_read


@dataclass(slots=True)
class ToolCall:
    name: str
    input_data: dict[str, Any]
    tool_id: str | None = None
    output_data: str | None = None
    file_path: str | None = None
    operation_type: str | None = None  # "create", "modify", "read", "delete", "exec"


@dataclass(slots=True)
class UnifiedMessage:
    """Full message representation for batch analysis."""

    id: str
    provider: Provider
    session_id: str
    project: str
    role: MessageRole
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    timestamp: datetime | None = None
    model: str | None = None
    tokens: TokenUsage | None = None
    parent_id: str | None = None
    is_sidechain: bool = False
    cwd: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class UnifiedEvent:
    """Lightweight event for SSE streaming to the dashboard."""

    provider: Provider
    project: str
    event_type: str  # mirrors MessageRole values
    timestamp: str  # ISO string
    summary: str  # human-readable one-liner
    session_id: str | None = None
    tokens: dict[str, int] | None = None  # {"input": N, "output": N}
    tool_name: str | None = None
    file_path: str | None = None
    model: str | None = None
    cwd: str | None = None  # working directory of the session
    full_text: str | None = None
    # Provider-native per-entry id (#67), when the format has one (Pi entry
    # ``id``, Claude line ``uuid``). Part of the dedupe fingerprint.
    entry_id: str | None = None
    # Provider-native id of the entry's parent (#67: Pi ``parentId``, Claude
    # ``parentUuid``). Lets a tree-shaped session be shown as its active
    # branch (``hub.linearize``). Never part of the fingerprint.
    parent_id: str | None = None
    # True for a per-directory extra ``tool_use`` that Codex/Pi ``to_events``
    # emits next to the primary call (#122). Stored as ``events.dir_extra``;
    # surfaces that count *calls* skip it. Never part of the fingerprint.
    dir_extra: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider": self.provider.value,
            "project": self.project,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "summary": self.summary,
        }
        if self.session_id:
            d["session_id"] = self.session_id
        if self.tokens:
            d["tokens"] = self.tokens
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.file_path:
            d["file_path"] = self.file_path
        if self.model:
            d["model"] = self.model
        if self.cwd:
            d["cwd"] = self.cwd
        if self.full_text:
            d["full_text"] = self.full_text
        if self.entry_id:
            d["entry_id"] = self.entry_id
        if self.parent_id:
            d["parent_id"] = self.parent_id
        if self.dir_extra:
            d["dir_extra"] = True
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass(slots=True)
class SessionMeta:
    """Metadata about a session, extracted from provider-specific entries."""

    id: str
    provider: Provider
    project: str
    title: str = ""
    cwd: str = ""
    git_branch: str = ""
    model: str = ""
    cli_version: str = ""
    source: str = ""
    cost: float = 0.0
    is_sidechain: bool = False
    initial_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # ISO time of the entry that produced this meta, for the caller's
    # ``upsert_session(meta, timestamp)`` (#79). Optional; not in to_dict().
    first_event_at: str = ""
    last_event_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "provider": self.provider.value,
            "project": self.project,
        }
        if self.title:
            d["title"] = self.title
        if self.cwd:
            d["cwd"] = self.cwd
        if self.git_branch:
            d["git_branch"] = self.git_branch
        if self.model:
            d["model"] = self.model
        if self.cli_version:
            d["cli_version"] = self.cli_version
        if self.source:
            d["source"] = self.source
        if self.cost:
            d["cost"] = self.cost
        if self.is_sidechain:
            d["is_sidechain"] = self.is_sidechain
        if self.initial_prompt:
            d["initial_prompt"] = self.initial_prompt
        if self.metadata:
            d["metadata"] = self.metadata
        return d
