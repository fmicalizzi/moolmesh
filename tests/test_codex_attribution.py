"""Codex touched-path extraction → events.file_path (issue #40, part A).

Fixtures mirror the real rollout formats (anonymized): ``custom_tool_call``
``exec`` (JavaScript driving ``tools.exec_command`` / ``tools.apply_patch``),
raw ``custom_tool_call`` ``apply_patch``, and the older ``function_call``
``exec_command`` with JSON arguments.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub.adapters.codex_adapter import CodexAdapter, extract_codex_paths
from hub.cache.event_store import EventStore
from hub.models.codex import CodexEntry, CodexFunctionCall
from hub.parsers.codex_parser import CodexParser
from hub.watchers.codex_watcher import CodexWatcher

SESSION_CWD = "/Users/dev/clients/acme/site"


def _js(s: str) -> str:
    """Write JS source the way Codex stores it: newlines escaped as ``\\n``."""
    return s.replace("\n", "\\n")


# --- extract_codex_paths ------------------------------------------------------


class TestExtractCodexPaths:
    def test_exec_js_apply_patch_update_file(self):
        js = (
            'const patch = "' + _js(
                "*** Begin Patch\n*** Update File: /abs/docs/a.md\n@@\n-old\n+new\n*** End Patch\n"
            ) + '";\nconst r = await tools.apply_patch(patch);\ntext(r.output)\n'
        )
        paths, workdir = extract_codex_paths("exec", js, SESSION_CWD)
        assert paths == ["/abs/docs/a.md"]
        assert workdir is None

    def test_raw_apply_patch_relative_path_resolves_against_session_cwd(self):
        patch = "*** Begin Patch\n*** Add File: docs/README.md\n+hello\n*** End Patch\n"
        paths, _ = extract_codex_paths("apply_patch", patch, SESSION_CWD)
        assert paths == [f"{SESSION_CWD}/docs/README.md"]

    def test_relative_path_prefers_call_workdir_over_session_cwd(self):
        js = (
            'await tools.exec_command({cmd:"apply_patch <<\'EOF\'\\n*** Begin Patch\\n'
            '*** Add File: notes/x.md\\n+x\\n*** End Patch\\nEOF",workdir:"/w/other"});'
        )
        paths, workdir = extract_codex_paths("exec", js, SESSION_CWD)
        assert workdir == "/w/other"
        assert paths == ["/w/other/notes/x.md"]

    def test_relative_path_without_any_base_is_dropped(self):
        patch = "*** Begin Patch\n*** Add File: README.md\n+x\n*** End Patch\n"
        assert extract_codex_paths("apply_patch", patch, "") == ([], None)

    def test_all_patch_markers_in_order_without_duplicates(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: /r/a/one.md\n@@\n-x\n+y\n"
            "*** Delete File: /r/b/two.md\n"
            "*** Update File: /r/a/old.md\n*** Move to: /r/c/new.md\n@@\n-x\n+y\n"
            "*** Update File: /r/a/one.md\n@@\n-y\n+z\n"
            "*** End Patch\n"
        )
        paths, _ = extract_codex_paths("apply_patch", patch)
        assert paths == ["/r/a/one.md", "/r/b/two.md", "/r/a/old.md", "/r/c/new.md"]

    def test_marker_text_inside_patch_body_is_not_a_path(self):
        patch = (
            "*** Begin Patch\n*** Add File: /r/doc.md\n"
            "+Example: *** Update File: /not/touched.md\n*** End Patch\n"
        )
        assert extract_codex_paths("apply_patch", patch)[0] == ["/r/doc.md"]

    def test_exec_command_function_call_workdir_is_cwd_not_file(self):
        args = json.dumps({"cmd": "pwd && rg --files", "workdir": "/w/proj",
                           "yield_time_ms": 1000})
        assert extract_codex_paths("exec_command", args, SESSION_CWD) == ([], "/w/proj")

    def test_exec_js_exec_command_workdir(self):
        js = ('const r = await tools.exec_command({cmd:"sed -n \'1,260p\' docs/g.md",'
              'workdir:"/w/proj",max_output_tokens:18000}); text(r.output)\n')
        assert extract_codex_paths("exec", js) == ([], "/w/proj")

    def test_long_path_is_not_truncated(self):
        long_path = "/Users/dev/" + "/".join(["a-very-long-directory-name"] * 6) + "/file.md"
        assert len(long_path) > 80
        patch = f"*** Begin Patch\n*** Add File: {long_path}\n+x\n*** End Patch\n"
        assert extract_codex_paths("apply_patch", patch)[0] == [long_path]

    def test_dynamic_js_without_markers_yields_nothing(self):
        js = ('const base = "/r"; const files = {a: base + "/a.md"};\n'
              'for (const k in files) { await tools.exec_command({cmd: "touch " + files[k]}); }')
        assert extract_codex_paths("exec", js, SESSION_CWD) == ([], None)

    @pytest.mark.parametrize("js", [
        'await tools.apply_patch("*** Begin Patch\\n*** Add File: " + base + "/a.md\\n+x\\n*** End Patch")',
        'await tools.apply_patch("*** Begin Patch\\n*** Add File: /r/" + name + "\\n+x\\n*** End Patch")',
        'await tools.apply_patch(`*** Begin Patch\\n*** Add File: ${dir}/a.md\\n+x\\n*** End Patch`)',
        "await tools.apply_patch('*** Begin Patch\\n*** Add File: ' + p + '\\n*** End Patch')",
    ])
    def test_runtime_built_marker_paths_are_rejected(self, js):
        assert extract_codex_paths("exec", js, SESSION_CWD) == ([], None)

    @pytest.mark.parametrize("name,raw", [
        ("exec", 'garbage ((( "unterminated'),
        ("exec", ""),
        ("exec_command", "not json"),
        ("exec_command", "[1, 2]"),
        ("apply_patch", ""),
        ("shell", json.dumps({"command": ["bash", "-lc", "ls"]})),
        ("exec", 'tools.apply_patch("\\uD83D")'),  # lone surrogate escape
    ])
    def test_malformed_input_never_raises(self, name, raw):
        paths, workdir = extract_codex_paths(name, raw, SESSION_CWD)
        assert paths == [] and workdir is None

    def test_js_unicode_escapes_decode(self):
        js = ('await tools.apply_patch("*** Begin Patch\\n*** Add File: '
              '/r/presentaci\\u00f3n/a\\u00f1o.md\\n+x\\n*** End Patch")')
        assert extract_codex_paths("exec", js)[0] == ["/r/presentación/año.md"]

    def test_view_image_path(self):
        args = json.dumps({"path": "/r/docs/shot.png"})
        assert extract_codex_paths("view_image", args)[0] == ["/r/docs/shot.png"]
        js = 'await tools.view_image({path:"/r/docs/shot.png"})'
        assert extract_codex_paths("exec", js)[0] == ["/r/docs/shot.png"]

    def test_path_argument_ignored_for_unknown_tools(self):
        # ``path`` may be a directory for other tools — not a touched file.
        args = json.dumps({"path": "/r/docs"})
        assert extract_codex_paths("list_dir", args)[0] == []

    def test_normalizes_dot_segments(self):
        patch = "*** Begin Patch\n*** Add File: /r/a/../b/./c.md\n+x\n*** End Patch\n"
        assert extract_codex_paths("apply_patch", patch)[0] == ["/r/b/c.md"]


class TestExtractCodexPathsWindows:
    """Windows rollouts: drive-letter paths with backslashes, host-independent."""

    def test_js_escaped_backslashes_in_patch(self):
        # JS source for "C:\Users\nico\proj\a.md" — note the "\n" of "\nico".
        js = ('const p = "*** Begin Patch\\n*** Update File: C:\\\\Users\\\\nico'
              '\\\\proj\\\\a.md\\n@@\\n-x\\n+y\\n*** End Patch"; await tools.apply_patch(p);')
        assert extract_codex_paths("exec", js)[0] == [r"C:\Users\nico\proj\a.md"]

    def test_raw_patch_backslash_path(self):
        patch = "*** Begin Patch\n*** Add File: C:\\Users\\nico\\proj\\b.md\n+x\n*** End Patch\n"
        assert extract_codex_paths("apply_patch", patch)[0] == [r"C:\Users\nico\proj\b.md"]

    def test_relative_path_joins_windows_workdir(self):
        js = ('await tools.exec_command({cmd:"apply_patch <<EOF\\n*** Begin Patch\\n'
              '*** Add File: docs/x.md\\n+x\\n*** End Patch\\nEOF",'
              'workdir:"C:\\\\work\\\\proj"})')
        paths, workdir = extract_codex_paths("exec", js)
        assert workdir == r"C:\work\proj"
        assert paths == [r"C:\work\proj\docs\x.md"]

    def test_forward_slash_drive_path_normalizes(self):
        patch = "*** Begin Patch\n*** Add File: C:/work/proj/./y.md\n+x\n*** End Patch\n"
        assert extract_codex_paths("apply_patch", patch)[0] == [r"C:\work\proj\y.md"]

    def test_function_call_windows_workdir(self):
        args = json.dumps({"cmd": "dir", "workdir": "C:\\work\\proj"})
        assert extract_codex_paths("exec_command", args) == ([], r"C:\work\proj")


# --- Parser -------------------------------------------------------------------


def _meta_line(session_id="sess-1", cwd=SESSION_CWD) -> dict:
    return {"type": "session_meta", "timestamp": "2026-09-21T10:00:00Z",
            "payload": {"id": session_id, "cwd": cwd, "cli_version": "0.130.0",
                        "model_provider": "openai", "source": "vscode"}}


def _custom_call(name: str, raw_input: str, ts="2026-09-21T10:00:05Z", call_id="call_1") -> dict:
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "custom_tool_call", "status": "completed",
                        "call_id": call_id, "name": name, "input": raw_input}}


def _custom_output(call_id="call_1", ts="2026-09-21T10:00:06Z") -> dict:
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                        "output": [{"type": "input_text", "text": "Script completed\n"},
                                   {"type": "input_text", "text": "Success."}]}}


class TestCodexParserCustomToolCall:
    def test_custom_tool_call_produces_entry(self):
        raw_input = "*** Begin Patch\n*** Add File: README.md\n+x\n*** End Patch\n"
        entry = CodexParser()._parse_line(_custom_call("apply_patch", raw_input))
        assert entry is not None
        assert entry.payload_type == "custom_tool_call"
        assert entry.function_call.name == "apply_patch"
        assert entry.function_call.call_id == "call_1"
        assert entry.function_call.arguments == raw_input

    def test_custom_tool_call_output_pairs_by_call_id(self):
        entry = CodexParser()._parse_line(_custom_output())
        assert entry is not None
        assert entry.payload_type == "custom_tool_call_output"
        assert entry.function_output.call_id == "call_1"
        assert "Success." in entry.function_output.output

    def test_function_call_unchanged(self):
        entry = CodexParser()._parse_line({
            "type": "response_item", "timestamp": "t",
            "payload": {"type": "function_call", "name": "exec_command", "call_id": "c",
                        "arguments": '{"cmd":"ls","workdir":"/w"}'}})
        assert entry.payload_type == "function_call"
        assert entry.function_call.arguments == '{"cmd":"ls","workdir":"/w"}'


# --- Adapter ------------------------------------------------------------------


def _entry(name: str, raw_input: str, payload_type="custom_tool_call",
           cwd=SESSION_CWD) -> CodexEntry:
    return CodexEntry(
        event_type="response_item", timestamp="2026-09-21T10:00:05Z",
        payload_type=payload_type,
        function_call=CodexFunctionCall(call_id="c1", name=name, arguments=raw_input),
        session_id="sess-1", cwd=cwd,
    )


class TestCodexAdapterPaths:
    def setup_method(self):
        self.adapter = CodexAdapter()

    def test_custom_call_maps_to_tool_use_with_absolute_file_path(self):
        js = 'await tools.apply_patch("*** Begin Patch\\n*** Update File: /abs/a.md\\n*** End Patch")'
        evt = self.adapter.to_event(_entry("exec", js), "proj")
        assert evt.event_type == "tool_use"
        assert evt.file_path == "/abs/a.md"
        assert evt.tool_name == "apply_patch"  # the single inner tool
        assert evt.cwd == SESSION_CWD

    def test_exec_with_several_inner_tools_keeps_exec_name(self):
        js = ('await tools.exec_command({cmd:"ls",workdir:"/w"}); '
              'await tools.view_image({path:"/w/a.png"})')
        evt = self.adapter.to_event(_entry("exec", js), "proj")
        assert evt.tool_name == "exec"

    def test_exec_command_workdir_becomes_event_cwd(self):
        args = json.dumps({"cmd": "pwd", "workdir": "/w/proj"})
        evt = self.adapter.to_event(_entry("exec_command", args, "function_call"), "proj")
        assert evt.cwd == "/w/proj"
        assert evt.file_path is None  # a workdir is a directory, never a file_path
        assert evt.tool_name == "exec_command"

    def test_relative_workdir_falls_back_to_session_cwd(self):
        args = json.dumps({"cmd": "pwd", "workdir": "sub"})
        evt = self.adapter.to_event(_entry("exec_command", args, "function_call"), "proj")
        assert evt.cwd == SESSION_CWD

    def test_long_path_stored_untruncated(self):
        long_path = "/Users/dev/" + "x" * 120 + "/file.md"
        patch = f"*** Begin Patch\n*** Add File: {long_path}\n+x\n*** End Patch\n"
        evt = self.adapter.to_event(_entry("apply_patch", patch), "proj")
        assert evt.file_path == long_path

    def test_legacy_command_brief_kept_when_no_path(self):
        args = json.dumps({"command": "git diff"})
        evt = self.adapter.to_event(_entry("shell", args, "function_call"), "proj")
        assert evt.file_path == "git diff"

    def test_real_path_replaces_command_brief(self):
        args = json.dumps({"command": "apply_patch <<EOF\n*** Begin Patch\n"
                                      "*** Add File: /r/x.md\n+x\n*** End Patch\nEOF"})
        evt = self.adapter.to_event(_entry("shell", args, "function_call"), "proj")
        assert evt.file_path == "/r/x.md"

    def test_list_command_does_not_leak_into_file_path(self):
        args = json.dumps({"command": ["bash", "-lc", "ls"]})
        evt = self.adapter.to_event(_entry("shell", args, "function_call"), "proj")
        assert evt.file_path is None

    def test_dynamic_js_has_no_file_path(self):
        js = 'const b="/r"; await tools.exec_command({cmd:"touch "+b+"/a"});'
        evt = self.adapter.to_event(_entry("exec", js), "proj")
        assert evt.file_path is None
        assert evt.event_type == "tool_use"

    def test_custom_output_maps_to_tool_result(self):
        entry = CodexParser()._parse_line(_custom_output())
        evt = self.adapter.to_event(entry, "proj")
        assert evt.event_type == "tool_result"
        assert "Success." in evt.full_text


class TestCodexAdapterMultiDirectory:
    def setup_method(self):
        self.adapter = CodexAdapter()

    def _patch(self, *paths: str) -> CodexEntry:
        body = "".join(f"*** Update File: {p}\n@@\n-x\n+y\n" for p in paths)
        return _entry("apply_patch", f"*** Begin Patch\n{body}*** End Patch\n")

    def test_two_directories_two_events(self):
        events = self.adapter.to_events(self._patch("/r/a/1.md", "/r/b/2.md"), "proj")
        assert [e.file_path for e in events] == ["/r/a/1.md", "/r/b/2.md"]
        assert events[1].event_type == "tool_use"
        assert events[1].summary == "apply_patch: /r/b/2.md"
        assert events[1].tokens is None and events[1].full_text is None

    def test_same_directory_one_event(self):
        events = self.adapter.to_events(self._patch("/r/a/1.md", "/r/a/2.md", "/r/a/3.md"), "proj")
        assert len(events) == 1
        assert events[0].file_path == "/r/a/1.md"

    def test_one_event_per_directory_not_per_file(self):
        events = self.adapter.to_events(
            self._patch("/r/a/1.md", "/r/b/1.md", "/r/a/2.md", "/r/b/2.md", "/r/c/1.md"), "proj")
        assert [e.file_path for e in events] == ["/r/a/1.md", "/r/b/1.md", "/r/c/1.md"]

    def test_primary_event_identical_to_to_event(self):
        entry = self._patch("/r/a/1.md", "/r/b/2.md")
        assert self.adapter.to_events(entry, "proj")[0] == self.adapter.to_event(entry, "proj")

    def test_non_tool_entries_yield_single_event(self):
        entry = CodexEntry(event_type="event_msg", event_subtype="agent_message",
                           event_msg_text="hi", role="assistant", session_id="s")
        assert len(self.adapter.to_events(entry, "proj")) == 1
        assert self.adapter.to_events(CodexEntry(event_type="turn_context"), "proj") == []

    def test_fingerprints_distinct_and_stable_across_reparse(self, tmp_path):
        store = EventStore(tmp_path / "events.db")
        entry = self._patch("/r/a/1.md", "/r/b/2.md", "/r/c/3.md")
        dicts = [e.to_dict() for e in self.adapter.to_events(entry, "proj")]
        stored = store.store_with_offset(dicts, "fp1", "codex", "/x.jsonl", 10)
        assert len(stored) == 3  # extras never collapse into each other/primary
        again = [e.to_dict() for e in self.adapter.to_events(entry, "proj")]
        assert store.store_with_offset(again, "fp1", "codex", "/x.jsonl", 10) == []
        rows = store._conn.execute(
            "SELECT file_path FROM events ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["/r/a/1.md", "/r/b/2.md", "/r/c/3.md"]


# --- Watcher end-to-end -------------------------------------------------------


class TestCodexWatcherAttributionPaths:
    def test_rollout_lands_absolute_paths_and_workdir_cwd(self, tmp_path):
        rollout = tmp_path / "rollout-2026-09-21T10-00-00-sess-1.jsonl"
        js_patch = ('const patch = "*** Begin Patch\\n*** Add File: '
                    f'{SESSION_CWD}/docs/a.md\\n+x\\n*** Update File: /other/repo/b.md'
                    '\\n@@\\n-x\\n+y\\n*** End Patch\\n"; await tools.apply_patch(patch);')
        lines = [
            _meta_line(),
            _custom_call("exec", js_patch, call_id="c1"),
            _custom_output("c1"),
            _custom_call("apply_patch", "*** Begin Patch\n*** Add File: notes.md\n+x\n*** End Patch\n",
                         ts="2026-09-21T10:01:00Z", call_id="c2"),
            {"type": "response_item", "timestamp": "2026-09-21T10:02:00Z",
             "payload": {"type": "function_call", "name": "exec_command", "call_id": "c3",
                         "arguments": json.dumps({"cmd": "ls", "workdir": "/w/elsewhere"})}},
        ]
        rollout.write_text("".join(json.dumps(l) + "\n" for l in lines))

        store = EventStore(tmp_path / "events.db")
        watcher = CodexWatcher(store)
        events, _ = watcher._parse_and_adapt(rollout, 0)
        tool_uses = [e for e in events if e["event_type"] == "tool_use"]
        assert [(e.get("file_path"), e.get("cwd")) for e in tool_uses] == [
            (f"{SESSION_CWD}/docs/a.md", SESSION_CWD),
            ("/other/repo/b.md", SESSION_CWD),
            (f"{SESSION_CWD}/notes.md", SESSION_CWD),
            (None, "/w/elsewhere"),
        ]
        assert all(e.get("session_id") == "sess-1" for e in events)
