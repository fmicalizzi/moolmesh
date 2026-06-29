"""Tests for the Cursor parser."""

import json
import sqlite3
from pathlib import Path

from hub.parsers.cursor_parser import CursorParser


def _make_global_db(path: Path, bubbles: list[tuple[str, str, dict]]) -> None:
    """bubbles: list of (composer_id, bubble_id, value_dict)."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    for composer_id, bubble_id, value in bubbles:
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (f"bubbleId:{composer_id}:{bubble_id}", json.dumps(value)),
        )
    conn.commit()
    conn.close()


def _make_workspace(base: Path, ws_hash: str, folder: str, composers: list[dict]) -> None:
    ws_dir = base / "workspaceStorage" / ws_hash
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": folder}))
    conn = sqlite3.connect(str(ws_dir / "state.vscdb"))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("composer.composerData", json.dumps({"allComposers": composers})),
    )
    conn.commit()
    conn.close()


def test_build_composer_map_maps_project(tmp_path):
    base = tmp_path / "User"
    base.mkdir()
    _make_workspace(
        base, "ws1", "file:///home/u/dev/myproj",
        [{"composerId": "c1", "name": "Fix bug", "createdAt": 1000,
          "lastUpdatedAt": 2000, "totalLinesAdded": 10, "unifiedMode": "agent"}],
    )
    parser = CursorParser(cursor_base=base)
    cmap = parser.build_composer_map()
    assert "c1" in cmap
    assert cmap["c1"].project == "myproj"
    assert cmap["c1"].cwd == "/home/u/dev/myproj"
    assert cmap["c1"].total_lines_added == 10


def test_parse_incremental_reads_new_bubbles_by_rowid(tmp_path):
    base = tmp_path / "User"
    base.mkdir()
    (base / "globalStorage").mkdir()
    gdb = base / "globalStorage" / "state.vscdb"
    _make_global_db(gdb, [
        ("c1", "b1", {"_v": 2, "type": 1, "text": "hello"}),
        ("c1", "b2", {"_v": 2, "type": 2, "text": "hi there", "tokenCount": 42}),
    ])
    _make_workspace(base, "ws1", "file:///home/u/dev/myproj",
                    [{"composerId": "c1", "name": "Chat"}])
    parser = CursorParser(cursor_base=base)
    bubbles, new_offset = parser.parse_incremental(gdb, 0)
    assert [b.text for b in bubbles] == ["hello", "hi there"]
    assert bubbles[1].bubble_type == 2
    assert bubbles[1].token_count == 42
    assert bubbles[0].composer is not None and bubbles[0].composer.project == "myproj"
    assert new_offset == 2
    # Second call from the new offset returns nothing.
    again, off2 = parser.parse_incremental(gdb, new_offset)
    assert again == []
    assert off2 == new_offset


def test_parse_incremental_skips_malformed(tmp_path):
    base = tmp_path / "User"
    base.mkdir()
    (base / "globalStorage").mkdir()
    gdb = base / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(gdb))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                 ("bubbleId:c1:bad", "{not json"))
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                 ("bubbleId:c1:ok", json.dumps({"type": 1, "text": "ok"})))
    conn.commit()
    conn.close()
    parser = CursorParser(cursor_base=base)
    bubbles, _ = parser.parse_incremental(gdb, 0)
    assert [b.text for b in bubbles] == ["ok"]


def test_can_parse_requires_cursordiskkv(tmp_path):
    good = tmp_path / "state.vscdb"
    conn = sqlite3.connect(str(good))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    assert CursorParser.can_parse(good) is True
    assert CursorParser.can_parse(tmp_path / "missing.vscdb") is False
