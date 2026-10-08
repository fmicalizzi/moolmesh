"""Parser for Pi (``@earendil-works/pi-coding-agent``) session JSONL files.

Pi stores one JSONL file per session under
``~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl`` (the agent
directory follows ``PI_CODING_AGENT_DIR`` when set, and ``--session-dir`` can
move a single run). Line 1 is the session header; every following entry has
``id`` + ``parentId``, so the file is a *tree*: editing or forking a message
appends a new branch.

The parser ingests every entry exactly once (deduped by ``id``), in file order,
so no branch is ever lost. It also exposes :func:`linearize` to walk the active
branch (leaf → root) when a consumer needs the linear transcript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hub.models.pi import PiEntry, PiToolCall
from hub.parsers.base import BaseParser

# Title / initial prompt bounds (sessions metadata is a summary, not a copy).
_TITLE_MAX = 120
_PROMPT_MAX = 2000


def default_pi_base() -> Path:
    """Pi's agent directory: ``$PI_CODING_AGENT_DIR`` or ``~/.pi/agent``.

    Pi resolves ``PI_CODING_AGENT_DIR`` (``<APP>_CODING_AGENT_DIR``) and joins
    ``sessions`` under it; ``--session-dir`` only moves one run. Windows uses
    the same ``%USERPROFILE%\\.pi\\agent`` layout.
    """
    env = os.environ.get("PI_CODING_AGENT_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".pi" / "agent"


def _int(value: Any) -> int:
    """Coerce a JSON token count to int (0 for anything non-numeric)."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _blocks(content: Any) -> tuple[str, str, list[PiToolCall]]:
    """Split message content into (text, thinking, tool_calls).

    ``content`` is a string (system messages and simple user turns) or a list
    of Pi content blocks: ``text``, ``thinking`` and ``toolCall``. Unknown
    block types are ignored rather than guessed at.
    """
    if isinstance(content, str):
        return content, "", []
    texts: list[str] = []
    thinkings: list[str] = []
    calls: list[PiToolCall] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                texts.append(_text(block.get("text")))
            elif block_type == "thinking":
                thinkings.append(_text(block.get("thinking")))
            elif block_type == "toolCall":
                args = block.get("arguments")
                calls.append(
                    PiToolCall(
                        call_id=_text(block.get("id")),
                        name=_text(block.get("name")),
                        arguments=args if isinstance(args, dict) else {},
                    )
                )
    return "\n".join(texts), "\n".join(thinkings), calls


class PiParser(BaseParser):

    def __init__(self):
        # Per-file context (session header facts, last model, seen ids) so an
        # incremental chunk read from offset > 0 carries the same context as a
        # full parse. Cleared by the history path (see ``watchers/base.py``).
        self._session_ctx: dict[str, dict[str, Any]] = {}

    # ── public API ───────────────────────────────────────────────

    def parse_file(self, path: Path) -> list[PiEntry]:
        ctx = self._new_ctx()
        entries: list[PiEntry] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                if not self._admit(raw, ctx):
                    continue
                entry = self._parse_line(raw)
                if entry is None:
                    continue
                self._apply_session_ctx(entry, ctx)
                entries.append(entry)
        return entries

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[PiEntry], int]:
        ctx = self._session_ctx.setdefault(str(path), self._new_ctx())
        if offset > 0 and not ctx.get("session_id"):
            # Daemon restart: the header was consumed in an earlier run.
            self._seed_session_ctx(path, ctx)
        entries: list[PiEntry] = []
        with open(path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            if offset > file_size:
                offset = 0
            f.seek(offset)
            data = f.read()
        if not data:
            return entries, offset

        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return entries, offset

        complete = data[:last_nl + 1]
        new_offset = offset + len(complete)

        for line in complete.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            if not self._admit(raw, ctx):
                continue
            entry = self._parse_line(raw)
            if entry is None:
                continue
            self._apply_session_ctx(entry, ctx)
            entries.append(entry)
        return entries, new_offset

    @staticmethod
    def can_parse(path: Path) -> bool:
        if path.suffix != ".jsonl":
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                if not first:
                    return False
                data = json.loads(first)
        except (json.JSONDecodeError, OSError):
            return False
        return (
            isinstance(data, dict)
            and data.get("type") == "session"
            and isinstance(data.get("id"), str)
            and bool(data.get("id"))
            and ("cwd" in data or "version" in data)
        )

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _new_ctx() -> dict[str, Any]:
        return {
            "session_id": "",
            "cwd": "",
            "version": 0,
            "parent_session": "",
            "model": "",
            "model_provider": "",
            "session_title": "",
            "initial_prompt": "",
            "seen": set(),
            "leaf_id": "",
        }

    @staticmethod
    def _admit(raw: dict[str, Any], ctx: dict[str, Any]) -> bool:
        """Tree/fork safety: ingest each entry id at most once per file."""
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            return True  # nothing to dedupe on; the file is append-only
        seen: set[str] = ctx["seen"]
        if entry_id in seen:
            return False
        seen.add(entry_id)
        return True

    def _seed_session_ctx(self, path: Path, ctx: dict[str, Any]) -> None:
        """Fill ``ctx`` from the file's first line (the session header)."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.loads(f.readline())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("type") != "session":
            return
        self._admit(raw, ctx)
        entry = self._parse_line(raw)
        if entry is not None:
            self._apply_session_ctx(entry, ctx)

    def _apply_session_ctx(self, entry: PiEntry, ctx: dict[str, Any]) -> None:
        """Fold this entry into the context, then stamp it onto the entry.

        First-wins for identity (session id/cwd/first prompt), last-wins for
        the model (``model_change`` or the assistant that used it) — matching
        the session metadata semantics of the other providers.
        """
        match entry.event_type:
            case "session":
                if not ctx["session_id"]:
                    ctx["session_id"] = entry.session_id
                    ctx["cwd"] = entry.cwd
                    ctx["version"] = entry.version
                    ctx["parent_session"] = entry.parent_session
            case "model_change":
                if entry.model:
                    ctx["model"] = entry.model
                    ctx["model_provider"] = entry.model_provider
            case "session_info":
                # An explicit session name (``/name``) wins over the first
                # user line, regardless of arrival order.
                if entry.summary:
                    ctx["session_title"] = entry.summary[:_TITLE_MAX]
            case "message":
                if entry.role == "assistant" and entry.model:
                    ctx["model"] = entry.model
                    ctx["model_provider"] = entry.model_provider
                if entry.role == "user" and entry.text and not ctx["initial_prompt"]:
                    ctx["initial_prompt"] = entry.text[:_PROMPT_MAX]
                    if not ctx["session_title"]:
                        first_line = entry.text.strip().splitlines()[0].strip()
                        ctx["session_title"] = first_line[:_TITLE_MAX]

        if entry.entry_id:
            ctx["leaf_id"] = entry.entry_id

        entry.session_id = entry.session_id or ctx["session_id"]
        entry.cwd = entry.cwd or ctx["cwd"]
        entry.version = entry.version or ctx["version"]
        entry.parent_session = entry.parent_session or ctx["parent_session"]
        entry.model = entry.model or ctx["model"]
        entry.model_provider = entry.model_provider or ctx["model_provider"]
        entry.session_title = ctx["session_title"]
        entry.initial_prompt = ctx["initial_prompt"]
        entry.leaf_id = ctx["leaf_id"]

    def _parse_line(self, raw: dict[str, Any]) -> PiEntry | None:
        event_type = raw.get("type", "")
        entry_id = _text(raw.get("id"))
        parent_id = _text(raw.get("parentId"))
        timestamp = _text(raw.get("timestamp"))

        match event_type:
            case "session":
                version = raw.get("version")
                return PiEntry(
                    event_type="session",
                    entry_id=entry_id,
                    timestamp=timestamp,
                    session_id=_text(raw.get("id")),
                    cwd=_text(raw.get("cwd")),
                    version=version if isinstance(version, int) else 0,
                    parent_session=_text(raw.get("parentSession")),
                    raw=raw,
                )

            case "message":
                message = raw.get("message")
                if not isinstance(message, dict):
                    return None
                role = _text(message.get("role"))
                text, thinking, tool_calls = _blocks(message.get("content"))
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                cost = usage.get("cost")
                if not isinstance(cost, dict):
                    cost = {}
                return PiEntry(
                    event_type="message",
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    role=role,
                    text=text,
                    thinking=thinking,
                    tool_calls=tool_calls,
                    tool_call_id=_text(message.get("toolCallId")),
                    tool_name=_text(message.get("toolName")),
                    tool_is_error=bool(message.get("isError")),
                    model=_text(message.get("model")),
                    model_provider=_text(message.get("provider")),
                    token_input=_int(usage.get("input")),
                    token_output=_int(usage.get("output")),
                    token_cache_read=_int(usage.get("cacheRead")),
                    # cacheWrite1h (Anthropic-only split) is part of cacheWrite;
                    # Pi's ``output`` already includes ``reasoning``.
                    token_cache_write=_int(usage.get("cacheWrite")),
                    token_reasoning=_int(usage.get("reasoning")),
                    token_total=_int(usage.get("totalTokens")),
                    cost_total=_float(cost.get("total")),
                    raw=raw,
                )

            case "model_change":
                return PiEntry(
                    event_type="model_change",
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    model=_text(raw.get("modelId")),
                    model_provider=_text(raw.get("provider")),
                    raw=raw,
                )

            case "thinking_level_change":
                return PiEntry(
                    event_type="thinking_level_change",
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    thinking_level=_text(raw.get("thinkingLevel")),
                    raw=raw,
                )

            case "compaction" | "branch_summary":
                return PiEntry(
                    event_type=event_type,
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    summary=_text(raw.get("summary")),
                    raw=raw,
                )

            case "usage":
                usage = raw.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                cost = usage.get("cost")
                if not isinstance(cost, dict):
                    cost = {}
                return PiEntry(
                    event_type="usage",
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    summary=_text(raw.get("kind")),
                    model=_text(raw.get("model")),
                    model_provider=_text(raw.get("provider")),
                    token_input=_int(usage.get("input")),
                    token_output=_int(usage.get("output")),
                    token_cache_read=_int(usage.get("cacheRead")),
                    token_cache_write=_int(usage.get("cacheWrite")),
                    token_reasoning=_int(usage.get("reasoning")),
                    token_total=_int(usage.get("totalTokens")),
                    cost_total=_float(cost.get("total")),
                    raw=raw,
                )

            case "session_info":
                return PiEntry(
                    event_type="session_info",
                    entry_id=entry_id,
                    parent_id=parent_id,
                    timestamp=timestamp,
                    summary=_text(raw.get("name")),
                    raw=raw,
                )

            case _:
                # label / custom / custom_message / context_edit: extension
                # bookkeeping, never conversation. Nothing to observe.
                return None


def linearize(entries: list[PiEntry], leaf_id: str | None = None) -> list[PiEntry]:
    """The active branch of a Pi tree, root → leaf (Pi's own transcript order).

    ``entries`` is any set of parsed Pi entries that still carry ``entry_id`` /
    ``parent_id`` (a full ``parse_file`` pass). The walk starts at ``leaf_id``
    — default (or when the id is unknown): the last entry with an id, which is
    the file's append-only tip — and follows ``parent_id`` to the root. Entries
    whose parent is missing are still returned (best effort); the result is
    deduplicated and sorted by the original file order.
    """
    by_id = {e.entry_id: e for e in entries if e.entry_id}
    leaf = leaf_id
    if not leaf or leaf not in by_id:
        # No leaf (or one this entry set does not know): use the file's
        # append-only tip — the last entry that carries an id.
        leaf = next((e.entry_id for e in reversed(entries) if e.entry_id), None)
    chain: list[PiEntry] = []
    seen: set[str] = set()
    while leaf and leaf in by_id and leaf not in seen:
        seen.add(leaf)
        entry = by_id[leaf]
        chain.append(entry)
        leaf = entry.parent_id or None
    order = {id(e): i for i, e in enumerate(entries)}
    return sorted(chain, key=lambda e: order.get(id(e), 0))
