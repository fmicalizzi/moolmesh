"""Core models shared across all providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Provider(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    QWEN = "qwen"
    OPENCODE = "opencode"


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
