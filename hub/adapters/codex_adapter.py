"""Adapter converting Codex entries to unified models."""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from hub.adapters.base import BaseAdapter
from hub.models.base import (
    MessageRole,
    Provider,
    SessionMeta,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
    UnifiedMessage,
)
from hub.models.codex import CodexEntry


# --- Touched-path extraction (issue #40) -------------------------------------
#
# Codex encodes edits as apply_patch envelopes (raw, inside a JSON ``cmd``, or
# inside a JavaScript string literal of an ``exec`` custom tool call) and runs
# commands with an explicit ``workdir``. Everything here is regex + string math:
# JavaScript is never evaluated, and paths built dynamically at runtime are
# rejected rather than guessed (the cwd fallback in workspace_store covers them).

# A patch header at a line start (or right after the opening quote of a string
# literal). Ends at a newline or a closing quote.
_PATCH_MARKER_RE = re.compile(
    r"(?:^|(?<=[\"'`]))\*\*\* (?:Add File|Update File|Delete File|Move to): "
    r"([^\r\n\"'`]*)",
    re.MULTILINE,
)
# ``"..." + expr`` right after a captured marker path: a runtime-built path.
_JS_CONCAT_RE = re.compile(r"[\"'`]\s*\+")
# ``workdir: "..."`` / ``"workdir": '...'`` in exec JavaScript.
_JS_WORKDIR_RE = re.compile(
    r"""\bworkdir["']?\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.DOTALL
)
# ``tools.view_image({path: "..."})`` in exec JavaScript.
_JS_VIEW_IMAGE_RE = re.compile(
    r"""tools\.view_image\(\s*\{[^}]*?\bpath["']?\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""",
    re.DOTALL,
)
_JS_INNER_TOOL_RE = re.compile(r"\btools\.(\w+)\s*\(")
_JS_ESCAPE_RE = re.compile(
    r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL
)
_JS_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                      "v": "\v", "0": "\0"}
_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _js_unescape(text: str) -> str:
    """Decode JavaScript string escapes (``\\n``, ``\\\\``, ``\\u00f1`` ...)."""
    def _sub(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc[0] in "ux" and len(esc) > 1:
            try:
                return chr(int(esc[1:].strip("{}"), 16))
            except (ValueError, OverflowError):
                return esc
        return _JS_SIMPLE_ESCAPES.get(esc, esc)

    out = _JS_ESCAPE_RE.sub(_sub, text)
    # Re-pair UTF-16 surrogate escapes (emoji); lone halves become U+FFFD so
    # the string always encodes for SQLite.
    return out.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _pathmod(p: str):
    """``ntpath`` for drive-letter/UNC paths, ``posixpath`` otherwise —
    decided by the path's own style, not the host OS, so Windows rollouts
    normalize the same everywhere."""
    return ntpath if _WIN_ABS_RE.match(p) else posixpath


def _is_abs(p: str) -> bool:
    return p.startswith("/") or bool(_WIN_ABS_RE.match(p))


def _normalize_path(raw: str, base: str | None) -> str | None:
    """Absolute, normalized path — or None for anything that is not a
    literal file path (empty, a directory, a runtime template)."""
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


def _js_literal(pattern: re.Pattern[str], js: str) -> list[str]:
    return [_js_unescape(m.group(2)) for m in pattern.finditer(js)]


def extract_codex_paths(
    name: str, raw_input: str, cwd: str = ""
) -> tuple[list[str], str | None]:
    """Files a Codex tool call touched, plus the call's own working directory.

    ``raw_input`` is the custom tool call's ``input`` (``exec`` JavaScript or a
    raw ``apply_patch`` envelope) or a function call's JSON ``arguments``.
    Returns ``(paths, workdir)``: ``paths`` are absolute and normalized, in
    first-seen order without duplicates (re-parses fingerprint identically);
    relative paths resolve against the call's ``workdir`` when absolute, else
    the session ``cwd``, else are dropped. ``workdir`` is the call's absolute
    working directory (None if absent/relative) — a directory, so it is never
    reported as a touched path. Never raises on malformed input.
    """
    texts: list[str] = []       # may contain apply_patch envelopes
    candidates: list[str] = []  # direct path arguments
    workdirs: list[str] = []

    if name == "exec":
        workdirs = _js_literal(_JS_WORKDIR_RE, raw_input)
        candidates = _js_literal(_JS_VIEW_IMAGE_RE, raw_input)
        texts.append(_js_unescape(raw_input))
    else:
        try:
            args = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError, ValueError):
            args = None
        if isinstance(args, dict):
            wd = args.get("workdir")
            if isinstance(wd, str):
                workdirs.append(wd)
            for key in ("cmd", "command", "input", "patch"):
                val = args.get(key)
                if isinstance(val, list):
                    val = " ".join(str(v) for v in val)
                if isinstance(val, str):
                    texts.append(val)
            # ``path`` only where it is known to name a file (it can be a
            # directory for other tools); ``file_path`` always names one.
            for key in ("file_path", "path") if name == "view_image" else ("file_path",):
                val = args.get(key)
                if isinstance(val, str):
                    candidates.append(val)
        elif isinstance(raw_input, str):
            texts.append(raw_input)  # raw apply_patch envelope

    workdir = next(
        (_pathmod(w).normpath(w) for w in workdirs
         if _is_abs(w) and "${" not in w),
        None,
    )
    base = workdir or (cwd if cwd and _is_abs(cwd) else None)

    found: list[str] = []
    for text in texts:
        for m in _PATCH_MARKER_RE.finditer(text):
            if _JS_CONCAT_RE.match(text, m.end()):
                continue  # "*** Add File: " + dir + "/x" — built at runtime
            found.append(m.group(1))
    found.extend(candidates)

    paths: list[str] = []
    for raw in found:
        p = _normalize_path(raw, base)
        if p and p not in paths:
            paths.append(p)
    return paths, workdir


def _exec_tool_name(js: str) -> str:
    """The single ``tools.<name>(...)`` an exec script calls, else ``exec``."""
    inner = set(_JS_INNER_TOOL_RE.findall(js))
    return inner.pop() if len(inner) == 1 else "exec"


class CodexAdapter(BaseAdapter):

    def to_unified(self, entry: CodexEntry, project: str) -> UnifiedMessage | None:
        role = self._map_role(entry)
        if role is None:
            return None

        tool_calls = self._extract_tool_calls(entry)
        timestamp = self._parse_timestamp(entry.timestamp)
        text = self._extract_text(entry)

        tokens = self._extract_tokens(entry)

        return UnifiedMessage(
            id=entry.session_id or entry.timestamp,
            provider=Provider.CODEX,
            session_id=entry.session_id,
            project=project,
            role=role,
            text=text,
            tool_calls=tool_calls,
            timestamp=timestamp,
            model=entry.model_provider or None,
            tokens=tokens,
            cwd=entry.cwd or None,
            raw=entry.raw,
        )

    def to_event(self, entry: CodexEntry, project: str) -> UnifiedEvent | None:
        """The primary event for ``entry`` (first touched path, if any)."""
        built = self._build_event(entry, project)
        return built[0] if built else None

    def to_events(self, entry: CodexEntry, project: str) -> list[UnifiedEvent]:
        """Primary event plus one extra event per additional directory touched.

        A single Codex call can patch files in several directories (often in
        different workspaces). The primary event carries the first path; each
        further *distinct containing directory* gets one extra ``tool_use``
        event carrying its first path — one per directory, not per file, so a
        10-file patch in one folder still counts once. Extras have summary
        ``"<tool>: <path>"``: unique per directory (the EventStore fingerprint
        includes the summary, so they neither collapse into the primary nor
        into each other) and deterministic (a re-parse dedupes, never
        duplicates). Extras carry no tokens and no full_text.
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
        self, entry: CodexEntry, project: str
    ) -> tuple[UnifiedEvent, list[str]] | None:
        role = self._map_role(entry)
        if role is None:
            return None

        summary = self._summarize(entry)
        tool_name = None
        file_path = None
        cwd = entry.cwd or None
        paths: list[str] = []

        if entry.function_call:
            fc = entry.function_call
            tool_name = fc.name
            paths, workdir = extract_codex_paths(fc.name, fc.arguments, entry.cwd)
            if workdir:
                cwd = workdir
            if entry.payload_type == "custom_tool_call" and fc.name == "exec":
                tool_name = _exec_tool_name(fc.arguments)
            if paths:
                # Untruncated: the workspace layer resolves this path.
                file_path = paths[0]
            elif entry.payload_type == "function_call":
                # No touched path: keep the legacy brief of the command.
                try:
                    args = json.loads(fc.arguments)
                    cmd = args.get("command", "")
                    fp = args.get("file_path", "")
                    file_path = (
                        (cmd[:80] if isinstance(cmd, str) else "")
                        or (fp[:80] if isinstance(fp, str) else "")
                    )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    file_path = fc.arguments[:80]

        tokens_dict = None
        if entry.event_type == "token_count" and entry.token_total > 0:
            tokens_dict = {
                "input": entry.token_input,
                "output": entry.token_output,
                "cached_input": entry.token_cached_input,
                "reasoning": entry.token_reasoning,
            }

        full_text = self._extract_full_text(entry)

        event = UnifiedEvent(
            provider=Provider.CODEX,
            project=project,
            event_type=role.value,
            timestamp=entry.timestamp,
            summary=summary,
            session_id=entry.session_id or None,
            tokens=tokens_dict,
            tool_name=tool_name,
            file_path=file_path if file_path else None,
            cwd=cwd,
            full_text=full_text,
        )
        return event, paths

    def to_session_meta(self, entry: CodexEntry, project: str) -> SessionMeta | None:
        return SessionMeta(
            id=entry.session_id,
            provider=Provider.CODEX,
            project=project,
            cwd=entry.cwd or "",
            model=entry.model_provider or "",
            cli_version=entry.cli_version or "",
            source=entry.source or "",
        )

    def _map_role(self, entry: CodexEntry) -> MessageRole | None:
        match entry.event_type:
            case "session_meta":
                return MessageRole.SYSTEM
            case "event_msg":
                match entry.role:
                    case "assistant":
                        return MessageRole.ASSISTANT
                    case "tool_result":
                        return MessageRole.TOOL_RESULT
                    case "system":
                        return MessageRole.SYSTEM
                    case _:
                        text = entry.event_msg_text
                        if text and self._is_system_prompt(text):
                            return MessageRole.SYSTEM
                        return MessageRole.USER
            case "token_count":
                return MessageRole.SUMMARY
            case "response_item":
                match entry.payload_type:
                    case "message":
                        # "developer" role = system instructions for the model
                        if entry.role == "developer":
                            return MessageRole.SYSTEM
                        if entry.role == "user":
                            # Check if user message is actually a system prompt
                            if entry.text and self._is_system_prompt(entry.text):
                                return MessageRole.SYSTEM
                            return MessageRole.USER
                        return MessageRole.ASSISTANT
                    case "function_call" | "custom_tool_call":
                        return MessageRole.TOOL_USE
                    case "function_call_output" | "custom_tool_call_output":
                        return MessageRole.TOOL_RESULT
                    case "reasoning":
                        return MessageRole.THINKING
                    case _:
                        return None
            case _:
                return None

    @staticmethod
    def _is_system_prompt(text: str) -> bool:
        """Detect system/developer prompts that are not real user input.

        These patterns appear in Codex sessions as event_msg or response_item
        with role 'user' but are actually system-injected instructions.
        """
        stripped = text.strip()
        # XML-style system blocks
        if stripped.startswith("<") and any(
            stripped.startswith(f"<{tag}")
            for tag in ("permissions", "skills_instructions", "environment_context",
                        "system", "instructions", "tool_instructions")
        ):
            return True
        # Very long messages (>2000 chars) starting with common system patterns
        if len(stripped) > 2000 and any(
            stripped.startswith(prefix)
            for prefix in ("You are ", "You have ", "The following ", "## ")
        ):
            return True
        return False

    @staticmethod
    def _extract_tokens(entry: CodexEntry) -> TokenUsage | None:
        if entry.event_type != "token_count" or entry.token_total == 0:
            return None
        return TokenUsage(
            input_tokens=entry.token_input,
            output_tokens=entry.token_output + entry.token_reasoning,
            cache_creation=0,
            cache_read=entry.token_cached_input,
        )

    def _extract_text(self, entry: CodexEntry) -> str:
        if entry.event_msg_text:
            return entry.event_msg_text
        if entry.text:
            return entry.text
        if entry.reasoning_text:
            return entry.reasoning_text
        if entry.function_call:
            return f"{entry.function_call.name}({entry.function_call.arguments[:200]})"
        if entry.function_output:
            return entry.function_output.output
        return ""

    def _extract_tool_calls(self, entry: CodexEntry) -> list[ToolCall]:
        if not entry.function_call:
            return []
        try:
            args = json.loads(entry.function_call.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {"raw": entry.function_call.arguments}
        return [
            ToolCall(
                name=entry.function_call.name,
                input_data=args,
                tool_id=entry.function_call.call_id,
                operation_type="exec",
            )
        ]

    def _extract_full_text(self, entry: CodexEntry) -> str | None:
        result: Any = None
        match entry.event_type:
            case "event_msg":
                text = entry.event_msg_text.strip() if entry.event_msg_text else ""
                result = text if text else None
            case "response_item":
                match entry.payload_type:
                    case "message":
                        text = entry.text.strip() if entry.text else ""
                        result = text if text else None
                    case "function_call_output" | "custom_tool_call_output":
                        if entry.function_output and entry.function_output.output:
                            result = entry.function_output.output
                    case "reasoning":
                        if entry.reasoning_text:
                            result = entry.reasoning_text.strip()
        if result is None:
            return None
        if not isinstance(result, str):
            result = str(result)
        return result if result else None

    def _summarize(self, entry: CodexEntry) -> str:
        match entry.event_type:
            case "session_meta":
                return f"[session start] cwd={entry.cwd} v{entry.cli_version}"
            case "token_count":
                return f"[tokens] in={entry.token_input:,} out={entry.token_output:,} cached={entry.token_cached_input:,} reasoning={entry.token_reasoning:,}"
            case "event_msg":
                text = (entry.event_msg_text or "").strip().replace("\n", " ")
                match entry.event_subtype:
                    case "agent_message":
                        return text[:120] if text else "[agent message]"
                    case "exec_command_end":
                        return text[:120] if text else "[command output]"
                    case "patch_apply_end":
                        return text[:120] if text else "[patch result]"
                    case "task_complete":
                        return text[:120] if text else "[task complete]"
                    case _:
                        return text[:120] if text else "[user input]"
            case "response_item":
                match entry.payload_type:
                    case "message":
                        text = (entry.text or "").strip().replace("\n", " ")
                        return text[:120] if text else "[message]"
                    case "function_call" | "custom_tool_call":
                        if entry.function_call:
                            name = entry.function_call.name
                            args_brief = entry.function_call.arguments[:80]
                            return f"{name}: {args_brief}"
                        return "[function call]"
                    case "function_call_output" | "custom_tool_call_output":
                        if entry.function_output:
                            return f"[output] {entry.function_output.output[:100]}"
                        return "[function output]"
                    case "reasoning":
                        return f"[reasoning] {(entry.reasoning_text or '')[:100]}"
                    case _:
                        return f"[{entry.payload_type}]"
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
