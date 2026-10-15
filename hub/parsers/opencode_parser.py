"""Parser for OpenCode sessions from SQLite database."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from hub.log import get as get_logger
from hub.models.opencode import OpenCodeEntry, OpenCodeToolCall
from hub.parsers.base import BaseParser
from hub.sqlite_ro import connect_ro

_log = get_logger("OpenCodeParser")

# OpenCode inserts a ``part`` row when a stream starts and UPDATEs it in place
# as text/tool state arrives (#75). A part still streaming after this long is
# treated as complete: a cut stream or crashed session never gets its end mark.
_STALE_MS = 10 * 60 * 1000
_BATCH = 500


def _is_complete(part_data: dict, role: str, time_updated: object, now_ms: int) -> bool:
    """Whether a ``part`` row has its final content (#75).

    * ``text`` / ``reasoning``: complete once ``data.time.end`` is set. A user
      ``text`` part has no ``data.time`` and is complete when inserted; a part
      with no ``time`` at all carries no streaming signal and counts as complete.
    * ``tool``: complete when ``data.state.status`` is ``completed``/``error``;
      ``pending``/``running`` are in progress. No status = no signal = complete.
    * Anything else (``step-start``, ``step-finish``, ``patch``, ``file``,
      ``compaction``, unknown types) is complete when read.

    An in-progress part whose ``time_updated`` (ms epoch) is older than
    ``_STALE_MS`` — or unreadable — is treated as complete, so a stream that
    never finishes can't pin the cursor forever.
    """
    if not isinstance(part_data, dict):
        return True
    part_type = part_data.get("type", "")
    in_progress = False
    if part_type in ("text", "reasoning"):
        t = part_data.get("time")
        if not (part_type == "text" and role == "user") and isinstance(t, dict):
            in_progress = not t.get("end")
    elif part_type == "tool":
        state = part_data.get("state")
        if isinstance(state, dict):
            in_progress = state.get("status") in ("pending", "running")
    if not in_progress:
        return True
    try:
        updated = int(time_updated)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    return now_ms - updated > _STALE_MS


class OpenCodeParser(BaseParser):
    """Reads ``opencode.db`` — always read-only (``hub.sqlite_ro``, #51)."""

    def __init__(self) -> None:
        # Incremental reads poll every few seconds: log a failing DB once, when
        # it starts failing, and once when it reads again — not every poll.
        self._failing = False

    def _connect(self, path: Path, timeout: float = 5.0) -> sqlite3.Connection:
        conn = connect_ro(path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def parse_file(self, path: Path) -> list[OpenCodeEntry]:
        """Parse all sessions from an OpenCode SQLite database."""
        if not path.exists():
            return []
        try:
            conn = self._connect(path)
            try:
                return self._extract_all(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            _log.warning("No se pudo leer %s en solo lectura; se saltea", path, exc_info=True)
            return []

    def parse_session(
        self, path: Path, session_id: str, now_ms: int | None = None
    ) -> list[OpenCodeEntry]:
        """Parse a single session by ID.

        With ``now_ms``, parts still streaming (``_is_complete``, including its
        staleness cap) are left out — the ``--reparse opencode`` path (#75).
        """
        if not path.exists():
            return []
        try:
            conn = self._connect(path)
            try:
                return self._extract_session(conn, session_id, now_ms)
            finally:
                conn.close()
        except sqlite3.Error:
            _log.warning("No se pudo leer %s en solo lectura; se saltea", path, exc_info=True)
            return []

    def parse_incremental(
        self, path: Path, offset: int, now_ms: int | None = None
    ) -> tuple[list[OpenCodeEntry], int]:
        """Incremental parse using rowid as cursor. offset = last processed rowid.

        Reads one batch of ``_BATCH`` parts after ``offset`` (#75):

        * complete parts (``_is_complete``) are emitted;
        * a part still streaming is NOT emitted and holds the cursor at its
          rowid - 1, so its later UPDATEs are read on a next poll. Complete
          parts after it are emitted now and re-read then; the event
          fingerprint + ``INSERT OR IGNORE`` drop the repeats (no SSE either);
        * otherwise the cursor moves to the batch's last rowid — never to the
          table's global MAX(rowid), which skipped everything past the first
          500 new parts of a poll.

        ``now_ms`` (ms epoch) is injectable for tests. A DB that can't be read
        (e.g. a WAL whose ``-shm`` can't be created) is skipped for this cycle
        — never reopened read-write.
        """
        if not path.exists():
            return [], offset
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            conn = self._connect(path)
            try:
                rows = conn.execute(
                    self._QUERY + " WHERE pt.rowid > ? ORDER BY pt.rowid ASC LIMIT ?",
                    (offset, _BATCH),
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            if not self._failing:
                self._failing = True
                _log.warning(
                    "No se pudo leer %s en solo lectura; se saltea hasta que vuelva a abrir",
                    path, exc_info=True,
                )
            return [], offset
        if self._failing:
            self._failing = False
            _log.info("%s vuelve a leerse en solo lectura", path)
        if not rows:
            return [], offset

        entries: list[OpenCodeEntry] = []
        pending_rid: int | None = None
        for row in rows:
            if not self._row_is_complete(row, now_ms):
                if pending_rid is None:
                    pending_rid = row["rid"]
                continue
            if (e := self._row_to_entry(row)) is not None:
                entries.append(e)
        new_offset = pending_rid - 1 if pending_rid is not None else rows[-1]["rid"]
        return entries, max(new_offset, offset)

    @staticmethod
    def _row_is_complete(row: sqlite3.Row, now_ms: int) -> bool:
        """``_is_complete`` on a raw query row (bad JSON counts as complete)."""
        try:
            part_data = json.loads(row["part_data"]) if row["part_data"] else {}
            msg_data = json.loads(row["message_data"]) if row["message_data"] else {}
        except (json.JSONDecodeError, TypeError):
            return True
        role = msg_data.get("role", "") if isinstance(msg_data, dict) else ""
        return _is_complete(part_data, role, row["part_updated"], now_ms)

    @staticmethod
    def can_parse(path: Path) -> bool:
        if path.suffix != ".db" or path.name != "opencode.db":
            return False
        if not path.exists():
            return False
        try:
            conn = connect_ro(path, timeout=2)
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            finally:
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
            pt.rowid AS rid,
            pt.id AS part_id,
            pt.data AS part_data,
            pt.time_created AS part_time,
            pt.time_updated AS part_updated
        FROM part pt
        JOIN message m ON pt.message_id = m.id
        JOIN session s ON pt.session_id = s.id
        LEFT JOIN project p ON s.project_id = p.id
    """

    def _extract_all(self, conn: sqlite3.Connection) -> list[OpenCodeEntry]:
        query = self._QUERY + " ORDER BY pt.time_created ASC"
        rows = conn.execute(query).fetchall()
        return [e for row in rows if (e := self._row_to_entry(row)) is not None]

    def _extract_session(
        self, conn: sqlite3.Connection, session_id: str, now_ms: int | None = None
    ) -> list[OpenCodeEntry]:
        # pt.session_id keeps the lookup on part_session_idx.
        query = self._QUERY + " WHERE pt.session_id = ? ORDER BY pt.time_created ASC"
        rows = conn.execute(query, (session_id,)).fetchall()
        if now_ms is not None:
            rows = [r for r in rows if self._row_is_complete(r, now_ms)]
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
            session_title=row["session_title"] or "",
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
                return state.get("output", "") if state.get("output") else ""
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
                output_data={"output": str(state.get("output", ""))} if state.get("output") else {},
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
