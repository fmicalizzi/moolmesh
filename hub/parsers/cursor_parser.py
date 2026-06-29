"""Parser for Cursor sessions from VS Code-style SQLite key-value stores.

Bubbles (messages) live in <base>/globalStorage/state.vscdb -> cursorDiskKV,
keyed `bubbleId:<composerId>:<bubbleId>`, append-mostly so rowid works as an
incremental cursor. Project/model attribution comes from each
workspaceStorage/<hash>/state.vscdb (composer.composerData) + workspace.json.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from hub.models.cursor import CursorBubble, CursorComposer
from hub.parsers.base import BaseParser

_MAP_TTL_SECONDS = 30.0
_BUBBLE_BATCH = 500


def default_cursor_base() -> Path:
    """Per-platform Cursor User directory."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if sys.platform.startswith("win"):
        import os
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Cursor" / "User"
        return home / "AppData" / "Roaming" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


def decode_project_name(folder_uri: str) -> tuple[str, str]:
    """Return (project_name, cwd) from a workspace.json 'folder' value."""
    if not folder_uri:
        return "", ""
    path = folder_uri
    if folder_uri.startswith("file:"):
        path = unquote(urlparse(folder_uri).path)
    path = path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    return name, path


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB read-only; never blocks the writer."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


class CursorParser(BaseParser):

    def __init__(self, cursor_base: Path | None = None):
        self._base = cursor_base or default_cursor_base()
        self._map: dict[str, CursorComposer] = {}
        self._map_built_at: float = -1e9

    # ── public API ──────────────────────────────────────────────

    def build_composer_map(self) -> dict[str, CursorComposer]:
        """Scan every workspace DB, returning composerId -> CursorComposer."""
        result: dict[str, CursorComposer] = {}
        ws_root = self._base / "workspaceStorage"
        if not ws_root.is_dir():
            self._map = result
            self._map_built_at = time.monotonic()
            return result
        for ws_dir in ws_root.iterdir():
            if not ws_dir.is_dir():
                continue
            folder = self._read_workspace_folder(ws_dir)
            project, cwd = decode_project_name(folder)
            for comp in self._read_workspace_composers(ws_dir):
                comp.project = project
                comp.cwd = cwd
                result[comp.composer_id] = comp
        self._map = result
        self._map_built_at = time.monotonic()
        return result

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[CursorBubble], int]:
        if not path.exists():
            return [], offset
        self._maybe_refresh_map()
        try:
            conn = _ro_connect(path)
        except (sqlite3.Error, OSError):
            return [], offset
        try:
            rows = conn.execute(
                "SELECT rowid, key, value FROM cursorDiskKV "
                "WHERE rowid > ? AND key LIKE 'bubbleId:%' "
                "ORDER BY rowid ASC LIMIT ?",
                (offset, _BUBBLE_BATCH),
            ).fetchall()
        except sqlite3.Error:
            conn.close()
            return [], offset
        bubbles: list[CursorBubble] = []
        new_offset = offset
        for row in rows:
            new_offset = max(new_offset, row["rowid"])
            bubble = self._row_to_bubble(row)
            if bubble is not None:
                bubbles.append(bubble)
        conn.close()
        return bubbles, new_offset

    def parse_file(self, path: Path) -> list[CursorBubble]:
        bubbles, _ = self.parse_incremental(path, 0)
        return bubbles

    @staticmethod
    def can_parse(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.close()
            return "cursorDiskKV" in tables
        except (sqlite3.Error, OSError):
            return False

    # ── internals ───────────────────────────────────────────────

    def _maybe_refresh_map(self) -> None:
        if time.monotonic() - self._map_built_at > _MAP_TTL_SECONDS:
            self.build_composer_map()

    def _read_workspace_folder(self, ws_dir: Path) -> str:
        try:
            data = json.loads((ws_dir / "workspace.json").read_text())
            return data.get("folder", "") or ""
        except (OSError, json.JSONDecodeError):
            return ""

    def _read_workspace_composers(self, ws_dir: Path) -> list[CursorComposer]:
        db = ws_dir / "state.vscdb"
        if not db.exists():
            return []
        try:
            conn = _ro_connect(db)
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
            ).fetchone()
            conn.close()
        except (sqlite3.Error, OSError):
            return []
        if not row or not row["value"]:
            return []
        try:
            data = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return []
        out: list[CursorComposer] = []
        for c in data.get("allComposers", []):
            if not isinstance(c, dict) or not c.get("composerId"):
                continue
            out.append(CursorComposer(
                composer_id=c.get("composerId", ""),
                name=c.get("name", "") or c.get("subtitle", ""),
                model=c.get("model", "") or "",
                created_at=int(c.get("createdAt", 0) or 0),
                last_updated_at=int(c.get("lastUpdatedAt", 0) or 0),
                total_lines_added=int(c.get("totalLinesAdded", 0) or 0),
                total_lines_removed=int(c.get("totalLinesRemoved", 0) or 0),
                files_changed_count=int(c.get("filesChangedCount", 0) or 0),
                unified_mode=c.get("unifiedMode", "") or "",
            ))
        return out

    def _row_to_bubble(self, row: sqlite3.Row) -> CursorBubble | None:
        try:
            data = json.loads(row["value"]) if row["value"] else None
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        key_parts = row["key"].split(":")
        composer_id = key_parts[1] if len(key_parts) > 1 else ""
        bubble_id = key_parts[2] if len(key_parts) > 2 else ""
        btype = data.get("type", 0)
        if not isinstance(btype, int):
            btype = 0
        tool_name, file_path = self._extract_tool(data)
        token_count = data.get("tokenCount", 0)
        if not isinstance(token_count, int):
            token_count = 0
        return CursorBubble(
            composer_id=composer_id,
            bubble_id=bubble_id,
            bubble_type=btype,
            text=(data.get("text", "") or "").strip(),
            token_count=token_count,
            tool_name=tool_name,
            file_path=file_path,
            is_agentic=bool(data.get("isAgentic", False)),
            rowid=row["rowid"],
            composer=self._map.get(composer_id),
            raw=data,
        )

    @staticmethod
    def _extract_tool(data: dict[str, Any]) -> tuple[str, str]:
        """Best-effort tool name + file path from a bubble (schema is loose)."""
        tool_name = ""
        file_path = ""
        tool_results = data.get("toolResults")
        if isinstance(tool_results, list) and tool_results:
            first = tool_results[0]
            if isinstance(first, dict):
                tool_name = first.get("tool", "") or first.get("name", "") or ""
        code_blocks = data.get("codeBlocks")
        if isinstance(code_blocks, list) and code_blocks:
            first = code_blocks[0]
            if isinstance(first, dict):
                file_path = first.get("uri", "") or first.get("file", "") or ""
        return tool_name[:80], file_path[:80]
