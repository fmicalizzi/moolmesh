"""Parser for Codex (GPT-5.x) rollout JSONL files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub.models.codex import CodexEntry, CodexFunctionCall, CodexFunctionOutput
from hub.parsers.base import BaseParser


def _scalar(value: Any, *, max_len: int = 200) -> str:
    """Coerce any JSON value to a short string for a scalar model field (#65).

    String values pass through untouched; ``None``/missing become ``""``;
    numbers and booleans stringify; anything else (dict/list) becomes compact,
    key-sorted JSON truncated to ``max_len`` — never a raw object destined for
    a SQLite TEXT column.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return ""
    return text[:max_len]


def _source_label(value: Any) -> str:
    """Short, stable label for ``session_meta.payload.source`` (#65).

    ``source`` is a string in older rollouts and an object in the new
    sub-agent ones (``{"subagent": {"thread_spawn": {...}}}``). An object
    becomes ``"subagent:thread_spawn"`` — key-sorted so a re-parse (and the
    dedupe fingerprint) is stable — falling back to the outer key, then to
    short JSON.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            inner = value[key]
            if isinstance(inner, dict) and inner:
                for sub in sorted(inner, key=str):
                    return f"{key}:{sub}"
            return _scalar(key, max_len=80)
        return _scalar(value, max_len=80)
    return _scalar(value, max_len=80)


class CodexParser(BaseParser):

    def __init__(self):
        # Session context propagated from session_meta to all subsequent
        # entries, keyed per file: one watcher parser tails many rollouts, and
        # a chunk read from offset > 0 carries no session_meta of its own.
        self._session_ctx: dict[str, dict[str, Any]] = {}

    def parse_file(self, path: Path) -> list[CodexEntry]:
        # Use a local context — thread-safe, no shared state between calls
        local_ctx: dict[str, str] = {}
        entries: list[CodexEntry] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = self._parse_line(raw, ctx=local_ctx)
                if entry is not None:
                    self._apply_session_ctx(entry, ctx=local_ctx)
                    entries.append(entry)
        return entries

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[CodexEntry], int]:
        # NOTE: the per-file context persists between calls (for live watcher)
        ctx = self._session_ctx.setdefault(str(path), {})
        if offset > 0 and not ctx:
            # Resuming mid-file (daemon restart): session_meta was consumed
            # in an earlier run, so re-seed from the file's first line.
            self._seed_session_ctx(path, ctx)
        entries: list[CodexEntry] = []
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
            entry = self._parse_line(raw, ctx=ctx)
            if entry is not None:
                self._apply_session_ctx(entry, ctx=ctx)
                entries.append(entry)
        return entries, new_offset

    def _seed_session_ctx(self, path: Path, ctx: dict[str, Any]) -> None:
        """Fill ``ctx`` from the rollout's first line (``session_meta``, per ``can_parse``)."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.loads(f.readline())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict) and raw.get("type") == "session_meta":
            entry = self._parse_line(raw, ctx=ctx)
            if entry is not None:
                self._apply_session_ctx(entry, ctx=ctx)

    def _apply_session_ctx(self, entry: CodexEntry, ctx: dict[str, Any]) -> None:
        """Store context from session_meta, propagate to all other entries.

        First ``session_meta`` wins the identity context: sub-agent rollouts
        replay the parent's ``session_meta`` as a SECOND line (#65), and letting
        it overwrite ``session_id``/``source`` would attribute the sub-agent's
        own events to the parent (and drop the ``subagent`` label). The replayed
        meta entry itself is still parsed and upserted under its own id.
        """
        if entry.event_type == "session_meta":
            if not ctx.get("session_id"):
                ctx.update({
                    "session_id": entry.session_id,
                    "cwd": entry.cwd,
                    "cli_version": entry.cli_version,
                    "model_provider": entry.model_provider,
                    "source": entry.source,
                    "parent_session_id": entry.parent_session_id,
                    "agent_meta": entry.agent_meta,
                })
        else:
            entry.session_id = ctx.get("session_id", "")
            entry.cwd = ctx.get("cwd", "")
            entry.cli_version = ctx.get("cli_version", "")
            entry.model_provider = ctx.get("model_provider", "")
            entry.source = ctx.get("source", "")
            entry.parent_session_id = ctx.get("parent_session_id", "")
            entry.agent_meta = ctx.get("agent_meta")

    @staticmethod
    def can_parse(path: Path) -> bool:
        if not path.suffix == ".jsonl":
            return False
        if not path.name.startswith("rollout-"):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                if not first:
                    return False
                data = json.loads(first)
                return data.get("type") == "session_meta" and "payload" in data
        except (json.JSONDecodeError, OSError):
            return False

    def _parse_line(self, raw: dict[str, Any], ctx: dict[str, str] | None = None) -> CodexEntry | None:
        event_type = raw.get("type", "")
        timestamp = raw.get("timestamp", "")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        match event_type:
            case "session_meta":
                # New sub-agent rollouts carry non-scalar fields (source,
                # base_instructions, context_window, git, ...). Scalar model
                # fields get scalars only; `source` gets a short stable label
                # and the parent link is captured for `session_links` (#65).
                parent, agent_meta = self._subagent_info(payload)
                return CodexEntry(
                    event_type=event_type,
                    timestamp=timestamp,
                    session_id=_scalar(payload.get("id", "")),
                    cwd=_scalar(payload.get("cwd", "")),
                    cli_version=_scalar(payload.get("cli_version", "")),
                    model_provider=_scalar(payload.get("model_provider", "")),
                    source=_source_label(payload.get("source", "")),
                    parent_session_id=parent,
                    agent_meta=agent_meta,
                    raw=raw,
                )

            case "event_msg":
                subtype = payload.get("type", "")

                if subtype == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    if not last:
                        last = info.get("total_token_usage", {})
                    return CodexEntry(
                        event_type="token_count",
                        timestamp=timestamp,
                        token_input=last.get("input_tokens", 0),
                        token_output=last.get("output_tokens", 0),
                        token_cached_input=last.get("cached_input_tokens", 0),
                        token_reasoning=last.get("reasoning_output_tokens", 0),
                        token_total=last.get("total_tokens", 0),
                        raw=raw,
                    )

                if subtype in ("thread_rolled_back", "thread_name_updated",
                               "context_compacted", "turn_aborted",
                               "item_completed", "thread_settings_applied"):
                    return None

                if subtype == "agent_message":
                    text = payload.get("message", "")
                    return CodexEntry(
                        event_type=event_type,
                        timestamp=timestamp,
                        event_subtype=subtype,
                        event_msg_text=text,
                        role="assistant",
                        raw=raw,
                    )

                if subtype == "exec_command_end":
                    cmd = payload.get("command", [])
                    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                    stdout = payload.get("stdout", "")
                    exit_code = payload.get("exit_code", None)
                    text = f"$ {cmd_str}\n{stdout}"
                    if exit_code is not None and exit_code != 0:
                        text += f"\n[exit code: {exit_code}]"
                    return CodexEntry(
                        event_type=event_type,
                        timestamp=timestamp,
                        event_subtype=subtype,
                        event_msg_text=text,
                        role="tool_result",
                        raw=raw,
                    )

                if subtype == "patch_apply_end":
                    stdout = payload.get("stdout", "")
                    return CodexEntry(
                        event_type=event_type,
                        timestamp=timestamp,
                        event_subtype=subtype,
                        event_msg_text=stdout,
                        role="tool_result",
                        raw=raw,
                    )

                if subtype == "task_started":
                    return None

                if subtype == "task_complete":
                    text = payload.get("last_agent_message", "")
                    return CodexEntry(
                        event_type=event_type,
                        timestamp=timestamp,
                        event_subtype=subtype,
                        event_msg_text=text,
                        role="system",
                        raw=raw,
                    )

                # user_message or legacy format (no subtype)
                text = payload.get("message", "") or payload.get("content", "")
                if isinstance(text, list):
                    text = " ".join(
                        c.get("text", "") for c in text if isinstance(c, dict)
                    )
                if not isinstance(text, str):
                    text = str(text) if text else ""
                text = text.strip()
                if not text:
                    return None
                return CodexEntry(
                    event_type=event_type,
                    timestamp=timestamp,
                    event_subtype=subtype or "user_message",
                    event_msg_text=text,
                    role="user",
                    raw=raw,
                )

            case "response_item":
                return self._parse_response_item(timestamp, payload, raw)

            case "turn_context":
                # Skip turn_context — configuration metadata
                return None

            case _:
                return None

    @staticmethod
    def _subagent_info(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Parent thread id and agent details from a sub-agent ``session_meta``.

        The parent comes from ``payload.parent_thread_id`` with a fallback to
        ``source.subagent.thread_spawn.parent_thread_id`` (the new format).
        Returns ``("", {})`` for ordinary sessions.
        """
        spawn: dict[str, Any] = {}
        source = payload.get("source")
        if isinstance(source, dict):
            subagent = source.get("subagent")
            if isinstance(subagent, dict):
                candidate = subagent.get("thread_spawn")
                if isinstance(candidate, dict):
                    spawn = candidate
        parent = _scalar(payload.get("parent_thread_id")) or _scalar(
            spawn.get("parent_thread_id")
        )
        meta: dict[str, Any] = {}
        for key in ("agent_path", "agent_nickname", "agent_role"):
            value = _scalar(spawn.get(key))
            if value:
                meta[key] = value
        depth = spawn.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool):
            meta["depth"] = depth
        return parent, meta

    def _parse_response_item(
        self, timestamp: str, payload: dict[str, Any], raw: dict
    ) -> CodexEntry | None:
        payload_type = payload.get("type", "")

        match payload_type:
            case "message":
                role = payload.get("role", "")
                content = payload.get("content", [])
                text_parts: list[str] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt in ("input_text", "output_text", "text"):
                            text_parts.append(block.get("text", ""))
                elif isinstance(content, str):
                    text_parts.append(content)

                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    role=role,
                    text="\n".join(text_parts),
                    raw=raw,
                )

            case "function_call":
                raw_args = payload.get("arguments", "")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                fc = CodexFunctionCall(
                    call_id=payload.get("call_id", ""),
                    name=payload.get("name", ""),
                    arguments=raw_args,
                )
                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    function_call=fc,
                    raw=raw,
                )

            case "custom_tool_call":
                # Freeform tools (``exec`` = JavaScript driving tools.*,
                # ``apply_patch`` = raw patch). The raw input rides in
                # ``arguments`` so the call pairs with its output by call_id.
                raw_input = payload.get("input", "")
                if not isinstance(raw_input, str):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                fc = CodexFunctionCall(
                    call_id=payload.get("call_id", ""),
                    name=payload.get("name", ""),
                    arguments=raw_input,
                )
                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    function_call=fc,
                    raw=raw,
                )

            case "function_call_output" | "custom_tool_call_output":
                raw_output = payload.get("output", "")
                if isinstance(raw_output, list):
                    raw_output = "\n".join(
                        o.get("text", str(o)) if isinstance(o, dict) else str(o)
                        for o in raw_output
                    )
                elif not isinstance(raw_output, str):
                    raw_output = str(raw_output)
                fo = CodexFunctionOutput(
                    call_id=payload.get("call_id", ""),
                    output=raw_output,
                )
                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    function_output=fo,
                    raw=raw,
                )

            case "reasoning":
                summary_list = payload.get("summary", [])
                reasoning = ""
                if isinstance(summary_list, list):
                    reasoning = " ".join(
                        s.get("text", "") for s in summary_list if isinstance(s, dict)
                    )
                elif isinstance(summary_list, str):
                    reasoning = summary_list
                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    reasoning_text=reasoning,
                    raw=raw,
                )

            case _:
                return None
