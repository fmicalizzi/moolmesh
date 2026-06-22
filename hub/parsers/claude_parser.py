"""Parser for Claude Code JSONL session files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub.models.claude import ClaudeContentBlock, ClaudeEntry, ClaudeUsage
from hub.parsers.base import BaseParser

# Entry types we skip entirely
_SKIP_TYPES = frozenset({"file-history-snapshot"})


class ClaudeParser(BaseParser):

    def parse_file(self, path: Path) -> list[ClaudeEntry]:
        entries: list[ClaudeEntry] = []
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

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[ClaudeEntry], int]:
        entries: list[ClaudeEntry] = []
        with open(path, "rb") as f:
            # Truncation detection: if offset > file size, file was rewritten
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if offset > file_size:
                offset = 0
            f.seek(offset)
            data = f.read()
        if not data:
            return entries, offset

        # Chunk-and-tail: only advance offset to last complete newline
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet — don't advance offset, re-read next time
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
        """Sniff first line to detect Claude JSONL format."""
        if not path.suffix == ".jsonl":
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                if not first:
                    return False
                data = json.loads(first)
                # Claude entries have sessionId and type at top level
                return "sessionId" in data and "type" in data
        except (json.JSONDecodeError, OSError):
            return False

    def _parse_line(self, raw: dict[str, Any]) -> ClaudeEntry | None:
        entry_type = raw.get("type", "")
        if entry_type in _SKIP_TYPES:
            return None

        message = raw.get("message", {})
        if isinstance(message, str):
            # Some system entries have message as string
            message = {}

        content_blocks, content_text = self._parse_content_blocks(
            message.get("content", "")
        )
        usage = self._parse_usage(message.get("usage"))

        return ClaudeEntry(
            type=entry_type,
            uuid=raw.get("uuid", ""),
            parent_uuid=raw.get("parentUuid"),
            session_id=raw.get("sessionId", ""),
            timestamp=raw.get("timestamp", ""),
            cwd=raw.get("cwd", ""),
            version=raw.get("version", ""),
            is_sidechain=raw.get("isSidechain", False),
            git_branch=raw.get("gitBranch"),
            role=message.get("role", ""),
            content_blocks=content_blocks,
            content_text=content_text,
            model=message.get("model"),
            message_id=message.get("id"),
            usage=usage,
            stop_reason=message.get("stop_reason"),
            subtype=raw.get("subtype"),
            raw=raw,
        )

    def _parse_content_blocks(
        self, content: Any
    ) -> tuple[list[ClaudeContentBlock], str]:
        """Parse message.content which can be str or list[dict]."""
        if isinstance(content, str):
            return [], content

        if not isinstance(content, list):
            return [], str(content) if content else ""

        blocks: list[ClaudeContentBlock] = []
        text_parts: list[str] = []

        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = item.get("type", "")

            block = ClaudeContentBlock(type=block_type)

            match block_type:
                case "text":
                    block.text = item.get("text", "")
                    text_parts.append(block.text)
                case "thinking":
                    block.thinking = item.get("thinking", "")
                case "tool_use":
                    block.tool_name = item.get("name", "")
                    block.tool_id = item.get("id", "")
                    block.tool_input = item.get("input", {})
                case "tool_result":
                    block.tool_use_id = item.get("tool_use_id", "")
                    block.tool_content = item.get("content", "")

            blocks.append(block)

        return blocks, "\n".join(text_parts)

    def _parse_usage(self, usage_dict: dict | None) -> ClaudeUsage | None:
        if not usage_dict or not isinstance(usage_dict, dict):
            return None
        return ClaudeUsage(
            input_tokens=usage_dict.get("input_tokens", 0),
            output_tokens=usage_dict.get("output_tokens", 0),
            cache_creation_input_tokens=usage_dict.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage_dict.get("cache_read_input_tokens", 0),
        )
