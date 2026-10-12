"""Tests for the Antigravity CLI parser (transcripts + conversation_summaries.db).

All data is synthetic: fictional uuids, ``/Users/u/work/proj`` paths and invented
text. The helpers at the top are shared by the other ``test_antigravity_*`` files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hub.parsers.antigravity_parser import (
    SUMMARIES_USER_VERSION,
    AntigravityParser,
    conversation_id_from_path,
    decode_arg,
    default_antigravity_base,
    workspace_uri_to_path,
)

CONV = "11111111-1111-4111-8111-111111111111"
CONV2 = "22222222-2222-4222-8222-222222222222"
CONV3 = "33333333-3333-4333-8333-333333333333"
FIXTURE = Path(__file__).parent / "fixtures" / "antigravity_sample.jsonl"
LOGGER = "hub.AntigravityParser"  # conftest mutes hub.*; tests raise it per case


# ── helpers (shared) ─────────────────────────────────────────────


def transcript_path(base: Path, conv: str = CONV) -> Path:
    return base / "brain" / conv / ".system_generated" / "logs" / "transcript.jsonl"


def fixture_lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def write_transcript(
    base: Path, lines: list, conv: str = CONV, age: float = 0.0, newline: bool = True
) -> Path:
    """Write ``lines`` (dicts or raw strings) as a conversation's transcript."""
    path = transcript_path(base, conv)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines)
    path.write_text(text + ("\n" if newline else ""), encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def append(path: Path, lines: list, newline: bool = True) -> None:
    text = "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + ("\n" if newline else ""))


def step(index: int, type_: str = "PLANNER_RESPONSE", sec: int | None = None, **extra) -> dict:
    d = {
        "step_index": index, "type": type_, "source": "MODEL", "status": "DONE",
        "created_at": "2026-05-01T10:00:%02dZ" % (index if sec is None else sec),
    }
    d.update(extra)
    return d


def enc(**kw) -> dict:
    """``args`` as the real transcript stores them: every value a JSON-encoded string."""
    return {k: json.dumps(v) for k, v in kw.items()}


SUMMARY_COLUMNS = (
    "conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER, "
    "last_modified_time datetime, workspace_uris TEXT, status TEXT, source TEXT, "
    "project_id TEXT, agent_name TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, "
    "battle_id TEXT, winning_conversation_id TEXT, not_fully_idle numeric, killed numeric, "
    "last_user_input_time datetime, last_user_input_step_index INTEGER, app_data_dir TEXT, "
    "raw_summary BLOB, group_id TEXT"
)


def summary_row(
    conv: str,
    title: str = "",
    preview: str = "",
    workspace: str | None = "file:///Users/u/work/proj",
    parent: str = "",
    depth: int = 0,
    app_dir: str = "antigravity-cli",
) -> tuple:
    uris = json.dumps([workspace]) if workspace else ""
    return (
        conv, title, preview, 3, "2026-05-01 10:00:20.123456+00:00", uris, "", "", "p1", "",
        parent, depth, "", "", 0, 0, "2026-05-01 10:00:00+00:00", 0, app_dir, None, "",
    )


def make_summaries_db(
    base: Path, rows: list[tuple], user_version: int = SUMMARIES_USER_VERSION,
    wal: bool = False, columns: str = SUMMARY_COLUMNS,
) -> sqlite3.Connection:
    """Create ``conversation_summaries.db`` with the real column names.

    Returns the open connection (close it to get a checkpointed, ``-wal``-less
    file; with ``wal=True`` the frames stay in ``-wal`` while it is open).
    """
    base.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base / "conversation_summaries.db")
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")  # keep the frames in -wal
    conn.execute(f"CREATE TABLE conversation_summaries ({columns})")
    marks = ",".join("?" * len(rows[0])) if rows else ""
    for row in rows:
        conn.execute(f"INSERT INTO conversation_summaries VALUES ({marks})", row)
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    return conn


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── layout / environment ─────────────────────────────────────────


class TestLayout:
    def test_conversation_id_comes_from_the_directory(self, tmp_path):
        assert conversation_id_from_path(transcript_path(tmp_path)) == CONV

    @pytest.mark.parametrize("rel", [
        "transcript.jsonl",
        "brain/x/transcript.jsonl",
        "brain/x/.system_generated/transcript.jsonl",
        "brain/x/.system_generated/logs/transcript_full.jsonl",
    ])
    def test_other_layouts_have_no_id(self, tmp_path, rel):
        assert conversation_id_from_path(tmp_path / rel) == ""

    def test_default_base_and_env_override(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        assert default_antigravity_base() == Path.home() / ".gemini" / "antigravity-cli"
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(tmp_path / "agy"))
        assert default_antigravity_base() == tmp_path / "agy"

    def test_can_parse(self, tmp_path):
        good = write_transcript(tmp_path, fixture_lines())
        assert AntigravityParser.can_parse(good)
        # same content, wrong layout
        loose = tmp_path / "transcript.jsonl"
        loose.write_text(FIXTURE.read_text())
        assert not AntigravityParser.can_parse(loose)
        # right layout, not a transcript
        junk = write_transcript(tmp_path, ["not json"], conv=CONV2)
        assert not AntigravityParser.can_parse(junk)
        empty = write_transcript(tmp_path, [], conv=CONV3, newline=False)
        assert not AntigravityParser.can_parse(empty)
        assert not AntigravityParser.can_parse(tmp_path / "missing" / "transcript.jsonl")

    def test_wrong_layout_is_skipped_and_logged_once(self, tmp_path, caplog):
        caplog.set_level("WARNING", logger=LOGGER)
        parser = AntigravityParser()
        loose = tmp_path / "transcript.jsonl"
        loose.write_text(FIXTURE.read_text())
        assert parser.parse_file(loose) == []
        assert parser.parse_incremental(loose, 0) == ([], 0)
        assert parser.skipped["layout"] == 2
        assert sum("formato conocido" in r.getMessage() for r in caplog.records) == 1


# ── args decoding ────────────────────────────────────────────────


class TestDecodeArg:
    def test_json_encoded_string_is_decoded(self):
        assert decode_arg('"/Users/u/work/proj/a.py"') == "/Users/u/work/proj/a.py"
        assert decode_arg("12") == 12
        assert decode_arg("true") is True
        assert decode_arg('{"k": 1}') == {"k": 1}

    def test_undecodable_string_is_kept_as_is(self):
        # free text, and a value the writer cut mid-string (unterminated quote)
        assert decode_arg("ls -la") == "ls -la"
        assert decode_arg('"/Users/u/work/pro') == '"/Users/u/work/pro'

    def test_non_strings_pass_through(self):
        assert decode_arg(7) == 7
        assert decode_arg(None) is None


# ── full parse ───────────────────────────────────────────────────


class TestParseFile:
    def test_every_step_with_context(self, tmp_path):
        path = write_transcript(tmp_path, fixture_lines())
        entries = AntigravityParser().parse_file(path)

        assert [e.step_index for e in entries] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14]
        assert {e.session_id for e in entries} == {CONV}
        by = {e.step_index: e for e in entries}

        # decoded args: paths without quotes, numbers and booleans typed
        view, write = by[3].tool_calls
        assert (view.name, view.args["AbsolutePath"]) == ("view_file", "/Users/u/work/proj/src/app.py")
        assert (view.args["StartLine"], view.args["EndLine"]) == (1, 40)
        assert (write.name, write.args["Overwrite"]) == ("write_to_file", False)
        assert by[1].tool_calls[0].args["CommandLine"] == "ls src"

        # conversation context, stamped on every entry
        assert {e.cwd for e in entries} == {"/Users/u/work/proj"}
        assert {e.session_title for e in entries} == {
            "Please add a greeting helper to the widget service."}
        assert all(e.initial_prompt.startswith("Please add") for e in entries)

        # format details kept verbatim
        assert by[8].status == "RUNNING"
        assert by[9].truncated_fields == ["content"]
        assert by[10].step_type == "FUTURE_STEP_KIND"
        assert by[12].error == "no such file"
        assert by[1].thinking.startswith("I should list")

    def test_gap_in_step_index_is_not_an_error(self, tmp_path):
        parser = AntigravityParser()
        entries = parser.parse_file(write_transcript(tmp_path, fixture_lines()))
        assert 13 not in {e.step_index for e in entries}
        assert not parser.skipped

    def test_token_keys_are_ignored(self, tmp_path):
        # tokens are not read in v1 (owner decision); the keys must not break a parse
        entries = AntigravityParser().parse_file(write_transcript(tmp_path, fixture_lines()))
        step6 = next(e for e in entries if e.step_index == 6)
        assert step6.raw["input_tokens"] == 1200
        assert not hasattr(step6, "input_tokens")


class TestDuplicatesAndOrder:
    def test_a_repeated_conversation_adds_nothing(self, tmp_path):
        lines = fixture_lines()
        once = AntigravityParser().parse_file(write_transcript(tmp_path, lines, conv=CONV))
        twice = AntigravityParser().parse_file(
            write_transcript(tmp_path, lines + lines, conv=CONV2))
        assert [e.step_index for e in twice] == [e.step_index for e in once]
        assert len({e.step_index for e in twice}) == len(twice)

    def test_a_partial_copy_restarting_at_zero_adds_nothing(self, tmp_path):
        lines = fixture_lines()
        entries = AntigravityParser().parse_file(write_transcript(tmp_path, lines + lines[:5]))
        assert len(entries) == len(lines)

    def test_first_occurrence_wins(self, tmp_path):
        a = step(1, content="first")
        b = step(1, content="second")
        entries = AntigravityParser().parse_file(write_transcript(tmp_path, [a, b]))
        assert [e.content for e in entries] == ["first"]

    def test_out_of_order_steps_are_all_kept_in_file_order(self, tmp_path):
        lines = [step(0, "USER_INPUT", content="a"), step(3, content="d"),
                 step(1, content="b"), step(2, content="c"), step(5, content="f")]
        parser = AntigravityParser()
        entries = parser.parse_file(write_transcript(tmp_path, lines))
        assert [e.step_index for e in entries] == [0, 3, 1, 2, 5]
        assert not parser.skipped

    def test_first_prompt_and_cwd_are_the_lowest_step_index_not_the_first_line(self, tmp_path):
        run = lambda i, cwd: step(i, tool_calls=[{"name": "run_command", "args": enc(
            CommandLine="x", Cwd=cwd)}])
        lines = [
            step(4, "USER_INPUT", content="second prompt"), run(5, "/Users/u/work/later"),
            step(0, "USER_INPUT", content="first prompt"), run(1, "/Users/u/work/earlier"),
        ]
        entries = AntigravityParser().parse_file(write_transcript(tmp_path, lines))
        assert {e.initial_prompt for e in entries} == {"first prompt"}
        assert {e.cwd for e in entries} == {"/Users/u/work/earlier"}


class TestBadLines:
    def test_unusable_lines_are_skipped_and_logged_once(self, tmp_path, caplog):
        caplog.set_level("WARNING", logger=LOGGER)
        good = step(0, "USER_INPUT", content="ok")
        bad = [
            "not json at all",
            "[1, 2]",
            json.dumps(dict(good, step_index="3")),       # not numeric
            json.dumps(dict(good, step_index=2.5)),
            json.dumps(dict(good, step_index=True)),      # bool is not an index
            json.dumps(dict(good, step_index=-1)),
            json.dumps({k: v for k, v in good.items() if k != "step_index"}),
            json.dumps({k: v for k, v in good.items() if k != "type"}),
            json.dumps(dict(good, type="")),
            json.dumps({k: v for k, v in good.items() if k != "created_at"}),
            json.dumps(dict(good, created_at="yesterday")),
            json.dumps(dict(good, step_index=7)),         # a valid one among them
        ]
        parser = AntigravityParser()
        entries = parser.parse_file(write_transcript(tmp_path, [good] + bad + bad))
        assert [e.step_index for e in entries] == [0, 7]
        # 5 invalid step_index variants (str, float, bool, negative, missing), two passes
        assert parser.skipped["step_index no numérico"] == 10
        assert parser.skipped["sin type"] == 4
        assert parser.skipped["sin created_at válido"] == 4
        assert parser.skipped["json inválido"] == 2
        assert parser.skipped["no es un objeto"] == 2
        # one warning per (file, reason), however many lines or cycles
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == len(set(messages)) == 5

    def test_a_file_of_unknown_format_never_raises(self, tmp_path):
        path = write_transcript(tmp_path, [{"hello": "world"}] * 3)
        parser = AntigravityParser()
        assert parser.parse_file(path) == []
        entries, offset = parser.parse_incremental(path, 0)
        assert entries == [] and offset == path.stat().st_size  # consumed, not re-read forever

    def test_invalid_utf8_inside_a_line_is_tolerated(self, tmp_path):
        path = transcript_path(tmp_path)
        path.parent.mkdir(parents=True)
        raw = json.dumps(step(0, "USER_INPUT", content="XX")).encode().replace(b"XX", b"\xff\xfe")
        path.write_bytes(raw + b"\n")
        entries = AntigravityParser().parse_file(path)
        assert len(entries) == 1 and "\ufffd" in entries[0].content


# ── incremental read ─────────────────────────────────────────────


class TestIncremental:
    def test_appends_in_two_batches(self, tmp_path):
        lines = fixture_lines()
        path = write_transcript(tmp_path, lines[:5])
        parser = AntigravityParser()
        first, off1 = parser.parse_incremental(path, 0)
        assert [e.step_index for e in first] == [0, 1, 2, 3, 4]
        assert off1 == path.stat().st_size

        append(path, lines[5:9])
        second, off2 = parser.parse_incremental(path, off1)
        assert [e.step_index for e in second] == [5, 6, 7, 8]
        assert off2 == path.stat().st_size

        again, off3 = parser.parse_incremental(path, off2)
        assert again == [] and off3 == off2
        # context persists across the chunks
        assert {e.initial_prompt for e in first + second} == {first[0].initial_prompt}
        assert second[0].cwd == "/Users/u/work/proj"

    def test_partial_trailing_line_waits_for_its_newline(self, tmp_path):
        lines = fixture_lines()
        path = write_transcript(tmp_path, lines[:3])
        parser = AntigravityParser()
        _, off = parser.parse_incremental(path, 0)

        half = lines[3]
        append(path, [half[: len(half) // 2]], newline=False)
        entries, off_after_partial = parser.parse_incremental(path, off)
        assert entries == [] and off_after_partial == off  # nothing consumed

        append(path, [half[len(half) // 2:]], newline=True)
        entries, new_off = parser.parse_incremental(path, off_after_partial)
        assert [e.step_index for e in entries] == [3]
        assert new_off == path.stat().st_size

    def test_a_copy_appended_later_is_not_emitted_again(self, tmp_path):
        lines = fixture_lines()
        path = write_transcript(tmp_path, lines)
        parser = AntigravityParser()
        first, off = parser.parse_incremental(path, 0)
        append(path, lines)  # the same steps again, restarting at 0
        again, off2 = parser.parse_incremental(path, off)
        assert again == [] and off2 == path.stat().st_size
        append(path, [step(20, "USER_INPUT", sec=30, content="one more")])
        new, _ = parser.parse_incremental(path, off2)
        assert [e.step_index for e in new] == [20]

    def test_seen_steps_survive_a_daemon_restart(self, tmp_path):
        """A new parser (restart) rebuilds the seen set and context from the bytes before the offset."""
        lines = fixture_lines()
        path = write_transcript(tmp_path, lines[:6])
        _, off = AntigravityParser().parse_incremental(path, 0)

        append(path, lines[:6])        # a repeated copy
        append(path, lines[6:9])
        restarted = AntigravityParser()
        entries, new_off = restarted.parse_incremental(path, off)
        assert [e.step_index for e in entries] == [6, 7, 8]
        assert new_off == path.stat().st_size
        # the first prompt and the Cwd were learned from the prefix, not lost
        assert entries[0].initial_prompt.startswith("Please add")
        assert entries[0].cwd == "/Users/u/work/proj"

    def test_a_failed_store_re_reads_the_same_bytes(self, tmp_path):
        """Seen = what the offset says was ingested: retrying an offset re-emits it."""
        path = write_transcript(tmp_path, fixture_lines()[:5])
        parser = AntigravityParser()
        first, off = parser.parse_incremental(path, 0)
        assert len(first) == 5
        # the caller could not store the batch: it asks again from the old offset
        retry, off_retry = parser.parse_incremental(path, 0)
        assert [e.step_index for e in retry] == [0, 1, 2, 3, 4]
        assert off_retry == off

    def test_truncated_file_restarts_from_zero(self, tmp_path):
        lines = fixture_lines()
        path = write_transcript(tmp_path, lines)
        parser = AntigravityParser()
        _, off = parser.parse_incremental(path, 0)

        write_transcript(tmp_path, lines[:3])  # rewound: smaller than the stored offset
        assert path.stat().st_size < off
        entries, new_off = parser.parse_incremental(path, off)
        assert [e.step_index for e in entries] == [0, 1, 2]
        assert new_off == path.stat().st_size

    def test_the_session_time_range_covers_every_step_seen_not_just_the_chunk(self, tmp_path):
        path = write_transcript(tmp_path, [step(0, "USER_INPUT", sec=5, content="a"),
                                           step(3, sec=50, content="b")])
        parser = AntigravityParser()
        first, off = parser.parse_incremental(path, 0)
        assert {(e.first_event_at, e.last_event_at) for e in first} == {
            ("2026-05-01T10:00:05Z", "2026-05-01T10:00:50Z")}
        append(path, [step(1, sec=20, content="older, written later")])
        (older,), _ = parser.parse_incremental(path, off)
        assert (older.first_event_at, older.last_event_at) == (
            "2026-05-01T10:00:05Z", "2026-05-01T10:00:50Z")
        # a restarted parser derives the same range from the prefix
        (again,), _ = AntigravityParser().parse_incremental(path, off)
        assert (again.first_event_at, again.last_event_at) == (
            older.first_event_at, older.last_event_at)

    def test_timestamps_with_and_without_fractions_or_offsets_compare_by_instant(self, tmp_path):
        lines = [step(0, "USER_INPUT", content="a", created_at="2026-05-01T10:00:09.500Z"),
                 step(1, content="b", created_at="2026-05-01T10:00:09Z"),
                 step(2, content="c", created_at="2026-05-01T05:00:30-05:00")]  # = 10:00:30Z
        entry = AntigravityParser().parse_file(write_transcript(tmp_path, lines))[0]
        assert (entry.first_event_at, entry.last_event_at) == (
            "2026-05-01T10:00:09Z", "2026-05-01T05:00:30-05:00")

    def test_missing_file_raises_oserror_for_the_harvester(self, tmp_path):
        with pytest.raises(OSError):
            AntigravityParser().parse_incremental(transcript_path(tmp_path), 0)

    def test_history_walk_state_can_be_dropped(self, tmp_path):
        path = write_transcript(tmp_path, fixture_lines())
        parser = AntigravityParser()
        parser.parse_incremental(path, 0)
        parser._session_ctx.pop(str(path), None)  # what harvest_history_file does
        entries, off = parser.parse_incremental(path, path.stat().st_size)
        assert entries == [] and off == path.stat().st_size


# ── workspace URIs ───────────────────────────────────────────────


class TestWorkspaceUri:
    @pytest.mark.parametrize("uri,expected", [
        ("file:///Users/u/work/proj", "/Users/u/work/proj"),
        ("file:///Users/u/work/proj/", "/Users/u/work/proj"),
        ("file:///Users/u/work/my%20proj", "/Users/u/work/my proj"),
        ("file://localhost/Users/u/work/proj", "/Users/u/work/proj"),
        ("file:///C:/Users/u/work/proj", "C:\\Users\\u\\work\\proj"),
        ("file:///c%3A/Users/u/proj", "c:\\Users\\u\\proj"),  # VS Code style: encoded colon
        ("file://fileserver/share/dir", "\\\\fileserver\\share\\dir"),
        ("https://example.com/x", ""),
        ("not a uri", ""),
        ("", ""),
        (None, ""),
        (7, ""),
    ])
    def test_decoding(self, uri, expected):
        assert workspace_uri_to_path(uri) == expected


# ── conversation_summaries.db ────────────────────────────────────


class TestSummaries:
    def test_only_the_cli_rows_with_their_metadata(self, tmp_path):
        base = tmp_path / "antigravity-cli"
        make_summaries_db(base, [
            summary_row(CONV, title="Greeting helper", preview="Please add a greeting",
                        workspace="file:///Users/u/work/proj"),
            summary_row(CONV2, workspace="file:///Users/u/work/other", app_dir="antigravity"),
            summary_row(CONV3, parent=CONV, depth=1, workspace=None),
        ]).close()
        rows = AntigravityParser().read_summaries(base)
        assert set(rows) == {CONV, CONV3}  # the desktop app's row is another surface
        main = rows[CONV]
        assert (main.title, main.preview) == ("Greeting helper", "Please add a greeting")
        assert main.cwd == "/Users/u/work/proj"
        assert main.parent_conversation_id == "" and main.nesting_depth == 0
        sub = rows[CONV3]
        assert (sub.parent_conversation_id, sub.nesting_depth, sub.cwd) == (CONV, 1, "")

    def test_app_dir_is_compared_by_name(self, tmp_path):
        base = tmp_path / "custom-agy"
        make_summaries_db(base, [
            summary_row(CONV, app_dir="custom-agy"),
            summary_row(CONV2, app_dir="/somewhere/else/custom-agy/"),
            summary_row(CONV3, app_dir=""),
        ]).close()
        assert set(AntigravityParser().read_summaries(base)) == {CONV, CONV2}

    def test_absent_index_is_none_and_quiet(self, tmp_path, caplog):
        caplog.set_level("INFO", logger=LOGGER)
        assert AntigravityParser().read_summaries(tmp_path) is None
        assert not caplog.records

    def test_unknown_user_version_is_skipped_with_one_warning(self, tmp_path, caplog):
        caplog.set_level("WARNING", logger=LOGGER)
        base = tmp_path / "antigravity-cli"
        make_summaries_db(base, [summary_row(CONV)], user_version=4).close()
        parser = AntigravityParser()
        assert parser.read_summaries(base) is None
        assert parser.read_summaries(base) is None
        assert parser.read_summaries(base) is None
        assert sum("user_version" in r.getMessage() for r in caplog.records) == 1

    def test_unreadable_index_is_skipped_once_and_recovers(self, tmp_path, caplog):
        caplog.set_level("INFO", logger=LOGGER)
        base = tmp_path / "antigravity-cli"
        base.mkdir()
        db = base / "conversation_summaries.db"
        db.write_bytes(b"this is not a sqlite database" * 50)
        parser = AntigravityParser()
        for _ in range(3):
            assert parser.read_summaries(base) is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

        db.unlink()
        make_summaries_db(base, [summary_row(CONV)]).close()
        assert set(parser.read_summaries(base)) == {CONV}
        assert sum("vuelve a leerse" in r.getMessage() for r in caplog.records) == 1

    def test_a_locked_index_with_a_wal_present_is_skipped_once_and_recovers(
            self, tmp_path, monkeypatch, caplog):
        caplog.set_level("INFO", logger=LOGGER)
        base = tmp_path / "antigravity-cli"
        owner = make_summaries_db(base, [summary_row(CONV)], wal=True)  # the CLI is running: -wal exists
        assert (base / "conversation_summaries.db-wal").exists()
        real = sqlite3.connect

        def locked(database, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        parser = AntigravityParser()
        monkeypatch.setattr(sqlite3, "connect", locked)
        for _ in range(3):
            assert parser.read_summaries(base) is None
        assert sum(r.levelname == "WARNING" for r in caplog.records) == 1
        monkeypatch.setattr(sqlite3, "connect", real)
        assert set(parser.read_summaries(base)) == {CONV}
        assert sum("vuelve a leerse" in r.getMessage() for r in caplog.records) == 1
        owner.close()

    def test_a_different_schema_is_skipped_not_raised(self, tmp_path, caplog):
        caplog.set_level("WARNING", logger=LOGGER)
        base = tmp_path / "antigravity-cli"
        make_summaries_db(base, [("only", "two")], columns="conversation_id TEXT, title TEXT").close()
        assert AntigravityParser().read_summaries(base) is None
        assert sum("No se pudo leer" in r.getMessage() for r in caplog.records) == 1

    def test_immutable_only_when_there_is_no_wal(self, tmp_path, monkeypatch):
        """#54: with the CLI off a read must leave no -wal/-shm; with it on, WAL frames are visible."""
        base = tmp_path / "antigravity-cli"
        # CLI off: a WAL-mode database that was closed cleanly (no -wal next to it)
        owner = make_summaries_db(base, [summary_row(CONV)], wal=True)
        owner.close()
        db = base / "conversation_summaries.db"
        assert not (base / "conversation_summaries.db-wal").exists()
        before = sorted(os.listdir(base))
        digest = _sha(db)

        uris: list[str] = []
        real = sqlite3.connect

        def spy(database, *args, **kwargs):
            uris.append(str(database))
            return real(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", spy)
        assert set(AntigravityParser().read_summaries(base)) == {CONV}
        assert len(uris) == 1 and uris[0].endswith("?mode=ro&immutable=1")
        assert sorted(os.listdir(base)) == before  # no -wal, no -shm, nothing new
        assert _sha(db) == digest

        # CLI on: its -wal sits next to the file and holds a row not yet checkpointed
        monkeypatch.setattr(sqlite3, "connect", real)
        live = sqlite3.connect(db)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO conversation_summaries VALUES (%s)" % ",".join("?" * 21),
                     summary_row(CONV2))
        live.commit()
        assert (base / "conversation_summaries.db-wal").stat().st_size > 0
        digest = _sha(db)

        uris.clear()
        monkeypatch.setattr(sqlite3, "connect", spy)
        rows = AntigravityParser().read_summaries(base)
        assert set(rows) == {CONV, CONV2}  # the WAL frame is visible: not immutable
        assert len(uris) == 1 and uris[0].endswith("?mode=ro") and "immutable=1" not in uris[0]
        assert _sha(db) == digest  # the main file is never written
        monkeypatch.setattr(sqlite3, "connect", real)
        live.close()


class TestScanCwd:
    def test_first_run_command_cwd(self, tmp_path):
        path = write_transcript(tmp_path, fixture_lines())
        assert AntigravityParser().scan_cwd(path) == "/Users/u/work/proj"

    def test_none_found_and_bounded(self, tmp_path):
        path = write_transcript(tmp_path, [step(0, "USER_INPUT", content="hi")])
        parser = AntigravityParser()
        assert parser.scan_cwd(path) == ""
        late = write_transcript(tmp_path, fixture_lines(), conv=CONV2)
        assert parser.scan_cwd(late, max_bytes=10) == ""  # the first line alone exceeds it
        assert parser.scan_cwd(tmp_path / "missing.jsonl") == ""
