"""Adapter converting Antigravity CLI steps to unified models.

How a step maps to events (the transcript has no ids pairing a call with its
result, so none is attempted):

* ``USER_INPUT`` → ``user``.
* ``PLANNER_RESPONSE`` → ``assistant`` (its ``content``), ``thinking`` (its
  ``thinking``) and one ``tool_use`` per entry of ``tool_calls``. In the real
  format the calls live on this step, not on the result steps.
* ``tool_calls`` on ANY step become ``tool_use`` events, so a format that moves
  them to another step type keeps working.
* Result steps (``GENERIC``, ``RUN_COMMAND``, ``VIEW_FILE``, ``LIST_DIRECTORY``,
  ``FIND``, ``CODE_ACTION``, ``INVOKE_SUBAGENT``, ``ASK_QUESTION``) →
  ``tool_result``. They carry no ``tool_name`` (the SQL tool counters treat a
  non-null ``tool_name`` as a call) and never a ``file_path``.
* ``SYSTEM_MESSAGE`` and ``ERROR_MESSAGE`` → ``system`` (Pi and Claude emit
  ``system`` for system messages; an error notice is worth seeing).
* ``CHECKPOINT`` and ``CONVERSATION_HISTORY`` → ``summary`` (context-compaction /
  resume bookkeeping, the analog of Pi's compaction).
* ``EPHEMERAL_MESSAGE`` → skipped (reminders injected for the model, never
  conversation).
* Any other ``type`` → skipped, counted in ``unknown_types`` and logged once.

Several events of one step share ``created_at`` (one-second resolution) and the
event fingerprint is ``provider|session|timestamp|event_type|summary``: each
``tool_use`` summary therefore carries the tool and its distinguishing args
(path and line range, command, query), so two different calls of one step stay
two events. Two *identical* calls (same tool, same args) collapse into one — as
Pi's do today.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
from collections import Counter
from datetime import datetime
from typing import Any

from hub.adapters.base import BaseAdapter
from hub.log import get as get_logger
from hub.models.antigravity import AntigravityEntry, AntigravityToolCall
from hub.models.base import (
    SHELL_TOOLS,
    MessageRole,
    Provider,
    SessionMeta,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)

_log = get_logger("AntigravityAdapter")

# Cap on the text stored per event (``full_text``): Pi's prompt bound. The
# transcript already truncates ``content`` (a few KB, see ``truncated_fields``);
# this keeps every provider's stored text bounded the same way (#52).
_TEXT_MAX = 2000
_SUMMARY_MAX = 120
_TOOL_SUMMARY_MAX = 200
_TITLE_MAX = 120

# Tools that name exactly one file: tool -> (argument holding the path, operation).
# ``list_dir`` / ``grep_search`` / ``find_by_name`` take directories and
# ``run_command`` is a shell tool whose command must NEVER become a path (#58).
_FILE_TOOLS = {
    "view_file": ("AbsolutePath", "read"),
    "write_to_file": ("TargetFile", "create"),
    "replace_file_content": ("TargetFile", "modify"),
    "multi_replace_file_content": ("TargetFile", "modify"),
}
_SEARCH_TOOLS = frozenset({"list_dir", "grep_search", "find_by_name"})

# Result step type -> label shown in the summary.
_RESULT_TYPES = {
    "GENERIC": "result",
    "RUN_COMMAND": "run_command",
    "VIEW_FILE": "view_file",
    "LIST_DIRECTORY": "list_dir",
    "FIND": "find",
    "CODE_ACTION": "code_action",
    "INVOKE_SUBAGENT": "invoke_subagent",
    "ASK_QUESTION": "ask_question",
}
_SUMMARY_TYPES = {"CHECKPOINT": "checkpoint", "CONVERSATION_HISTORY": "history"}
_SKIPPED_TYPES = frozenset({"EPHEMERAL_MESSAGE"})
# ``args`` keys that restate the call in prose (noise in a one-line summary).
_NOISE_ARGS = frozenset({"toolAction", "toolSummary"})

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _pathmod(p: str):
    """``ntpath`` for drive-letter/UNC paths, ``posixpath`` otherwise."""
    return ntpath if _WIN_ABS_RE.match(p) else posixpath


def _is_abs(p: str) -> bool:
    return p.startswith("/") or bool(_WIN_ABS_RE.match(p))


def _normalize_path(raw: str) -> str | None:
    """Absolute, normalized path — or None for anything that is not a literal file path.

    The value was JSON-decoded by the parser, so a value that still starts
    with a quote is a string the writer cut mid-way: no path, never a half one.
    """
    p = raw.strip()
    if not p or "${" in p or any(ord(c) < 32 for c in p):
        return None
    if p.endswith(("/", "\\")):
        return None  # a directory, not a file the call touched
    if not _is_abs(p):
        return None
    return _pathmod(p).normpath(p)


def _file_path(call: AntigravityToolCall) -> str | None:
    """The absolute file a file tool touched; None for every other tool."""
    spec = _FILE_TOOLS.get(call.name)
    if spec is None:
        return None
    raw = call.args.get(spec[0])
    return _normalize_path(raw) if isinstance(raw, str) else None


def _operation(call: AntigravityToolCall) -> str:
    spec = _FILE_TOOLS.get(call.name)
    if spec is not None:
        if call.name == "write_to_file" and call.args.get("Overwrite") is True:
            return "modify"  # replaces an existing file
        return spec[1]
    if call.name in SHELL_TOOLS:
        return "exec"
    if call.name in _SEARCH_TOOLS:
        return "search"
    return "other"


def _str_arg(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _collapse(text: str) -> str:
    return text.strip().replace("\n", " ")


def _cap(text: str) -> str | None:
    text = text.strip()
    return text[:_TEXT_MAX] if text else None


def _parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _line_range(args: dict[str, Any]) -> str:
    start, end = _str_arg(args, "StartLine"), _str_arg(args, "EndLine")
    if start and end:
        return f" L{start}-{end}"
    if start:
        return f" L{start}"
    return ""


def _tool_brief(call: AntigravityToolCall, path: str | None) -> str:
    """What distinguishes this call from another call of the same tool."""
    name, args = call.name, call.args
    if name in _FILE_TOOLS:
        target = path or _str_arg(args, _FILE_TOOLS[name][0])
        return f"{target}{_line_range(args)}"
    if name == "run_command":
        return _collapse(_str_arg(args, "CommandLine"))[:120]
    if name == "list_dir":
        return _str_arg(args, "DirectoryPath")
    if name == "grep_search":
        query, where = _str_arg(args, "Query"), _str_arg(args, "SearchPath")
        return f"{query} in {where}" if where else query
    if name == "find_by_name":
        pattern, where = _str_arg(args, "Pattern"), _str_arg(args, "SearchDirectory")
        return f"{pattern} in {where}" if where else pattern
    rest = {k: v for k, v in args.items() if k not in _NOISE_ARGS}
    if not rest:
        return ""
    try:
        return json.dumps(rest, ensure_ascii=False, sort_keys=True, default=str)[:120]
    except (TypeError, ValueError):
        return ""



class AntigravityAdapter(BaseAdapter):

    def __init__(self) -> None:
        # Steps of a ``type`` this adapter does not know are skipped (never an
        # error): counted for validation, logged once per type.
        self.unknown_types: Counter[str] = Counter()

    # ── BaseAdapter API ──────────────────────────────────────────

    def to_unified(self, entry: AntigravityEntry, project: str) -> UnifiedMessage | None:
        events = self.to_events(entry, project)
        primary = self._primary(events)
        if primary is None:
            return None
        entry_id = f"{entry.session_id}:{entry.step_index}"
        return UnifiedMessage(
            id=entry_id,
            provider=Provider.ANTIGRAVITY,
            session_id=entry.session_id,
            project=project,
            role=MessageRole(primary.event_type),
            text=self._text(entry, primary),
            tool_calls=[
                ToolCall(
                    name=call.name,
                    input_data=call.args,
                    tool_id=f"{entry_id}:{k}",
                    file_path=_file_path(call),
                    operation_type=_operation(call),
                )
                for k, call in enumerate(entry.tool_calls)
            ],
            timestamp=_parse_timestamp(entry.timestamp),
            model=None,
            tokens=None,
            parent_id=None,
            is_sidechain=bool(entry.parent_session),
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: AntigravityEntry, project: str) -> UnifiedEvent | None:
        """The primary event of ``entry`` (see ``to_events`` for all of them)."""
        return self._primary(self.to_events(entry, project))

    def to_events(self, entry: AntigravityEntry, project: str) -> list[UnifiedEvent]:
        """Every event a step yields: thinking, content, then one tool_use per call."""
        events: list[UnifiedEvent] = []

        if entry.thinking.strip():
            events.append(self._event(
                entry, project, MessageRole.THINKING,
                f"[thinking] {_collapse(entry.thinking)[:100]}",
                full_text=_cap(entry.thinking),
            ))

        content = self._content_event(entry, project)
        if content is not None:
            events.append(content)

        for call in entry.tool_calls:
            path = _file_path(call)
            brief = _tool_brief(call, path)
            name = call.name or "tool"
            summary = f"{name}: {brief}" if brief else name
            events.append(self._event(
                entry, project, MessageRole.TOOL_USE, summary[:_TOOL_SUMMARY_MAX],
                tool_name=call.name or None, file_path=path,
            ))
        return events

    def to_session_meta(self, entry: AntigravityEntry, project: str) -> SessionMeta | None:
        if not entry.session_id:
            return None
        title = entry.summary_title or entry.session_title
        if not title and entry.summary_preview:
            title = entry.summary_preview.splitlines()[0].strip()
        metadata: dict[str, Any] = {}
        if entry.parent_session:
            metadata["parent_session_id"] = entry.parent_session
            metadata["nesting_depth"] = entry.nesting_depth
        if entry.summary_last_modified:
            metadata["summary_last_modified"] = entry.summary_last_modified
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.ANTIGRAVITY,
            project=project,
            title=title[:_TITLE_MAX],
            cwd=entry.cwd or "",
            is_sidechain=bool(entry.parent_session),
            initial_prompt=entry.initial_prompt or "",
            metadata=metadata,
        )

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _primary(events: list[UnifiedEvent]) -> UnifiedEvent | None:
        """The event ``to_event`` reports: the first one that is not a thinking block."""
        for event in events:
            if event.event_type != MessageRole.THINKING.value:
                return event
        return events[0] if events else None

    def _event(
        self,
        entry: AntigravityEntry,
        project: str,
        role: MessageRole,
        summary: str,
        *,
        tool_name: str | None = None,
        file_path: str | None = None,
        full_text: str | None = None,
    ) -> UnifiedEvent:
        return UnifiedEvent(
            provider=Provider.ANTIGRAVITY,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id or None,
            tokens=None,  # tokens, model and cost are not read in v1
            tool_name=tool_name,
            file_path=file_path,
            model=None,
            cwd=entry.cwd or None,
            full_text=full_text,
        )

    def _content_event(self, entry: AntigravityEntry, project: str) -> UnifiedEvent | None:
        """The event of a step's ``content`` (None for a step without one)."""
        step_type = entry.step_type
        text = entry.content.strip()

        if step_type == "USER_INPUT":
            return self._text_event(entry, project, MessageRole.USER, text)
        if step_type == "PLANNER_RESPONSE":
            return self._text_event(entry, project, MessageRole.ASSISTANT, text)
        if step_type == "SYSTEM_MESSAGE":
            return self._text_event(entry, project, MessageRole.SYSTEM, text)
        if step_type == "ERROR_MESSAGE":
            return self._labelled_event(
                entry, project, MessageRole.SYSTEM, "error", entry.error.strip() or text)
        if step_type in _SUMMARY_TYPES:
            return self._labelled_event(
                entry, project, MessageRole.SUMMARY, _SUMMARY_TYPES[step_type], text)
        if step_type in _RESULT_TYPES:
            failed = entry.status == "ERROR" or bool(entry.error.strip())
            label = "error" if failed else _RESULT_TYPES[step_type]
            text = text or entry.error.strip()
            return self._labelled_event(entry, project, MessageRole.TOOL_RESULT, label, text)
        if step_type in _SKIPPED_TYPES:
            return None
        self._note_unknown(step_type)
        return None

    def _text_event(
        self, entry: AntigravityEntry, project: str, role: MessageRole, text: str
    ) -> UnifiedEvent | None:
        if not text:
            return None
        return self._event(
            entry, project, role, _collapse(text)[:_SUMMARY_MAX], full_text=_cap(text),
        )

    def _labelled_event(
        self, entry: AntigravityEntry, project: str, role: MessageRole, label: str, text: str
    ) -> UnifiedEvent | None:
        """A ``[label] text`` event; a step with no text to show yields none (like an empty prompt)."""
        if not text:
            return None
        return self._event(
            entry, project, role, f"[{label}] {_collapse(text)[:100]}", full_text=_cap(text))

    def _note_unknown(self, step_type: str) -> None:
        first = step_type not in self.unknown_types
        self.unknown_types[step_type] += 1
        if first:
            _log.warning(
                "Antigravity: tipo de paso desconocido %r; se ignora (se cuenta, no se rompe)",
                step_type,
            )

    @staticmethod
    def _text(entry: AntigravityEntry, primary: UnifiedEvent) -> str:
        """The text a ``UnifiedMessage`` carries: content, else thinking, else the call."""
        if entry.content.strip():
            return entry.content
        if entry.thinking.strip():
            return entry.thinking
        return primary.summary
