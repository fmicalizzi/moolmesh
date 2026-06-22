"""Parser for Codex (GPT-5.x) rollout JSONL files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub.models.codex import CodexEntry, CodexFunctionCall, CodexFunctionOutput
from hub.parsers.base import BaseParser


class CodexParser(BaseParser):

    def __init__(self):
        # Session context propagated from session_meta to all subsequent entries
        self._session_ctx: dict[str, str] = {}

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
        # NOTE: _session_ctx persists between calls (for live watcher)
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
            entry = self._parse_line(raw, ctx=self._session_ctx)
            if entry is not None:
                self._apply_session_ctx(entry, ctx=self._session_ctx)
                entries.append(entry)
        return entries, new_offset

    def _apply_session_ctx(self, entry: CodexEntry, ctx: dict[str, str] | None = None) -> None:
        """Store context from session_meta, propagate to all other entries."""
        if ctx is None:
            ctx = self._session_ctx
        if entry.event_type == "session_meta":
            ctx.update({
                "session_id": entry.session_id,
                "cwd": entry.cwd,
                "cli_version": entry.cli_version,
                "model_provider": entry.model_provider,
                "source": entry.source,
            })
        else:
            entry.session_id = ctx.get("session_id", "")
            entry.cwd = ctx.get("cwd", "")
            entry.cli_version = ctx.get("cli_version", "")
            entry.model_provider = ctx.get("model_provider", "")
            entry.source = ctx.get("source", "")

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
                return CodexEntry(
                    event_type=event_type,
                    timestamp=timestamp,
                    session_id=payload.get("id", ""),
                    cwd=payload.get("cwd", ""),
                    cli_version=payload.get("cli_version", ""),
                    model_provider=payload.get("model_provider", ""),
                    source=payload.get("source", ""),
                    raw=raw,
                )

            case "event_msg":
                # Check if this is a token_count event
                if payload.get("type") == "token_count":
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

                # User input event
                text = ""
                if isinstance(payload, dict):
                    content = payload.get("content", "")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                return CodexEntry(
                    event_type=event_type,
                    timestamp=timestamp,
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
                fc = CodexFunctionCall(
                    call_id=payload.get("call_id", ""),
                    name=payload.get("name", ""),
                    arguments=payload.get("arguments", ""),
                )
                return CodexEntry(
                    event_type="response_item",
                    timestamp=timestamp,
                    payload_type=payload_type,
                    function_call=fc,
                    raw=raw,
                )

            case "function_call_output":
                fo = CodexFunctionOutput(
                    call_id=payload.get("call_id", ""),
                    output=payload.get("output", ""),
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
