"""Provider contract: every parser+adapter emits well-formed events.

Six providers, one fixture each: JSONL files for claude / codex / qwen / pi, and
an in-test SQLite database for opencode / cursor (their session store is a DB,
not a file). The contract is the *minimum* every provider must uphold, and it is
deliberately provider-agnostic:

  * ``provider`` is the expected enum member;
  * ``session_id`` is present;
  * ``timestamp`` is parseable (ISO 8601 or epoch);
  * ``event_type`` is one of the unified roles;
  * ``file_path`` is absolute (POSIX or Windows) or ``None`` — never truncated,
    and NEVER a shell command (``SHELL_TOOLS``, #58).

Adding a provider to MoolMesh means adding its fixture loader to ``CASES`` and
implementing the quartet; this test then holds it to the same bar.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest

from hub.adapters.base import BaseAdapter
from hub.adapters.claude_adapter import ClaudeAdapter
from hub.adapters.codex_adapter import CodexAdapter
from hub.adapters.cursor_adapter import CursorAdapter
from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.adapters.pi_adapter import PiAdapter
from hub.adapters.qwen_adapter import QwenAdapter
from hub.models.base import SHELL_TOOLS, Provider
from hub.parsers.claude_parser import ClaudeParser
from hub.parsers.codex_parser import CodexParser
from hub.parsers.cursor_parser import CursorParser
from hub.parsers.opencode_parser import OpenCodeParser
from hub.parsers.pi_parser import PiParser
from hub.parsers.qwen_parser import QwenParser

FIXTURES = Path(__file__).parent / "fixtures"

VALID_EVENT_TYPES = {
    "user", "assistant", "system", "tool_use", "tool_result", "thinking", "summary",
}
_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")

EntryCase = tuple[object, BaseAdapter]


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or bool(_WIN_ABS_RE.match(path))


def _parseable_timestamp(ts: str) -> bool:
    if not ts:
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        pass
    try:
        float(ts)
        return True
    except (ValueError, TypeError):
        return False


def _jsonl_cases(parser, adapter, fixture: str) -> Callable[[Path], list[EntryCase]]:
    def load(_tmp: Path) -> list[EntryCase]:
        entries = parser.parse_file(FIXTURES / fixture)
        return [(e, adapter) for e in entries]
    return load


def _opencode_cases(tmp: Path) -> list[EntryCase]:
    from tests.test_opencode_parser import _create_opencode_db

    db = tmp / "opencode.db"
    _create_opencode_db(db, with_data=True)
    parser, adapter = OpenCodeParser(), OpenCodeAdapter()
    return [(e, adapter) for e in parser.parse_file(db)]


def _cursor_cases(tmp: Path) -> list[EntryCase]:
    from tests.test_cursor_parser import _make_global_db, _make_workspace

    base = tmp / "cursor"
    (base / "globalStorage").mkdir(parents=True)
    gdb = base / "globalStorage" / "state.vscdb"
    _make_global_db(gdb, [
        ("c1", "b1", {"_v": 2, "type": 1, "text": "hello"}),
        ("c1", "b2", {"_v": 2, "type": 2, "text": "hi there", "tokenCount": 42}),
    ])
    _make_workspace(base, "ws1", "file:///home/u/dev/myproj",
                    [{"composerId": "c1", "name": "Chat"}])
    parser, adapter = CursorParser(cursor_base=base), CursorAdapter()
    return [(e, adapter) for e in parser.parse_file(gdb)]


CASES: dict[str, tuple[Provider, Callable[[Path], list[EntryCase]]]] = {
    "claude": (Provider.CLAUDE, _jsonl_cases(
        ClaudeParser(), ClaudeAdapter(), "claude_sample.jsonl")),
    "codex": (Provider.CODEX, _jsonl_cases(
        CodexParser(), CodexAdapter(), "codex_sample.jsonl")),
    "qwen": (Provider.QWEN, _jsonl_cases(
        QwenParser(), QwenAdapter(), "qwen_sample.jsonl")),
    "pi": (Provider.PI, _jsonl_cases(
        PiParser(), PiAdapter(), "pi_sample.jsonl")),
    "opencode": (Provider.OPENCODE, _opencode_cases),
    "cursor": (Provider.CURSOR, _cursor_cases),
}


@pytest.mark.parametrize("provider_name", sorted(CASES))
def test_provider_contract(provider_name: str, tmp_path: Path):
    expected_provider, load = CASES[provider_name]
    cases = load(tmp_path)
    assert cases, f"{provider_name}: fixture produced no entries"

    events = 0
    metas = 0
    for entry, adapter in cases:
        meta = adapter.to_session_meta(entry, "proj")
        if meta is not None:
            metas += 1
            assert meta.provider == expected_provider
            assert meta.id, f"{provider_name}: session meta without an id"

        event = adapter.to_event(entry, "proj")
        if event is None:
            continue
        events += 1
        assert event.provider == expected_provider
        assert event.session_id, f"{provider_name}: event without session_id"
        assert event.event_type in VALID_EVENT_TYPES, (
            f"{provider_name}: invalid event_type {event.event_type!r}"
        )
        assert _parseable_timestamp(event.timestamp), (
            f"{provider_name}: unparseable timestamp {event.timestamp!r}"
        )
        if event.file_path:
            assert _is_absolute(event.file_path), (
                f"{provider_name}: relative file_path {event.file_path!r}"
            )
            assert event.tool_name not in SHELL_TOOLS, (
                f"{provider_name}: shell command leaked into file_path "
                f"({event.tool_name}: {event.file_path!r})"
            )

    assert events, f"{provider_name}: adapter produced no events"
    assert metas, f"{provider_name}: adapter produced no session metadata"
