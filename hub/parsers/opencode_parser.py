"""Parser for OpenCode sessions from SQLite database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hub.models.opencode import OpenCodeEntry, OpenCodeToolCall
from hub.parsers.base import BaseParser


class OpenCodeParser(BaseParser):

    def parse_file(self, path: Path) -> list[OpenCodeEntry]:
        """Parse all sessions from an OpenCode SQLite database."""
        if not path.exists():
            return []

        entries: list[OpenCodeEntry] = []
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            entries = self._extract_all(conn)
        finally:
            conn.close()
        return entries

    def parse_session(self, path: Path, session_id: str) -> list[OpenCodeEntry]:
        """Parse a single session by ID."""
        if not path.exists():
            return []
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            entries = self._extract_session(conn, session_id)
        finally:
            conn.close()
        return entries

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[OpenCodeEntry], int]:
        """Incremental parse using rowid as cursor. offset = last processed rowid."""
        if not path.exists():
            return [], offset
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                self._QUERY + " WHERE pt.rowid > ? ORDER BY pt.rowid ASC LIMIT 500",
                (offset,),
            ).fetchall()
            if not rows:
                conn.close()
                return [], offset
            entries = [e for row in rows if (e := self._row_to_entry(row)) is not None]
            new_offset = conn.execute(
                "SELECT MAX(rowid) FROM part WHERE rowid > ?", (offset,)
            ).fetchone()[0] or offset
            conn.close()
            return entries, new_offset
        except (sqlite3.Error, OSError):
            return [], offset

    @staticmethod
    def can_parse(path: Path) -> bool:
        if path.suffix != ".db" or path.name != "opencode.db":
            return False
        if not path.exists():
            return False
        try:
            conn = sqlite3.connect(str(path), timeout=2)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.close()
            return {"session", "message", "part", "project"}.issubset(tables)
        except (sqlite3.Error, OSError):
            return False

    _QUERY = """
        SELECT
            s.id AS session_id,
            s.directory AS session_dir,
            s.title AS session_title,
            s.model AS session_model,
            s.cost AS session_cost,
            p.name AS project_name,
            p.worktree AS project_worktree,
            m.id AS message_id,
            m.data AS message_data,
            m.time_created AS message_time,
            pt.id AS part_id,
            pt.data AS part_data,
            pt.time_created AS part_time
        FROM part pt
        JOIN message m ON pt.message_id = m.id
        JOIN session s ON pt.session_id = s.id
        LEFT JOIN project p ON s.project_id = p.id
    """

    def _extract_all(self, conn: sqlite3.Connection) -> list[OpenCodeEntry]:
        query = self._QUERY + " ORDER BY pt.time_created ASC"
        rows = conn.execute(query).fetchall()
        return [e for row in rows if (e := self._row_to_entry(row)) is not None]

    def _extract_session(self, conn: sqlite3.Connection, session_id: str) -> list[OpenCodeEntry]:
        query = self._QUERY + " WHERE s.id = ? ORDER BY pt.time_created ASC"
        rows = conn.execute(query, (session_id,)).fetchall()
        return [e for row in rows if (e := self._row_to_entry(row)) is not None]

    def _row_to_entry(self, row: sqlite3.Row) -> OpenCodeEntry | None:
        try:
            part_data = json.loads(row["part_data"]) if row["part_data"] else {}
        except (json.JSONDecodeError, TypeError):
            return None

        part_type = part_data.get("type", "")
        if part_type == "step-start":
            return None

        try:
            msg_data = json.loads(row["message_data"]) if row["message_data"] else {}
        except (json.JSONDecodeError, TypeError):
            msg_data = {}

        role = msg_data.get("role", "")

        model_id = ""
        model_provider = ""
        model_raw = row["session_model"] or ""
        try:
            model_json = json.loads(model_raw) if model_raw else {}
            model_id = model_json.get("id", "")
            model_provider = model_json.get("providerID", "")
        except (json.JSONDecodeError, TypeError):
            model_id = model_raw

        text = self._extract_text(part_data, part_type)
        tool_call = self._extract_tool_call(part_data, part_type)

        token_input = 0
        token_output = 0
        token_reasoning = 0
        token_cache_read = 0
        token_cache_write = 0

        if part_type == "step-finish":
            step_tokens = part_data.get("tokens", {}) or {}
            token_input = step_tokens.get("input", 0) or 0
            token_output = step_tokens.get("output", 0) or 0
            token_reasoning = step_tokens.get("reasoning", 0) or 0
            cache = step_tokens.get("cache", {}) or {}
            token_cache_read = cache.get("read", 0) or 0
            token_cache_write = cache.get("write", 0) or 0

        path_data = msg_data.get("path", {}) or {}
        cwd = path_data.get("cwd", "") or row["session_dir"] or ""

        files_affected: list[str] = []
        if part_type == "patch":
            for f in part_data.get("files", []):
                if isinstance(f, dict) and f.get("path"):
                    files_affected.append(f["path"])

        return OpenCodeEntry(
            session_id=row["session_id"] or "",
            message_id=row["message_id"] or "",
            part_type=part_type,
            role=role,
            text=text,
            timestamp=row["part_time"] or row["message_time"] or "",
            model_id=model_id,
            model_provider=model_provider,
            cwd=cwd,
            cost=row["session_cost"] or 0.0,
            token_input=token_input,
            token_output=token_output,
            token_reasoning=token_reasoning,
            token_cache_read=token_cache_read,
            token_cache_write=token_cache_write,
            tool_call=tool_call,
            files_affected=files_affected,
            project_name=row["project_name"] or "",
            project_dir=row["session_dir"] or "",
            raw=part_data,
        )

    @staticmethod
    def _extract_text(part_data: dict, part_type: str) -> str:
        match part_type:
            case "text":
                return part_data.get("content", "") or part_data.get("text", "")
            case "reasoning":
                return part_data.get("content", "") or part_data.get("text", "")
            case "compaction":
                return part_data.get("summary", "") or part_data.get("content", "")
            case "step-finish":
                return ""
            case "tool":
                state = part_data.get("state", {}) or {}
                return state.get("output", "")[:500] if state.get("output") else ""
            case "file":
                return part_data.get("path", "") or ""
            case "patch":
                files = part_data.get("files", [])
                if files:
                    paths = [f.get("path", "") for f in files if isinstance(f, dict)]
                    return f"[patch: {', '.join(paths)}]"
                return "[patch]"
            case _:
                return ""

    @staticmethod
    def _extract_tool_call(part_data: dict, part_type: str) -> OpenCodeToolCall | None:
        if part_type == "tool":
            tool_name = part_data.get("tool", "") or part_data.get("name", "")
            state = part_data.get("state", {}) or {}
            return OpenCodeToolCall(
                name=tool_name,
                input_data=state.get("input", {}) if isinstance(state.get("input"), dict) else {"raw": str(state.get("input", ""))},
                output_data={"output": str(state.get("output", ""))[:500]} if state.get("output") else {},
                tool_id=part_data.get("id", ""),
            )
        if part_type == "file":
            return OpenCodeToolCall(
                name="file_read",
                input_data={"path": part_data.get("path", "")},
            )
        if part_type == "patch":
            files = part_data.get("files", [])
            paths = [f.get("path", "") for f in files if isinstance(f, dict)]
            return OpenCodeToolCall(
                name="file_edit",
                input_data={"files": paths},
            )
        return None
