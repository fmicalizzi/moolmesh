"""Tests for the Antigravity CLI adapter (steps -> events, ids, file paths, summaries)."""

from __future__ import annotations

import logging

import pytest

from hub.adapters.antigravity_adapter import AntigravityAdapter
from hub.cache.event_store import _compute_fingerprint, _normalize_event
from hub.models.antigravity import AntigravityEntry, AntigravityToolCall
from hub.models.base import SHELL_TOOLS, MessageRole, Provider
from hub.parsers.antigravity_parser import AntigravityParser
from tests.test_antigravity_parser import (
    CONV,
    FIXTURE,
    write_transcript,
)
from tests.test_provider_contract import VALID_EVENT_TYPES, _is_absolute, _parseable_timestamp

TS = "2026-05-01T10:00:05Z"
PROJECT = "proj"


def entry(step_type="PLANNER_RESPONSE", index=1, **kw) -> AntigravityEntry:
    kw.setdefault("session_id", CONV)
    kw.setdefault("timestamp", TS)
    return AntigravityEntry(step_index=index, step_type=step_type, **kw)


def call(name: str, **args) -> AntigravityToolCall:
    return AntigravityToolCall(name=name, args=args)


def events_of(e: AntigravityEntry):
    return AntigravityAdapter().to_events(e, PROJECT)


def fingerprint(event) -> str:
    """The store's own fingerprint of an event, computed the way the store does."""
    return _compute_fingerprint(_normalize_event(event.to_dict()))


@pytest.fixture
def parsed(tmp_path):
    path = write_transcript(tmp_path, FIXTURE.read_text().splitlines())
    return {e.step_index: e for e in AntigravityParser().parse_file(path)}


# ── role mapping ─────────────────────────────────────────────────


class TestMapping:
    def test_user_input(self):
        (ev,) = events_of(entry("USER_INPUT", content="Add a helper.\nSmall one."))
        assert ev.event_type == "user"
        assert ev.summary == "Add a helper. Small one."
        assert ev.full_text == "Add a helper.\nSmall one."
        assert ev.provider is Provider.ANTIGRAVITY and ev.session_id == CONV

    def test_planner_response_is_thinking_assistant_and_one_tool_use_per_call(self):
        evs = events_of(entry(
            thinking="plan it", content="Doing it now.",
            tool_calls=[call("list_dir", DirectoryPath="/Users/u/work/proj"),
                        call("grep_search", Query="todo", SearchPath="/Users/u/work/proj")],
        ))
        assert [e.event_type for e in evs] == ["thinking", "assistant", "tool_use", "tool_use"]
        assert evs[0].summary == "[thinking] plan it" and evs[0].full_text == "plan it"
        assert evs[1].summary == "Doing it now."

    def test_to_event_is_the_first_event_that_is_not_thinking(self):
        e = entry(thinking="hmm", tool_calls=[call("list_dir", DirectoryPath="/Users/u/x")])
        assert AntigravityAdapter().to_event(e, PROJECT).event_type == "tool_use"
        only_thinking = entry(thinking="hmm")
        assert AntigravityAdapter().to_event(only_thinking, PROJECT).event_type == "thinking"

    @pytest.mark.parametrize("step_type,label", [
        ("GENERIC", "result"), ("RUN_COMMAND", "run_command"), ("VIEW_FILE", "view_file"),
        ("LIST_DIRECTORY", "list_dir"), ("FIND", "find"), ("CODE_ACTION", "code_action"),
        ("INVOKE_SUBAGENT", "invoke_subagent"), ("ASK_QUESTION", "ask_question"),
    ])
    def test_result_steps_are_tool_results_without_tool_name_or_path(self, step_type, label):
        (ev,) = events_of(entry(step_type, source="MODEL", content="output here"))
        assert ev.event_type == "tool_result"
        assert ev.summary == f"[{label}] output here"
        # the SQL tool counters treat a non-null tool_name as a call
        assert ev.tool_name is None and ev.file_path is None

    def test_failed_result_is_marked(self):
        (ev,) = events_of(entry("RUN_COMMAND", status="ERROR", content="exit 1"))
        assert ev.summary == "[error] exit 1"
        (ev,) = events_of(entry("GENERIC", error="boom", content=""))
        assert ev.summary == "[error] boom"

    def test_running_result_is_kept(self):
        (ev,) = events_of(entry("GENERIC", status="RUNNING", content="still running..."))
        assert ev.event_type == "tool_result" and "still running" in ev.summary

    def test_system_and_error_messages(self):
        (ev,) = events_of(entry("SYSTEM_MESSAGE", source="SYSTEM", content="Stay in the workspace."))
        assert (ev.event_type, ev.summary) == ("system", "Stay in the workspace.")
        (ev,) = events_of(entry("ERROR_MESSAGE", source="SYSTEM", content="long text", error="no such file"))
        assert (ev.event_type, ev.summary) == ("system", "[error] no such file")

    @pytest.mark.parametrize("step_type,label", [("CHECKPOINT", "checkpoint"),
                                                 ("CONVERSATION_HISTORY", "history")])
    def test_context_bookkeeping_is_a_summary(self, step_type, label):
        (ev,) = events_of(entry(step_type, source="SYSTEM", content="earlier work"))
        assert (ev.event_type, ev.summary) == ("summary", f"[{label}] earlier work")

    def test_ephemeral_messages_are_skipped(self):
        assert events_of(entry("EPHEMERAL_MESSAGE", source="SYSTEM", content="reminder")) == []

    def test_unknown_type_is_skipped_counted_and_logged_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="hub.AntigravityAdapter")
        adapter = AntigravityAdapter()
        for _ in range(3):
            assert adapter.to_events(entry("FUTURE_STEP_KIND", content="x"), PROJECT) == []
        assert adapter.unknown_types["FUTURE_STEP_KIND"] == 3
        assert sum("desconocido" in r.getMessage() for r in caplog.records) == 1

    def test_unknown_type_still_yields_its_tool_calls(self):
        adapter = AntigravityAdapter()
        evs = adapter.to_events(
            entry("FUTURE_STEP_KIND", tool_calls=[call("view_file", AbsolutePath="/Users/u/a.py")]),
            PROJECT)
        assert [e.event_type for e in evs] == ["tool_use"]

    def test_tool_calls_on_any_step_type_become_tool_use(self):
        # the real format keeps calls on PLANNER_RESPONSE; a format that moves them keeps working
        evs = events_of(entry("GENERIC", content="", tool_calls=[
            call("view_file", AbsolutePath="/Users/u/work/proj/a.py")]))
        assert [e.event_type for e in evs] == ["tool_use"]
        assert evs[0].file_path == "/Users/u/work/proj/a.py"

    @pytest.mark.parametrize("kind", [
        "USER_INPUT", "PLANNER_RESPONSE", "SYSTEM_MESSAGE", "GENERIC", "RUN_COMMAND",
        "CHECKPOINT", "ERROR_MESSAGE",
    ])
    def test_empty_steps_yield_nothing(self, kind):
        assert events_of(entry(kind, content="   ")) == []

    def test_tokens_model_and_cost_are_left_empty(self, parsed):
        # step 6 carries input_tokens/output_tokens/cache_read_tokens: not read in v1
        evs = events_of(parsed[6])
        assert evs and all(e.tokens is None and e.model is None for e in evs)
        assert all("tokens" not in e.to_dict() for e in evs)


# ── tool calls: summaries, fingerprints, collapse ────────────────


class TestToolCalls:
    def test_two_different_calls_of_one_step_are_two_distinct_events(self, parsed):
        evs = [e for e in events_of(parsed[3]) if e.event_type == "tool_use"]
        assert [e.tool_name for e in evs] == ["view_file", "write_to_file"]
        assert evs[0].summary == "view_file: /Users/u/work/proj/src/app.py L1-40"
        assert evs[1].summary == "write_to_file: /Users/u/work/proj/src/greeting.py"
        assert evs[0].timestamp == evs[1].timestamp  # they share created_at
        assert len({fingerprint(e) for e in evs}) == 2  # the store keeps both

    def test_calls_differing_only_by_line_range_do_not_collapse(self):
        evs = events_of(entry(tool_calls=[
            call("view_file", AbsolutePath="/Users/u/work/proj/a.py", StartLine=1, EndLine=50),
            call("view_file", AbsolutePath="/Users/u/work/proj/a.py", StartLine=51, EndLine=100),
        ]))
        assert len({fingerprint(e) for e in evs}) == 2

    def test_commands_and_queries_distinguish_calls(self):
        evs = events_of(entry(tool_calls=[
            call("run_command", CommandLine="ls src", Cwd="/Users/u/work/proj"),
            call("run_command", CommandLine="ls tests", Cwd="/Users/u/work/proj"),
            call("grep_search", Query="alpha", SearchPath="/Users/u/work/proj"),
            call("grep_search", Query="beta", SearchPath="/Users/u/work/proj"),
            call("find_by_name", Pattern="*.py", SearchDirectory="/Users/u/work/proj"),
        ]))
        assert len({fingerprint(e) for e in evs}) == 5
        assert evs[0].summary == "run_command: ls src"
        assert evs[2].summary == "grep_search: alpha in /Users/u/work/proj"

    def test_identical_calls_collapse_as_documented(self):
        # same tool + same args in one step: indistinguishable to the fingerprint (as for Pi)
        evs = events_of(entry(tool_calls=[
            call("view_file", AbsolutePath="/Users/u/work/proj/a.py"),
            call("view_file", AbsolutePath="/Users/u/work/proj/a.py"),
        ]))
        assert len(evs) == 2 and len({fingerprint(e) for e in evs}) == 1

    def test_other_tools_summarize_their_args_without_the_prose_keys(self):
        (ev,) = events_of(entry(tool_calls=[call(
            "send_message", Recipient="agent-1", Message="hello", toolAction="x", toolSummary="y")]))
        assert ev.summary == 'send_message: {"Message": "hello", "Recipient": "agent-1"}'
        (ev,) = events_of(entry(tool_calls=[call("list_permissions", toolAction="x")]))
        assert ev.summary == "list_permissions"

    def test_long_paths_keep_their_distinguishing_tail(self):
        base = "/Users/u/work/proj/" + "deep/" * 22
        a, b = base + "alpha.py", base + "beta.py"
        evs = events_of(entry(tool_calls=[call("view_file", AbsolutePath=a),
                                          call("view_file", AbsolutePath=b)]))
        assert len({fingerprint(e) for e in evs}) == 2


# ── file_path ────────────────────────────────────────────────────


class TestFilePath:
    @pytest.mark.parametrize("tool,key,op", [
        ("view_file", "AbsolutePath", "read"),
        ("write_to_file", "TargetFile", "create"),
        ("replace_file_content", "TargetFile", "modify"),
        ("multi_replace_file_content", "TargetFile", "modify"),
    ])
    def test_file_tools_name_an_absolute_path(self, tool, key, op):
        e = entry(tool_calls=[call(tool, **{key: "/Users/u/work/proj/src/../src/app.py"})])
        (ev,) = events_of(e)
        assert ev.file_path == "/Users/u/work/proj/src/app.py"  # normalized, never truncated
        msg = AntigravityAdapter().to_unified(e, PROJECT)
        assert msg.tool_calls[0].file_path == "/Users/u/work/proj/src/app.py"
        assert msg.tool_calls[0].operation_type == op

    def test_overwriting_write_is_a_modify(self):
        e = entry(tool_calls=[call("write_to_file", TargetFile="/Users/u/a.py", Overwrite=True)])
        assert AntigravityAdapter().to_unified(e, PROJECT).tool_calls[0].operation_type == "modify"

    def test_run_command_is_never_a_path(self):
        assert "run_command" in SHELL_TOOLS
        e = entry(tool_calls=[call(
            "run_command", CommandLine="/Users/u/work/proj/bin/tool --flag", Cwd="/Users/u/work/proj",
            AbsolutePath="/Users/u/work/proj/x.py", TargetFile="/Users/u/work/proj/y.py")])
        (ev,) = events_of(e)
        assert ev.file_path is None and ev.tool_name == "run_command"
        msg = AntigravityAdapter().to_unified(e, PROJECT)
        assert msg.tool_calls[0].file_path is None and msg.tool_calls[0].operation_type == "exec"

    @pytest.mark.parametrize("value", [
        "relative/app.py",                        # not absolute
        '"/Users/u/work/pro',                     # a value the writer cut mid-string: never a half path
        "/Users/u/work/proj/",                    # a directory
        "",                                       # empty
        "/Users/u/work/${VAR}/a.py",              # a template
        "/Users/u/work/pro\nj/a.py",              # control character
        None, 5, ["/Users/u/a.py"],               # not a string
    ])
    def test_values_that_are_not_a_literal_absolute_file_are_dropped(self, value):
        (ev,) = events_of(entry(tool_calls=[call("view_file", AbsolutePath=value)]))
        assert ev.file_path is None

    def test_windows_paths(self):
        (ev,) = events_of(entry(tool_calls=[call("view_file", AbsolutePath="C:\\Users\\u\\proj\\a.py")]))
        assert ev.file_path == "C:\\Users\\u\\proj\\a.py"

    @pytest.mark.parametrize("tool,args", [
        ("list_dir", {"DirectoryPath": "/Users/u/work/proj"}),
        ("grep_search", {"SearchPath": "/Users/u/work/proj", "Query": "x"}),
        ("find_by_name", {"SearchDirectory": "/Users/u/work/proj", "Pattern": "*.py"}),
    ])
    def test_directory_tools_never_set_a_path(self, tool, args):
        (ev,) = events_of(entry(tool_calls=[AntigravityToolCall(name=tool, args=args)]))
        assert ev.file_path is None
        assert AntigravityAdapter().to_unified(
            entry(tool_calls=[AntigravityToolCall(name=tool, args=args)]), PROJECT
        ).tool_calls[0].operation_type == "search"

    def test_a_truncated_call_from_a_real_parse_yields_no_path(self, tmp_path):
        # tool_calls listed in truncated_fields can carry a cut value: not a path
        line = {"step_index": 0, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
                "created_at": TS, "truncated_fields": ["tool_calls"],
                "tool_calls": [{"name": "write_to_file",
                                "args": {"TargetFile": '"/Users/u/work/proj/src/gre'}}]}
        (parsed,) = AntigravityParser().parse_file(write_transcript(tmp_path, [line]))
        (ev,) = events_of(parsed)
        assert ev.file_path is None and ev.tool_name == "write_to_file"


# ── ids and unified message ──────────────────────────────────────


class TestIds:
    def test_message_and_tool_ids_are_uuid_step_index(self, parsed):
        msg = AntigravityAdapter().to_unified(parsed[3], PROJECT)
        assert msg.id == f"{CONV}:3"
        assert [c.tool_id for c in msg.tool_calls] == [f"{CONV}:3:0", f"{CONV}:3:1"]
        assert [c.name for c in msg.tool_calls] == ["view_file", "write_to_file"]
        assert msg.role is MessageRole.TOOL_USE
        assert msg.provider is Provider.ANTIGRAVITY and msg.session_id == CONV
        assert msg.model is None and msg.tokens is None
        assert msg.timestamp.isoformat() == "2026-05-01T10:00:05+00:00"

    def test_a_text_step_carries_its_text(self, parsed):
        msg = AntigravityAdapter().to_unified(parsed[6], PROJECT)
        assert msg.id == f"{CONV}:6" and msg.role is MessageRole.ASSISTANT
        assert msg.text.startswith("Added the greeting helper")

    def test_skipped_steps_have_no_message(self, parsed):
        assert AntigravityAdapter().to_unified(parsed[11], PROJECT) is None

    def test_subagent_message_is_a_sidechain(self):
        e = entry("USER_INPUT", content="do it", parent_session="p" * 8)
        assert AntigravityAdapter().to_unified(e, PROJECT).is_sidechain is True


# ── bounded text (#52) ───────────────────────────────────────────


class TestBoundedText:
    def test_full_text_is_capped_for_content_thinking_and_results(self):
        big = "x" * 9000
        for e, role in [
            (entry("USER_INPUT", content=big), "user"),
            (entry(content=big), "assistant"),
            (entry(thinking=big), "thinking"),
            (entry("RUN_COMMAND", content=big), "tool_result"),
            (entry("CHECKPOINT", content=big), "summary"),
        ]:
            ev = next(x for x in events_of(e) if x.event_type == role)
            assert len(ev.full_text) == 2000
            assert len(ev.summary) <= 120

    def test_tool_use_has_no_full_text(self):
        (ev,) = events_of(entry(tool_calls=[call("view_file", AbsolutePath="/Users/u/a.py")]))
        assert ev.full_text is None


# ── session metadata ─────────────────────────────────────────────


class TestSessionMeta:
    def test_from_the_transcript_alone(self, parsed):
        meta = AntigravityAdapter().to_session_meta(parsed[14], "proj")
        assert meta.id == CONV and meta.provider is Provider.ANTIGRAVITY
        assert meta.cwd == "/Users/u/work/proj"
        assert meta.title == "Please add a greeting helper to the widget service."
        assert meta.initial_prompt.startswith("Please add")
        assert meta.model == "" and meta.git_branch == "" and meta.cost == 0.0
        assert meta.is_sidechain is False and meta.metadata == {}

    def test_summaries_title_wins_then_prompt_then_preview(self):
        base = dict(session_id=CONV, cwd="/Users/u/work/proj")
        a = AntigravityAdapter()
        e = entry(summary_title="Index title", session_title="Prompt title", **base)
        assert a.to_session_meta(e, "p").title == "Index title"
        e = entry(session_title="Prompt title", summary_preview="Preview line", **base)
        assert a.to_session_meta(e, "p").title == "Prompt title"
        e = entry(summary_preview="Preview line\nsecond", **base)
        assert a.to_session_meta(e, "p").title == "Preview line"
        assert len(a.to_session_meta(entry(summary_title="t" * 500, **base), "p").title) == 120

    def test_subagent_links_to_its_parent(self):
        parent = "11111111-1111-4111-8111-111111111111"
        e = entry(session_id="22222222-2222-4222-8222-222222222222", parent_session=parent,
                  nesting_depth=2, summary_last_modified="2026-05-01 10:00:20+00:00")
        meta = AntigravityAdapter().to_session_meta(e, "proj")
        assert meta.is_sidechain is True
        assert meta.metadata == {"parent_session_id": parent, "nesting_depth": 2,
                                 "summary_last_modified": "2026-05-01 10:00:20+00:00"}
        assert meta.to_dict()["is_sidechain"] is True

    def test_no_session_id_no_meta(self):
        assert AntigravityAdapter().to_session_meta(entry(session_id=""), "proj") is None


# ── the provider contract, for every event of every step ─────────


def test_every_event_of_the_fixture_meets_the_provider_contract(parsed):
    adapter = AntigravityAdapter()
    total = 0
    for e in parsed.values():
        for ev in adapter.to_events(e, PROJECT):
            total += 1
            assert ev.provider is Provider.ANTIGRAVITY and ev.session_id
            assert ev.event_type in VALID_EVENT_TYPES
            assert _parseable_timestamp(ev.timestamp)
            if ev.file_path:
                assert _is_absolute(ev.file_path)
                assert ev.tool_name not in SHELL_TOOLS
    assert total == 14
