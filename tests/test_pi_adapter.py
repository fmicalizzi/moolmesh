"""Tests for the Pi adapter (roles, paths, tokens, session metadata)."""

from pathlib import Path

from hub.adapters.pi_adapter import PiAdapter, extract_pi_paths
from hub.models.base import MessageRole, Provider
from hub.models.pi import PiEntry, PiToolCall
from hub.parsers.pi_parser import PiParser

FIXTURE = Path(__file__).parent / "fixtures" / "pi_sample.jsonl"
CWD = "/home/dev/acme-web"


def _entry(**overrides) -> PiEntry:
    base = dict(
        event_type="message", entry_id="e1", parent_id="p1",
        timestamp="2026-05-01T10:00:00.000Z", session_id="sess-1", cwd=CWD,
    )
    base.update(overrides)
    return PiEntry(**base)


class TestExtractPiPaths:
    def test_read_absolute_path(self):
        calls = [PiToolCall(name="read", arguments={"path": "/srv/app/main.py"})]
        assert extract_pi_paths(calls, CWD) == ["/srv/app/main.py"]

    def test_relative_path_resolved_against_cwd(self):
        calls = [PiToolCall(name="read", arguments={"path": "src/app.py"})]
        assert extract_pi_paths(calls, CWD) == [f"{CWD}/src/app.py"]

    def test_relative_dotdot_normalized(self):
        calls = [PiToolCall(name="write", arguments={"path": "../notes/x.md"})]
        assert extract_pi_paths(calls, CWD) == ["/home/dev/notes/x.md"]

    def test_edit_and_write_are_file_tools(self):
        calls = [
            PiToolCall(name="edit", arguments={"path": "a.py"}),
            PiToolCall(name="write", arguments={"path": "b.py"}),
        ]
        assert extract_pi_paths(calls, CWD) == [f"{CWD}/a.py", f"{CWD}/b.py"]

    def test_never_truncated(self):
        long_path = "/home/dev/" + "x" * 400 + "/file.md"
        calls = [PiToolCall(name="read", arguments={"path": long_path})]
        assert extract_pi_paths(calls, CWD) == [long_path]

    def test_shell_command_is_never_a_path(self):
        calls = [PiToolCall(name="bash", arguments={"command": "cat /etc/hosts"})]
        assert extract_pi_paths(calls, CWD) == []

    def test_directory_tools_are_skipped(self):
        calls = [
            PiToolCall(name="ls", arguments={"path": "src"}),
            PiToolCall(name="grep", arguments={"pattern": "x", "path": "src"}),
            PiToolCall(name="find", arguments={"pattern": "*.py", "path": "src"}),
        ]
        assert extract_pi_paths(calls, CWD) == []

    def test_directory_like_path_rejected(self):
        calls = [PiToolCall(name="read", arguments={"path": "src/"})]
        assert extract_pi_paths(calls, CWD) == []

    def test_dedupe_keeps_first_seen_order(self):
        calls = [
            PiToolCall(name="read", arguments={"path": "b.py"}),
            PiToolCall(name="read", arguments={"path": "a.py"}),
            PiToolCall(name="read", arguments={"path": "b.py"}),
        ]
        assert extract_pi_paths(calls, CWD) == [f"{CWD}/b.py", f"{CWD}/a.py"]

    def test_windows_style_path_normalized(self):
        calls = [PiToolCall(name="read", arguments={"path": "C:\\work\\app\\main.py"})]
        assert extract_pi_paths(calls, CWD) == ["C:\\work\\app\\main.py"]

    def test_no_cwd_drops_relative_path(self):
        calls = [PiToolCall(name="read", arguments={"path": "src/app.py"})]
        assert extract_pi_paths(calls, "") == []


class TestRoles:
    def setup_method(self):
        self.adapter = PiAdapter()

    def test_session_header_is_system(self):
        evt = self.adapter.to_event(_entry(event_type="session", entry_id="s1"), "proj")
        assert evt.event_type == MessageRole.SYSTEM.value
        assert "session start" in evt.summary

    def test_user_message(self):
        evt = self.adapter.to_event(_entry(role="user", text="do it"), "proj")
        assert evt.event_type == MessageRole.USER.value

    def test_empty_user_message_skipped(self):
        assert self.adapter.to_event(_entry(role="user", text="  "), "proj") is None

    def test_assistant_text_wins(self):
        evt = self.adapter.to_event(
            _entry(role="assistant", text="done",
                   tool_calls=[PiToolCall(name="read", arguments={"path": "a.py"})]),
            "proj",
        )
        assert evt.event_type == MessageRole.ASSISTANT.value
        # ...but the tool call still contributes tool_name + file_path.
        assert evt.tool_name == "read"
        assert evt.file_path == f"{CWD}/a.py"

    def test_assistant_tool_only_is_tool_use(self):
        evt = self.adapter.to_event(
            _entry(role="assistant",
                   tool_calls=[PiToolCall(name="bash", arguments={"command": "ls"})]),
            "proj",
        )
        assert evt.event_type == MessageRole.TOOL_USE.value
        assert evt.tool_name == "bash"
        assert evt.file_path is None

    def test_assistant_thinking_only(self):
        evt = self.adapter.to_event(_entry(role="assistant", thinking="hmm"), "proj")
        assert evt.event_type == MessageRole.THINKING.value

    def test_assistant_empty_is_skipped(self):
        assert self.adapter.to_event(_entry(role="assistant"), "proj") is None

    def test_tool_result(self):
        evt = self.adapter.to_event(
            _entry(role="toolResult", tool_name="read", text="contents"), "proj")
        assert evt.event_type == MessageRole.TOOL_RESULT.value
        assert evt.tool_name == "read"

    def test_tool_result_error_marked_in_summary(self):
        evt = self.adapter.to_event(
            _entry(role="toolResult", tool_name="bash", text="boom",
                   tool_is_error=True), "proj")
        assert evt.summary.startswith("[error]")

    def test_compaction_is_summary(self):
        evt = self.adapter.to_event(
            _entry(event_type="compaction", summary="long story"), "proj")
        assert evt.event_type == MessageRole.SUMMARY.value
        assert "compaction" in evt.summary

    def test_model_change_is_not_a_conversation_event(self):
        assert self.adapter.to_event(
            _entry(event_type="model_change", model="m", model_provider="p"), "proj"
        ) is None


class TestTokens:
    def setup_method(self):
        self.adapter = PiAdapter()

    def test_event_tokens_mirror_assistant_usage(self):
        evt = self.adapter.to_event(
            _entry(role="assistant", text="hi", token_input=1000, token_output=120,
                   token_cache_read=400, token_cache_write=20,
                   token_reasoning=30, token_total=1120, cost_total=0.0123),
            "proj",
        )
        assert evt.tokens == {
            "input": 1000, "output": 120, "cached_input": 400,
            "reasoning": 30, "cost": 0.0123,
        }

    def test_output_not_doubled_by_reasoning(self):
        """Pi's ``output`` already includes reasoning (documented in pi-ai)."""
        msg = self.adapter.to_unified(
            _entry(role="assistant", text="hi", token_input=10, token_output=50,
                   token_reasoning=20, token_total=60),
            "proj",
        )
        assert msg.tokens.output_tokens == 50

    def test_cache_write_maps_to_cache_creation(self):
        msg = self.adapter.to_unified(
            _entry(role="assistant", text="hi", token_input=10, token_output=5,
                   token_cache_write=7, token_total=15),
            "proj",
        )
        assert msg.tokens.cache_creation == 7

    def test_no_usage_no_tokens(self):
        evt = self.adapter.to_event(_entry(role="user", text="hi"), "proj")
        assert evt.tokens is None


class TestEventsExtras:
    def setup_method(self):
        self.adapter = PiAdapter()

    def test_one_extra_per_additional_directory(self):
        entry = _entry(
            role="assistant",
            tool_calls=[
                PiToolCall(name="read", arguments={"path": "README.md"}),
                PiToolCall(name="bash", arguments={"command": "ls"}),
                PiToolCall(name="edit", arguments={"path": "src/app.py"}),
                PiToolCall(name="write", arguments={"path": "docs/health.md"}),
            ],
        )
        events = self.adapter.to_events(entry, "proj")
        # One per distinct directory: acme-web (README), acme-web/src, docs.
        assert [e.file_path for e in events] == [
            f"{CWD}/README.md", f"{CWD}/src/app.py", f"{CWD}/docs/health.md",
        ]
        assert events[0].tool_name == "read"
        assert events[2].summary == f"read: {CWD}/docs/health.md"

    def test_shell_only_call_has_a_single_event_without_path(self):
        entry = _entry(role="assistant",
                       tool_calls=[PiToolCall(name="bash", arguments={"command": "ls"})])
        events = self.adapter.to_events(entry, "proj")
        assert len(events) == 1
        assert events[0].file_path is None


class TestUnifiedMessages:
    def setup_method(self):
        self.adapter = PiAdapter()

    def test_parent_id_kept_for_tree_linearization(self):
        msg = self.adapter.to_unified(_entry(role="user", text="hi"), "proj")
        assert msg.parent_id == "p1"
        assert msg.provider == Provider.PI
        assert msg.session_id == "sess-1"

    def test_tool_calls_classified(self):
        msg = self.adapter.to_unified(
            _entry(role="assistant", tool_calls=[
                PiToolCall(name="read", arguments={"path": "a.py"}, call_id="c1"),
                PiToolCall(name="edit", arguments={"path": "a.py"}, call_id="c2"),
                PiToolCall(name="bash", arguments={"command": "ls"}, call_id="c3"),
                PiToolCall(name="grep", arguments={"pattern": "x"}, call_id="c4"),
            ]),
            "proj",
        )
        assert [c.operation_type for c in msg.tool_calls] == [
            "read", "write", "exec", "search",
        ]
        assert [c.tool_id for c in msg.tool_calls] == ["c1", "c2", "c3", "c4"]


class TestSessionMeta:
    def setup_method(self):
        self.adapter = PiAdapter()

    def test_from_fixture_last_entry(self):
        entries = PiParser().parse_file(FIXTURE)
        meta = self.adapter.to_session_meta(entries[-1], "acme-web")
        assert meta.id == "sess-pi-0001"
        assert meta.provider == Provider.PI
        assert meta.cwd == "/home/dev/acme-web"
        assert meta.model == "claude-acme-4"  # last model_change
        assert meta.title == "Demo: health endpoint"
        assert meta.initial_prompt == "Add a health endpoint to the app and document it."
        assert meta.metadata["leaf_id"] == "m002"

    def test_model_from_assistant_when_no_model_change(self):
        meta = self.adapter.to_session_meta(
            _entry(role="assistant", text="x", model="m-1", model_provider="p-1"),
            "proj",
        )
        assert meta.model == "m-1"

    def test_forked_session_parent_recorded(self):
        meta = self.adapter.to_session_meta(
            _entry(event_type="session", session_id="s2", parent_session="s1"),
            "proj",
        )
        assert meta.metadata["parent_session_id"] == "s1"

    def test_no_session_id_yields_none(self):
        assert self.adapter.to_session_meta(_entry(session_id=""), "proj") is None
