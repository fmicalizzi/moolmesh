"""Adapter converting Pi entries to unified models."""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
from dataclasses import replace
from datetime import datetime

from hub.adapters.base import BaseAdapter
from hub.models.base import (
    SHELL_TOOLS,
    MessageRole,
    Provider,
    SessionMeta,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)
from hub.models.pi import PiEntry, PiToolCall

# Pi's built-in tools that always name a file (ls/grep/find take a directory).
_FILE_TOOLS = frozenset({"read", "edit", "write"})

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _pathmod(p: str):
    """``ntpath`` for drive-letter/UNC paths, ``posixpath`` otherwise."""
    return ntpath if _WIN_ABS_RE.match(p) else posixpath


def _is_abs(p: str) -> bool:
    return p.startswith("/") or bool(_WIN_ABS_RE.match(p))


def _normalize_path(raw: str, base: str | None) -> str | None:
    """Absolute, normalized path — or None for anything that is not a literal
    file path (empty, a directory, a runtime template)."""
    p = raw.strip()
    if not p or "${" in p or any(c in p for c in "\"'`\x00"):
        return None
    if p.endswith(("/", "\\")):
        return None  # a directory, not a file the call touched
    if _is_abs(p):
        return _pathmod(p).normpath(p)
    if base:
        mod = _pathmod(base)
        return mod.normpath(mod.join(base, p))
    return None


def extract_pi_paths(tool_calls: list[PiToolCall], cwd: str = "") -> list[str]:
    """Files the entry's tool calls touched, absolute and normalized.

    Only Pi's file tools (``read``/``edit``/``write``, whose ``path`` always
    names a file) contribute. ``ls``/``grep``/``find`` take a directory and are
    skipped; ``bash`` is a shell tool and its command NEVER becomes a path
    (#58). Relative paths resolve against the session ``cwd``. First-seen
    order, no duplicates, never truncated. Never raises.
    """
    base = cwd if cwd and _is_abs(cwd) else None
    paths: list[str] = []
    for call in tool_calls:
        if call.name not in _FILE_TOOLS:
            continue
        raw = call.arguments.get("path")
        if not isinstance(raw, str):
            continue
        p = _normalize_path(raw, base)
        if p and p not in paths:
            paths.append(p)
    return paths


class PiAdapter(BaseAdapter):

    def to_unified(self, entry: PiEntry, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None

        return UnifiedMessage(
            id=entry.entry_id or entry.timestamp,
            provider=Provider.PI,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=self._extract_text(entry),
            tool_calls=self._extract_tool_calls(entry),
            timestamp=self._parse_timestamp(entry.timestamp),
            model=entry.model or None,
            tokens=self._extract_tokens(entry),
            parent_id=entry.parent_id or None,
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: PiEntry, project: str) -> UnifiedEvent | None:
        """The primary event for ``entry`` (first touched path, if any)."""
        built = self._build_event(entry, project)
        return built[0] if built else None

    def to_events(self, entry: PiEntry, project: str) -> list[UnifiedEvent]:
        """Primary event plus one extra event per additional touched directory.

        One assistant message can call several file tools at once (the real
        format shows up to seven reads in one entry). The primary event carries
        the first path; each further *distinct containing directory* gets one
        extra ``tool_use`` event carrying its first path — one per directory,
        not per file, mirroring the Codex adapter. Extras have a unique,
        deterministic summary (so the event fingerprint neither collapses them
        into the primary nor duplicates them on re-parse), and carry no tokens
        and no full_text.
        """
        built = self._build_event(entry, project)
        if not built:
            return []
        primary, paths = built
        events = [primary]
        if len(paths) > 1:
            seen_dirs = {_pathmod(paths[0]).dirname(paths[0])}
            for p in paths[1:]:
                d = _pathmod(p).dirname(p)
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                events.append(replace(
                    primary,
                    summary=f"{primary.tool_name}: {p}",
                    file_path=p,
                    tokens=None,
                    full_text=None,
                ))
        return events

    def _build_event(
        self, entry: PiEntry, project: str
    ) -> tuple[UnifiedEvent, list[str]] | None:
        role = self._map_role(entry)
        if role is None:
            return None

        summary = self._summarize(entry)
        tool_name = None
        file_path = None
        paths: list[str] = []

        if entry.tool_calls:
            tool_name = entry.tool_calls[0].name
            paths = extract_pi_paths(entry.tool_calls, entry.cwd)
            if paths:
                # Untruncated: the workspace layer resolves this path.
                file_path = paths[0]
        elif entry.tool_name:
            tool_name = entry.tool_name

        event = UnifiedEvent(
            provider=Provider.PI,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id or None,
            tokens=self._tokens_dict(entry),
            tool_name=tool_name,
            file_path=file_path if file_path else None,
            model=entry.model or None,
            cwd=entry.cwd or None,
            full_text=self._extract_full_text(entry),
        )
        return event, paths

    def to_session_meta(self, entry: PiEntry, project: str) -> SessionMeta | None:
        if not entry.session_id:
            return None
        metadata: dict[str, str] = {}
        if entry.leaf_id:
            # Active branch tip at parse time: the leaf a leaf→root
            # linearization starts from (``parsers.pi_parser.linearize``).
            metadata["leaf_id"] = entry.leaf_id
        if entry.parent_session:
            metadata["parent_session_id"] = entry.parent_session
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.PI,
            project=project,
            title=entry.session_title or "",
            cwd=entry.cwd or "",
            model=entry.model or "",
            initial_prompt=entry.initial_prompt or "",
            metadata=metadata,
        )

    # ── role / text ──────────────────────────────────────────────

    def _map_role(self, entry: PiEntry) -> MessageRole | None:
        match entry.event_type:
            case "session":
                return MessageRole.SYSTEM
            case "compaction" | "branch_summary" | "usage":
                return MessageRole.SUMMARY
            case "message":
                match entry.role:
                    case "user":
                        # A user entry may carry image-only content; notify the
                        # user too: an empty user message is noise.
                        return MessageRole.USER if entry.text.strip() else None
                    case "system":
                        return MessageRole.SYSTEM if entry.text.strip() else None
                    case "toolResult":
                        return MessageRole.TOOL_RESULT
                    case "assistant":
                        if entry.text.strip():
                            return MessageRole.ASSISTANT
                        if entry.tool_calls:
                            return MessageRole.TOOL_USE
                        if entry.thinking.strip():
                            return MessageRole.THINKING
                        return None
                    case _:
                        return None
            case _:
                # model_change / thinking_level_change / session_info:
                # session context, not conversation events.
                return None

    def _extract_text(self, entry: PiEntry) -> str:
        if entry.text:
            return entry.text
        if entry.thinking:
            return entry.thinking
        if entry.summary:
            return entry.summary
        if entry.tool_calls:
            fc = entry.tool_calls[0]
            args = ", ".join(f"{k}={v!r}" for k, v in list(fc.arguments.items())[:3])
            return f"{fc.name}({args})"
        return ""

    @staticmethod
    def _extract_tool_calls(entry: PiEntry) -> list[ToolCall]:
        return [
            ToolCall(
                name=call.name,
                input_data=call.arguments,
                tool_id=call.call_id or None,
                operation_type=PiAdapter._classify_operation(call.name),
            )
            for call in entry.tool_calls
        ]

    @staticmethod
    def _classify_operation(tool_name: str) -> str:
        match tool_name:
            case "read":
                return "read"
            case "write" | "edit":
                return "write"
            case _ if tool_name in SHELL_TOOLS:
                return "exec"
            case "ls" | "grep" | "find":
                return "search"
            case _:
                return "other"

    def _extract_full_text(self, entry: PiEntry) -> str | None:
        match entry.event_type:
            case "compaction" | "branch_summary":
                text = entry.summary.strip()
            case "usage":
                return None
            case "message":
                text = entry.text.strip() or entry.thinking.strip()
                if not text and entry.role == "toolResult" and entry.tool_name:
                    text = f"[{entry.tool_name} result]"
            case "session":
                return None
            case _:
                return None
        return text or None

    # ── tokens / summary ─────────────────────────────────────────

    @staticmethod
    def _extract_tokens(entry: PiEntry) -> TokenUsage | None:
        if entry.token_total <= 0 and entry.token_input <= 0:
            return None
        return TokenUsage(
            input_tokens=entry.token_input,
            # Pi's ``output`` already includes ``reasoning``.
            output_tokens=entry.token_output,
            cache_creation=entry.token_cache_write,
            cache_read=entry.token_cache_read,
        )

    @staticmethod
    def _tokens_dict(entry: PiEntry) -> dict[str, int | float] | None:
        """Event-level tokens, mirroring the keys the other adapters store.

        ``cost`` is Pi's per-message ``usage.cost.total`` (the sessions table's
        ``cost`` column is a monotonic maximum, wrong for per-message costs —
        the JSON payload is the schema that admits it).
        """
        if entry.token_total <= 0 and entry.token_input <= 0:
            return None
        tokens: dict[str, int | float] = {
            "input": entry.token_input,
            "output": entry.token_output,
            "cached_input": entry.token_cache_read,
            "reasoning": entry.token_reasoning,
        }
        if entry.cost_total:
            tokens["cost"] = entry.cost_total
        return tokens

    def _summarize(self, entry: PiEntry) -> str:
        match entry.event_type:
            case "session":
                return f"[session start] cwd={entry.cwd} v{entry.version}"
            case "compaction":
                text = (entry.summary or "").strip().replace("\n", " ")
                return f"[compaction] {text[:100]}" if text else "[compaction]"
            case "branch_summary":
                text = (entry.summary or "").strip().replace("\n", " ")
                return f"[branch summary] {text[:100]}" if text else "[branch summary]"
            case "usage":
                kind = entry.summary or "usage"
                return (
                    f"[tokens] {kind} in={entry.token_input:,} "
                    f"out={entry.token_output:,} cached={entry.token_cache_read:,}"
                )
            case "message":
                match entry.role:
                    case "user":
                        text = (entry.text or "").strip().replace("\n", " ")
                        return text[:120] if text else "[user input]"
                    case "system":
                        text = (entry.text or "").strip().replace("\n", " ")
                        return text[:120] if text else "[system]"
                    case "assistant":
                        text = (entry.text or "").strip().replace("\n", " ")
                        if text:
                            return text[:120]
                        if entry.tool_calls:
                            fc = entry.tool_calls[0]
                            try:
                                brief = json.dumps(fc.arguments, ensure_ascii=False)
                            except (TypeError, ValueError):
                                brief = str(fc.arguments)
                            if len(entry.tool_calls) > 1:
                                return f"{fc.name}: {brief[:80]} (+{len(entry.tool_calls) - 1})"
                            return f"{fc.name}: {brief[:80]}"
                        text = (entry.thinking or "").strip().replace("\n", " ")
                        return f"[thinking] {text[:100]}" if text else "[thinking]"
                    case "toolResult":
                        text = (entry.text or "").strip().replace("\n", " ")
                        if entry.tool_is_error:
                            return f"[error] {text[:110]}" if text else "[error]"
                        name = entry.tool_name or "result"
                        return f"[{name}] {text[:100]}" if text else f"[{name}]"
                    case _:
                        return "[message]"
            case _:
                return f"[{entry.event_type}]"

    @staticmethod
    def _parse_timestamp(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
