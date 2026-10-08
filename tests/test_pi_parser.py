"""Tests for the Pi session parser (tree, dedupe, incremental offsets)."""

import json
from pathlib import Path

from hub.parsers.pi_parser import PiParser, linearize
from hub.models.pi import PiEntry

FIXTURE = Path(__file__).parent / "fixtures" / "pi_sample.jsonl"


def _entry(**overrides) -> dict:
    base = {
        "type": "message", "id": "x1", "parentId": "p1",
        "timestamp": "2026-05-01T10:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, *lines: dict) -> Path:
    f = tmp_path / "session.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return f


HEADER = {
    "type": "session", "version": 3, "id": "sess-1",
    "timestamp": "2026-05-01T10:00:00.000Z", "cwd": "/home/dev/proj",
}


class TestCanParse:
    def test_pi_fixture_is_recognized(self):
        assert PiParser.can_parse(FIXTURE)

    def test_other_providers_are_rejected(self):
        fixtures = Path(__file__).parent / "fixtures"
        assert not PiParser.can_parse(fixtures / "claude_sample.jsonl")
        assert not PiParser.can_parse(fixtures / "codex_sample.jsonl")
        assert not PiParser.can_parse(fixtures / "qwen_sample.jsonl")

    def test_non_jsonl_rejected(self, tmp_path):
        f = tmp_path / "session.txt"
        f.write_text(json.dumps(HEADER))
        assert not PiParser.can_parse(f)

    def test_empty_file_rejected(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert not PiParser.can_parse(f)


class TestEntryTypes:
    def setup_method(self):
        self.parser = PiParser()
        self.entries = self.parser.parse_file(FIXTURE)

    def test_every_entry_ingested_once_in_file_order(self):
        ids = [e.entry_id for e in self.entries]
        assert len(ids) == len(set(ids)) == 18

    def test_session_header(self):
        header = self.entries[0]
        assert header.event_type == "session"
        assert header.session_id == "sess-pi-0001"
        assert header.cwd == "/home/dev/acme-web"
        assert header.version == 3

    def test_model_change_carries_provider_and_model(self):
        changes = [e for e in self.entries if e.event_type == "model_change"]
        assert len(changes) == 2
        assert changes[0].model == "gpt-5.5"
        assert changes[0].model_provider == "openai-codex"
        assert changes[1].model == "claude-acme-4"

    def test_thinking_level_change(self):
        levels = [e for e in self.entries if e.event_type == "thinking_level_change"]
        assert [e.thinking_level for e in levels] == ["medium"]

    def test_user_message_text_and_parent(self):
        user = next(e for e in self.entries if e.role == "user")
        assert user.text == "Add a health endpoint to the app and document it."
        assert user.parent_id == "t001"
        assert user.session_id == "sess-pi-0001"

    def test_assistant_content_blocks_split(self):
        a1 = next(e for e in self.entries if e.entry_id == "a001")
        assert a1.role == "assistant"
        assert a1.thinking == "Read the app entrypoint first."
        assert a1.text == "I will look at the app first."
        assert len(a1.tool_calls) == 1
        call = a1.tool_calls[0]
        assert (call.call_id, call.name) == ("call-read-1", "read")
        assert call.arguments["path"] == "/home/dev/acme-web/src/app.py"

    def test_assistant_multiple_tool_calls_kept_in_order(self):
        a2 = next(e for e in self.entries if e.entry_id == "a002")
        names = [c.name for c in a2.tool_calls]
        assert names == ["read", "bash", "edit", "write"]
        assert a2.tool_calls[1].arguments["command"] == "ls -la /home/dev/acme-web"

    def test_tool_result_fields(self):
        r3 = next(e for e in self.entries if e.entry_id == "r003")
        assert r3.role == "toolResult"
        assert r3.tool_call_id == "call-bash-1"
        assert r3.tool_name == "bash"
        assert r3.tool_is_error is True
        assert "exit 2" in r3.text

    def test_usage_mapped_from_assistant(self):
        a1 = next(e for e in self.entries if e.entry_id == "a001")
        assert a1.token_input == 1000
        assert a1.token_output == 120
        assert a1.token_cache_read == 400
        assert a1.token_cache_write == 20
        assert a1.token_reasoning == 30
        assert a1.token_total == 1120
        assert a1.cost_total == 0.0123
        assert a1.model == "gpt-5.5"
        assert a1.model_provider == "openai-codex"

    def test_compaction_entry(self):
        c = next(e for e in self.entries if e.event_type == "compaction")
        assert c.summary == "Added a health endpoint and a test."
        assert c.parent_id == "a004"

    def test_usage_entry(self):
        g = next(e for e in self.entries if e.event_type == "usage")
        assert g.summary == "cache_warm"
        assert g.token_cache_write == 500
        assert g.cost_total == 0.01

    def test_session_info_entry(self):
        info = next(e for e in self.entries if e.event_type == "session_info")
        assert info.summary == "Demo: health endpoint"

    def test_both_branches_ingested(self):
        """The abandoned continuation (a003) and the fork (u002/a004) both stay."""
        ids = {e.entry_id for e in self.entries}
        assert {"a003", "u002", "a004"} <= ids

    def test_context_stamped_on_every_entry(self):
        for entry in self.entries[1:]:
            assert entry.session_id == "sess-pi-0001"
            assert entry.cwd == "/home/dev/acme-web"

    def test_title_is_explicit_session_name_and_prompt_is_first_user(self):
        last = self.entries[-1]
        assert last.session_title == "Demo: health endpoint"
        assert last.initial_prompt == "Add a health endpoint to the app and document it."

    def test_leaf_id_is_the_last_entry(self):
        assert self.entries[-1].leaf_id == "m002"

    def test_model_context_from_last_model_change(self):
        # The m002 model_change updates the running context...
        assert self.entries[-1].model == "claude-acme-4"
        # ...while the assistant keeps its own model.
        a1 = next(e for e in self.entries if e.entry_id == "a001")
        assert a1.model == "gpt-5.5"


class TestUnknownEntries:
    def test_unknown_types_are_skipped(self, tmp_path):
        f = _write(
            tmp_path, HEADER,
            {"type": "label", "id": "l1", "parentId": None,
             "timestamp": "2026-05-01T10:00:01.000Z", "label": "bookmark"},
            {"type": "custom", "id": "c1", "parentId": "l1",
             "timestamp": "2026-05-01T10:00:02.000Z", "customType": "ext"},
            _entry(),
        )
        entries = PiParser().parse_file(f)
        assert [e.event_type for e in entries] == ["session", "message"]


class TestDedupe:
    def test_same_id_never_emitted_twice_on_reread(self, tmp_path):
        f = _write(tmp_path, HEADER, _entry())
        parser = PiParser()
        first, offset = parser.parse_incremental(f, 0)
        assert len(first) == 2
        again, offset2 = parser.parse_incremental(f, 0)
        assert again == []
        # The offset still advances to the file end.
        assert offset2 == offset == f.stat().st_size

    def test_duplicated_line_in_file_is_deduped(self, tmp_path):
        f = _write(tmp_path, HEADER, _entry(), _entry())
        entries = PiParser().parse_file(f)
        assert [e.entry_id for e in entries] == ["sess-1", "x1"]


class TestIncremental:
    def test_offset_reads_only_new_lines(self, tmp_path):
        f = _write(tmp_path, HEADER, _entry(text="first"))
        parser = PiParser()
        first, offset = parser.parse_incremental(f, 0)
        assert len(first) == 2

        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_entry(
                id="x2", parentId="x1",
                message={"role": "assistant",
                         "content": [{"type": "text", "text": "second"}]},
            )) + "\n")
        second, offset2 = parser.parse_incremental(f, offset)
        assert [e.text for e in second] == ["second"]
        assert offset2 > offset

    def test_partial_trailing_line_waits(self, tmp_path):
        f = _write(tmp_path, HEADER)
        parser = PiParser()
        _, offset = parser.parse_incremental(f, 0)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write('{"type": "message", "id": "x9"')
        entries, offset2 = parser.parse_incremental(f, offset)
        assert entries == []
        assert offset2 == offset

    def test_restart_seeds_context_from_header(self, tmp_path):
        f = _write(tmp_path, HEADER, _entry())
        # A different parser instance = a daemon restart with a stored offset.
        fresh = PiParser()
        entries, _ = fresh.parse_incremental(f, len(HEADER_JSON))
        assert [e.entry_id for e in entries] == ["x1"]
        assert entries[0].session_id == "sess-1"
        assert entries[0].cwd == "/home/dev/proj"


HEADER_JSON = json.dumps(HEADER) + "\n"


class TestLinearize:
    def setup_method(self):
        self.entries = PiParser().parse_file(FIXTURE)

    def test_active_branch_excludes_abandoned_sibling(self):
        branch = linearize(self.entries)
        ids = [e.entry_id for e in branch]
        assert "a003" not in ids  # the abandoned continuation
        assert {"u002", "a004", "c001", "g001", "i001", "m002"} <= set(ids)

    def test_branch_is_returned_in_file_order(self):
        branch = linearize(self.entries)
        # The fork starts at a002: the tool results that followed it belong to
        # the abandoned branch and are (correctly) not part of the active path.
        assert [e.entry_id for e in branch] == [
            "m001", "t001", "u001", "a001", "r001", "a002",
            "u002", "a004", "c001", "g001", "i001", "m002",
        ]

    def test_explicit_leaf(self):
        branch = linearize(self.entries, leaf_id="a003")
        assert branch[-1].entry_id == "a003"
        assert "u002" not in {e.entry_id for e in branch}

    def test_unknown_leaf_falls_back_to_tip(self):
        branch = linearize(self.entries, leaf_id="nope")
        assert branch[-1].entry_id == "m002"

    def test_handles_missing_parent_gracefully(self):
        a = PiEntry(event_type="message", entry_id="a", parent_id="ghost")
        b = PiEntry(event_type="message", entry_id="b", parent_id="a")
        assert [e.entry_id for e in linearize([a, b])] == ["a", "b"]
