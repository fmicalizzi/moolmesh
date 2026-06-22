"""Parser for Qwen CLI JSONL session files."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from hub.models.qwen import (
    QwenEntry,
    QwenFunctionCall,
    QwenFunctionResponse,
    QwenUsage,
)
from hub.parsers.base import BaseParser


class QwenParser(BaseParser):

    def parse_file(self, path: Path) -> list[QwenEntry]:
        entries: list[QwenEntry] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = self._parse_line(raw)
                if entry is not None:
                    entries.append(entry)
        return entries

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[QwenEntry], int]:
        entries: list[QwenEntry] = []
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
            entry = self._parse_line(raw)
            if entry is not None:
                entries.append(entry)
        return entries, new_offset

    @staticmethod
    def can_parse(path: Path) -> bool:
        if not path.suffix == ".jsonl":
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                if not first:
                    return False
                data = json.loads(first)
                # Qwen entries have uuid + type, and message with parts (or systemPayload)
                has_uuid = "uuid" in data
                has_type = "type" in data
                # Distinguish from Claude: Qwen doesn't have "sessionId" at first,
                # or uses "systemPayload" for system events
                is_qwen = "systemPayload" in data or (
                    has_uuid and has_type and "message" in data
                    and isinstance(data.get("message", {}), dict)
                    and "parts" in data.get("message", {})
                )
                return has_uuid and has_type and is_qwen
        except (json.JSONDecodeError, OSError):
            return False

    def _parse_line(self, raw: dict[str, Any]) -> QwenEntry | None:
        entry_type = raw.get("type", "")

        message = raw.get("message", {})
        # Handle message as string (some Qwen entries)
        if isinstance(message, str):
            try:
                message = ast.literal_eval(message)
            except (ValueError, SyntaxError):
                message = {}
        if not isinstance(message, dict):
            message = {}

        parts = message.get("parts", [])
        if not isinstance(parts, list):
            parts = []

        text_parts: list[str] = []
        has_thought = False
        function_calls: list[QwenFunctionCall] = []
        function_responses: list[QwenFunctionResponse] = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            # Text content
            if "text" in part:
                text_parts.append(part["text"])
                if part.get("thought"):
                    has_thought = True

            # Function call
            if "functionCall" in part:
                fc_data = part["functionCall"]
                if isinstance(fc_data, dict):
                    function_calls.append(
                        QwenFunctionCall(
                            call_id=fc_data.get("id", ""),
                            name=fc_data.get("name", ""),
                            args=fc_data.get("args", {}),
                        )
                    )

            # Function response
            if "functionResponse" in part:
                fr_data = part["functionResponse"]
                if isinstance(fr_data, dict):
                    response = fr_data.get("response", {})
                    output = ""
                    if isinstance(response, dict):
                        output = response.get("output", "")
                    elif isinstance(response, str):
                        output = response
                    function_responses.append(
                        QwenFunctionResponse(
                            call_id=fr_data.get("id", ""),
                            name=fr_data.get("name", ""),
                            output=str(output)[:500],  # truncate large outputs
                        )
                    )

        # Parse usage metadata
        usage = None
        usage_meta = raw.get("usageMetadata")
        if isinstance(usage_meta, dict):
            usage = QwenUsage(
                prompt_tokens=usage_meta.get("promptTokenCount", 0),
                candidates_tokens=usage_meta.get("candidatesTokenCount", 0),
                thoughts_tokens=usage_meta.get("thoughtsTokenCount", 0),
                total_tokens=usage_meta.get("totalTokenCount", 0),
            )

        # For system events, extract text from systemPayload
        if entry_type == "system" and not text_parts:
            sys_payload = raw.get("systemPayload", {})
            if isinstance(sys_payload, dict):
                user_text = sys_payload.get("userText", "")
                if user_text:
                    text_parts.append(user_text)

        return QwenEntry(
            type=entry_type,
            uuid=raw.get("uuid", ""),
            parent_uuid=raw.get("parentUuid"),
            session_id=raw.get("sessionId", ""),
            timestamp=raw.get("timestamp", ""),
            cwd=raw.get("cwd", ""),
            version=raw.get("version", ""),
            model=raw.get("model"),
            role=message.get("role", ""),
            text="\n".join(text_parts),
            has_thought=has_thought,
            function_calls=function_calls,
            function_responses=function_responses,
            usage=usage,
            subtype=raw.get("subtype"),
            raw=raw,
        )
