"""A removed worktree folds back from its full ``git worktree add`` (#161).

#149 only read the capped summary of a shell call (``"Bash: " +
command[:80]``), which cuts the worktree path. Since #161 every adapter keeps
the full command of a shell call that adds a worktree (``with_shell_evidence``)
and the workspace layer reads it back — in the normal attribution pass and,
once, from the raw transcripts for history older than #161.

Synthetic fixtures only: the repo ``acme/widget-services`` and its worktree
``widget-services-wt-12``, which never exists on disk when a pass runs (it is
created and removed between two polls). HOME is a tmp dir.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3

import pytest

import hub.cache.workspace_store as ws
from hub.adapters.antigravity_adapter import AntigravityAdapter
from hub.adapters.claude_adapter import ClaudeAdapter
from hub.adapters.codex_adapter import CodexAdapter
from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.adapters.pi_adapter import PiAdapter
from hub.adapters.qwen_adapter import QwenAdapter
from hub.cache.event_store import EventStore, cap_full_text
from hub.cache.workspace_store import WorkspaceStore, stored_worktree_commands
from hub.models.antigravity import AntigravityEntry, AntigravityToolCall
from hub.models.base import (
    SHELL_EVIDENCE_MARKER, shell_command_text, shell_evidence_command,
    with_shell_evidence,
)
from hub.models.codex import CodexEntry, CodexFunctionCall
from hub.models.opencode import OpenCodeEntry, OpenCodeToolCall
from hub.models.pi import PiEntry, PiToolCall
from hub.models.qwen import QwenEntry, QwenFunctionCall
from hub.parsers.claude_parser import ClaudeParser
from hub.worktree_evidence import scan_worktree_adds
from tests.test_edge_active_days import UTC, _insert
from tests.test_workspace_store import _mkrepo

D = "2026-09-18"
REPO_KEY = "git_remote:github.com/acme/widget-services"
UUID = "7d1d86b7-3bf5-423a-a467-005784b6a1b2"


def _long_add(repo, wt_name="widget-services-wt-12"):
    """A real-looking add command, well over the 80-character summary cap."""
    cmd = (f"cd {repo} && git worktree add -b fix/portfolio-temp-worktrees-161 "
           f"../{wt_name} origin/main")
    assert len(cmd) > 80
    return cmd


def _claude_line(session, cwd, command, ts, uid):
    return json.dumps({
        "parentUuid": None, "isSidechain": False, "cwd": cwd,
        "sessionId": session, "type": "assistant", "uuid": uid, "timestamp": ts,
        "message": {
            "model": "m", "id": f"msg-{uid}", "type": "message",
            "role": "assistant", "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": f"tu-{uid}", "name": "Bash",
                         "input": {"command": command}}],
        },
    })


def _claude_events(tmp_path, lines):
    """Run JSONL lines through the real Claude parser + adapter."""
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(lines) + "\n")
    entries, _ = ClaudeParser().parse_incremental(path, 0)
    adapter = ClaudeAdapter()
    return [e.to_dict() for e in (adapter.to_event(x, "p") for x in entries) if e]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ws, "user_timezone", lambda: UTC)
    dev = home / "dev"
    repo = _mkrepo(dev, "widget-services", "git@github.com:acme/widget-services.git")
    wt = dev / "widget-services-wt-12"          # never on disk at a pass
    events = tmp_path / "events.db"
    EventStore(db_path=events).close()
    # Transcript roots for the #161 scan: only the fixture's.
    claude = tmp_path / "claude-projects"
    bases = {p: tmp_path / f"none-{p}" for p in
             ("codex", "qwen", "pi", "antigravity", "opencode")}
    bases["claude"] = claude
    monkeypatch.setattr(WorkspaceStore, "transcript_bases", bases)
    return tmp_path, events, repo, wt, claude


def _store_events(events, dicts):
    s = EventStore(db_path=events)
    try:
        for d in dicts:
            s.store(d)
    finally:
        s.close()


def _incremental(tmp_path, events):
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        return s.attribute_incremental(events)
    finally:
        s.close()


def _q(tmp_path, sql, params=()):
    c = sqlite3.connect(tmp_path / "workspace.db")
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _keys_of(tmp_path, session):
    return {k for (k,) in _q(tmp_path, """SELECT w.workspace_key
        FROM path_attributions a JOIN workspaces w ON w.id = a.workspace_id
        WHERE a.session_id = ?""", (session,))}


def _meta(tmp_path):
    return dict(_q(tmp_path, "SELECT key, value FROM rollup_meta"))


def _backups(tmp_path):
    d = tmp_path / "backups"
    return sorted(d.glob("workspace-*.db")) if d.exists() else []


# ── The evidence the adapters keep ───────────────────────────────────────

class TestEvidenceText:
    CMD = "git worktree add -b fix/x ../widget-services-wt-12 main"

    def test_only_a_worktree_add_is_kept(self):
        assert with_shell_evidence(None, "pytest -q") is None
        assert with_shell_evidence("hi", "git worktree list") == "hi"
        kept = with_shell_evidence(None, self.CMD)
        assert kept == SHELL_EVIDENCE_MARKER + self.CMD
        assert shell_evidence_command(kept) == self.CMD

    def test_appended_after_existing_text(self):
        kept = with_shell_evidence("[thinking]\nadd a worktree", self.CMD)
        assert kept.startswith("[thinking]\nadd a worktree\n\n")
        assert shell_evidence_command(kept) == self.CMD

    def test_text_without_the_block_has_no_command(self):
        assert shell_evidence_command("run [shell command]\nrm -rf x") is None
        assert shell_evidence_command(None) is None

    def test_argv_forms(self):
        assert shell_command_text(["bash", "-lc", self.CMD]) == self.CMD
        assert shell_command_text(["git", "worktree", "add", "a b"]) == \
            "git worktree add 'a b'"
        assert shell_command_text({"x": 1}) is None


class TestAdapters:
    """Every provider whose tool call carries the command keeps it."""
    CWD = "/home/u/work/widget-services"
    CMD = ("git worktree add -b fix/portfolio-temp-worktrees-161 "
           "../widget-services-wt-12 origin/main")

    def _check(self, event):
        assert event is not None
        assert shell_evidence_command(event.full_text) == self.CMD

    def test_claude(self, tmp_path):
        [ev] = _claude_events(tmp_path, [_claude_line("s", self.CWD, self.CMD,
                                                      f"{D}T08:00:00Z", "u1")])
        assert shell_evidence_command(ev["full_text"]) == self.CMD
        assert self.CMD not in ev["summary"]       # the summary stays capped

    def test_claude_other_commands_unchanged(self, tmp_path):
        [ev] = _claude_events(tmp_path, [_claude_line("s", self.CWD, "pytest -q",
                                                      f"{D}T08:00:00Z", "u1")])
        assert ev.get("full_text") is None

    @pytest.mark.parametrize("name,args", [
        ("exec_command", {"cmd": CMD, "workdir": CWD}),
        ("shell", {"command": ["bash", "-lc", CMD], "workdir": CWD}),
    ])
    def test_codex(self, name, args):
        entry = CodexEntry(
            event_type="response_item", timestamp=f"{D}T08:00:00Z",
            payload_type="function_call", session_id="s", cwd=self.CWD,
            function_call=CodexFunctionCall(call_id="c", name=name,
                                            arguments=json.dumps(args)))
        ev = CodexAdapter().to_event(entry, "p")
        self._check(ev)
        assert ev.cwd == self.CWD

    def test_qwen(self):
        entry = QwenEntry(type="assistant", session_id="s", cwd=self.CWD,
                          timestamp=f"{D}T08:00:00Z",
                          function_calls=[QwenFunctionCall(
                              name="run_shell_command", args={"command": self.CMD})])
        self._check(QwenAdapter().to_event(entry, "p"))

    def test_opencode(self):
        entry = OpenCodeEntry(session_id="s", cwd=self.CWD, role="assistant",
                              part_type="tool", timestamp="",
                              tool_call=OpenCodeToolCall(
                                  name="bash", input_data={"command": self.CMD}))
        self._check(OpenCodeAdapter().to_event(entry, "p"))

    def test_pi(self):
        entry = PiEntry(event_type="message", role="assistant", session_id="s",
                        cwd=self.CWD, timestamp=f"{D}T08:00:00Z",
                        tool_calls=[PiToolCall(name="bash",
                                               arguments={"command": self.CMD})])
        self._check(PiAdapter().to_event(entry, "p"))

    def test_antigravity(self):
        entry = AntigravityEntry(step_index=1, step_type="PLANNER_RESPONSE",
                                 session_id="s", cwd=self.CWD,
                                 timestamp=f"{D}T08:00:00Z",
                                 tool_calls=[AntigravityToolCall(
                                     name="run_command",
                                     args={"CommandLine": self.CMD})])
        evs = [e for e in AntigravityAdapter().to_events(entry, "p")
               if e.tool_name == "run_command"]
        assert evs and shell_evidence_command(evs[0].full_text) == self.CMD
        # A call run from another Cwd says so in the kept command.
        entry.tool_calls[0].args["Cwd"] = "/home/u/work/other"
        evs = [e for e in AntigravityAdapter().to_events(entry, "p")
               if e.tool_name == "run_command"]
        assert shell_evidence_command(evs[0].full_text) == (
            "cd /home/u/work/other && " + self.CMD)


def test_a_capped_full_text_is_not_complete():
    cmd = "git worktree add ../widget-services-wt-12 " + "x" * 400
    text = cap_full_text(with_shell_evidence(None, cmd), 200)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, tool_name TEXT, cwd TEXT)")
    c.execute("CREATE TABLE event_content (event_id INTEGER, full_text TEXT)")
    c.execute("INSERT INTO events VALUES (1, 'Bash', '/r')")
    c.execute("INSERT INTO event_content VALUES (1, ?)", (text,))
    [(_cmd, cwd, complete)] = stored_worktree_commands(c)
    assert cwd == "/r" and complete is False


# ── Normal flow: created and removed between two polls ───────────────────

class TestNormalFlow:
    def test_long_add_command_folds_the_removed_worktree(self, env, caplog):
        tmp_path, events, repo, wt, _claude = env
        # Poll 1: the orchestrator session adds the worktree (> 80 chars).
        _store_events(events, _claude_events(tmp_path, [_claude_line(
            "orch", str(repo), _long_add(repo), f"{D}T08:00:00Z", "u1")]))
        with caplog.at_level(logging.INFO, logger="hub"):
            res = _incremental(tmp_path, events)
        assert res["worktree_adds"] == {"recorded": 1, "conflicting": 0}
        assert _q(tmp_path, "SELECT workspace_key, source FROM known_git_roots"
                            " WHERE root_path = ?", (str(wt),)) == [
            (REPO_KEY, "worktree_add")]
        assert any("1 worktrees registrados" in r.getMessage()
                   for r in caplog.records)
        # Poll 2: the worker's session ran in the worktree, already removed.
        _insert(events, "worker", str(wt / "pkg" / "a.py"), f"{D}T09:00:00Z",
                cwd=str(wt))
        _insert(events, "worker-cwd", None, f"{D}T09:30:00Z", cwd=str(wt))
        _incremental(tmp_path, events)
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}
        assert _keys_of(tmp_path, "worker-cwd") == {REPO_KEY}

    def test_same_poll_window(self, env):
        tmp_path, events, repo, wt, _claude = env
        _store_events(events, _claude_events(tmp_path, [_claude_line(
            "orch", str(repo), _long_add(repo), f"{D}T08:00:00Z", "u1")]))
        _insert(events, "worker", str(wt / "a.py"), f"{D}T09:00:00Z", cwd=str(wt))
        _incremental(tmp_path, events)
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}

    def test_non_claude_provider(self, env):
        tmp_path, events, repo, wt, _claude = env
        entry = CodexEntry(
            event_type="response_item", timestamp=f"{D}T08:00:00Z",
            payload_type="function_call", session_id="orch", cwd=str(repo),
            function_call=CodexFunctionCall(call_id="c", name="exec_command",
                                            arguments=json.dumps({
                                                "cmd": _long_add(repo)})))
        _store_events(events, [CodexAdapter().to_event(entry, "p").to_dict()])
        _incremental(tmp_path, events)
        _insert(events, "worker", str(wt / "a.py"), f"{D}T09:00:00Z", cwd=str(wt))
        _incremental(tmp_path, events)
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}

    def test_a_folder_named_like_a_worktree_does_not_fold(self, env):
        tmp_path, events, repo, _wt, _claude = env
        lookalike = repo.parent / "widget-services-wt-13"   # no add command
        _insert(events, "w13", str(lookalike / "a.py"), f"{D}T09:00:00Z",
                cwd=str(lookalike))
        _incremental(tmp_path, events)
        assert next(iter(_keys_of(tmp_path, "w13"))).startswith("path_hash:")
        assert _q(tmp_path, "SELECT COUNT(*) FROM known_git_roots"
                            " WHERE source = 'worktree_add'") == [(0,)]

    @pytest.mark.parametrize("tail", [
        "git worktree add -b fix/x ../$WT_NAME origin/main",
        "git worktree add -b fix/x ../widget-services-wt-12 origin/main | tee log",
    ])
    def test_variable_or_pipe_does_not_count(self, env, tail):
        tmp_path, events, repo, wt, _claude = env
        _store_events(events, _claude_events(tmp_path, [_claude_line(
            "orch", str(repo), f"cd {repo} && {tail}", f"{D}T08:00:00Z", "u1")]))
        _insert(events, "worker", str(wt / "a.py"), f"{D}T09:00:00Z", cwd=str(wt))
        res = _incremental(tmp_path, events)
        assert res["worktree_adds"]["recorded"] == 0
        assert next(iter(_keys_of(tmp_path, "worker"))).startswith("path_hash:")

    def test_two_repos_for_one_path_fold_nothing(self, env):
        tmp_path, events, repo, wt, _claude = env
        rival = _mkrepo(repo.parent, "globex-app", "git@github.com:globex/app.git")
        _store_events(events, _claude_events(tmp_path, [
            _claude_line("orch", str(repo), _long_add(repo), f"{D}T08:00:00Z", "u1"),
            _claude_line("orch2", str(rival),
                         f"git worktree add -b fix/other-branch-name {wt} main",
                         f"{D}T08:05:00Z", "u2"),
        ]))
        _insert(events, "worker", str(wt / "a.py"), f"{D}T09:00:00Z", cwd=str(wt))
        res = _incremental(tmp_path, events)
        assert res["worktree_adds"] == {"recorded": 0, "conflicting": 1}
        assert next(iter(_keys_of(tmp_path, "worker"))).startswith("path_hash:")


# ── One-time backfill from the raw transcripts ───────────────────────────

class TestTranscriptBackfill:
    def _seed(self, env):
        """workspace.db as v1.36.x left it: the worktree's edges are
        repo-less and events.db only has the capped summary of the add."""
        tmp_path, events, repo, wt, claude = env
        cmd = _long_add(repo)
        c = sqlite3.connect(events)
        c.execute(
            "INSERT INTO events (provider, project, event_type, timestamp,"
            " summary, session_id, tool_name, file_path, cwd, fingerprint,"
            " created_at, historical) VALUES ('claude', 'p', 'tool_use', ?,"
            " ?, ?, 'Bash', NULL, ?, 'fp-add', 1800000000, 1)",
            (f"{D}T08:00:00Z", "Bash: " + cmd[:80], UUID, str(repo)))
        c.commit()
        c.close()
        _insert(events, "worker", str(wt / "a.py"), f"{D}T09:00:00Z", cwd=str(wt))
        _insert(events, "w13", str(repo.parent / "widget-services-wt-13" / "b.py"),
                f"{D}T09:00:00Z")
        _incremental(tmp_path, events)      # migration 9 on an empty DB: no-op
        assert next(iter(_keys_of(tmp_path, "worker"))).startswith("path_hash:")
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("DELETE FROM schema_migrations WHERE version = 9")
        c.commit()
        c.close()
        # The orchestrator's raw transcript still has the whole command.
        proj = claude / repo.as_posix().replace("/", "-")
        proj.mkdir(parents=True)
        (proj / f"{UUID}.jsonl").write_text(_claude_line(
            UUID, str(repo), cmd, f"{D}T08:00:00Z", "u1") + "\n")
        (proj / "other.jsonl").write_text(_claude_line(
            "o", str(repo), "pytest -q", f"{D}T08:00:00Z", "u2") + "\n")
        return tmp_path, events, wt

    def _upgrade(self, tmp_path, events, caplog):
        s = WorkspaceStore(tmp_path / "workspace.db")      # migration 9
        try:
            meta = _meta(tmp_path)
            assert meta["worktree_add_recover"] == "pending"
            with caplog.at_level(logging.INFO, logger="hub"):
                return s.attribute_incremental(events)
        finally:
            s.close()

    def test_scan_reads_only_the_add_commands(self, env):
        tmp_path, _events, wt = self._seed(env)
        scan = scan_worktree_adds(WorkspaceStore.transcript_bases)
        assert scan.files_seen == 2 and scan.files_parsed == 1
        assert len(scan.commands) == 1 and scan.commands[0][2] == "claude"

    def test_backfill_folds_with_backup_and_logs(self, env, caplog):
        tmp_path, events, wt = self._seed(env)
        backups = len(_backups(tmp_path))
        res = self._upgrade(tmp_path, events, caplog)
        assert len(_backups(tmp_path)) == backups + 1
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}
        assert next(iter(_keys_of(tmp_path, "w13"))).startswith("path_hash:")
        ev = res["worktree_evidence"]
        assert ev["worktree_add"] == 1 and ev["transcript_commands"] == 1
        assert res["worktree_edges_recovered"] == 1
        msgs = [r.getMessage() for r in caplog.records]
        assert any("1 comandos 'git worktree add' completos" in m for m in msgs)
        assert any("1 aristas volvieron a su repo" in m for m in msgs)
        assert "worktree_add_recover" not in _meta(tmp_path)

    def test_second_pass_is_a_noop(self, env, caplog):
        tmp_path, events, _wt = self._seed(env)
        self._upgrade(tmp_path, events, caplog)
        edges = _q(tmp_path, "SELECT session_id, file_path, workspace_id"
                             " FROM path_attributions ORDER BY 1, 2")
        backups = len(_backups(tmp_path))
        res = _incremental(tmp_path, events)
        assert "worktree_evidence" not in res and res["attributed"] == 0
        assert len(_backups(tmp_path)) == backups
        WorkspaceStore(tmp_path / "workspace.db").close()   # runs once
        assert "worktree_add_recover" not in _meta(tmp_path)
        assert _q(tmp_path, "SELECT session_id, file_path, workspace_id"
                            " FROM path_attributions ORDER BY 1, 2") == edges

    def test_cut_midway_resumes_without_a_new_backup(self, env, monkeypatch, caplog):
        tmp_path, events, _wt = self._seed(env)
        WorkspaceStore(tmp_path / "workspace.db").close()    # migration 9
        real = WorkspaceStore._attribute_event_range

        def _boom(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", _boom)
        backups = len(_backups(tmp_path))
        with pytest.raises(sqlite3.OperationalError):
            _incremental(tmp_path, events)
        assert _meta(tmp_path)["worktree_add_recover"] == "running"
        assert len(_backups(tmp_path)) == backups + 1
        monkeypatch.setattr(WorkspaceStore, "_attribute_event_range", real)
        with caplog.at_level(logging.INFO, logger="hub"):
            res = _incremental(tmp_path, events)
        assert len(_backups(tmp_path)) == backups + 1       # no second backup
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}
        assert res["worktree_edges_recovered"] == 1
        assert any("sin un respaldo nuevo" in r.getMessage() for r in caplog.records)
        assert "worktree_add_recover" not in _meta(tmp_path)

    def test_runs_after_the_149_recovery(self, env, caplog):
        """A DB that already went through #149 (migration 8 done, no marker)
        still gets the #161 backfill."""
        tmp_path, events, _wt = self._seed(env)
        assert _q(tmp_path, "SELECT 1 FROM schema_migrations WHERE version = 8")
        assert "worktree_recover" not in _meta(tmp_path)
        self._upgrade(tmp_path, events, caplog)
        assert _keys_of(tmp_path, "worker") == {REPO_KEY}

    def test_no_transcript_no_fold(self, env, caplog):
        tmp_path, events, _wt = self._seed(env)
        for f in (WorkspaceStore.transcript_bases["claude"]).rglob("*.jsonl"):
            f.unlink()
        res = self._upgrade(tmp_path, events, caplog)
        assert next(iter(_keys_of(tmp_path, "worker"))).startswith("path_hash:")
        assert res["worktree_evidence"]["worktree_add"] == 0


class TestScanNeverBreaks:
    """#165: the transcript scan is best-effort and never blocks attribution."""

    SUBAGENT = Path(__file__).parent / "fixtures" / "codex_subagent_sample.jsonl"

    def test_codex_subagent_session_goes_through_the_scan(self, tmp_path):
        day = tmp_path / "codex" / "sessions" / "2026" / "10" / "07"
        day.mkdir(parents=True)
        add = ("git worktree add /home/dev/project-alpha-wt-12 -b feature/module-a-wt "
               "origin/main && echo done-with-the-worktree")
        call = {"timestamp": "2026-10-07T07:30:20.000Z", "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command",
                            "call_id": "call-wt",
                            "arguments": json.dumps({"cmd": add,
                                                     "workdir": "/home/dev/project-alpha"})}}
        text = self.SUBAGENT.read_text() + json.dumps(call) + "\n"
        (day / "rollout-2026-10-07T07-30-06-cccccccc.jsonl").write_text(text)
        bases = {p: tmp_path / f"none-{p}" for p in
                 ("claude", "qwen", "pi", "antigravity", "opencode")}
        bases["codex"] = tmp_path / "codex"
        scan = scan_worktree_adds(bases)           # used to raise AttributeError
        assert scan.files_parsed == 1 and scan.files_failed == 0
        assert [c for c in scan.commands if c[2] == "codex"]

    def test_a_failing_file_is_counted_and_the_others_still_count(
            self, env, monkeypatch, caplog):
        tmp_path, _events, repo, _wt, claude = env
        proj = claude / repo.as_posix().replace("/", "-")
        proj.mkdir(parents=True)
        cmd = _long_add(repo)
        (proj / "good.jsonl").write_text(_claude_line(
            "g", str(repo), cmd, f"{D}T08:00:00Z", "u1") + "\n")
        (proj / "bad.jsonl").write_text(_claude_line(
            "b", str(repo), cmd, f"{D}T08:00:00Z", "u2") + "\n")
        from hub.watchers.claude_watcher import ClaudeWatcher
        real = ClaudeWatcher._parse_and_adapt

        def flaky(self, path, offset):
            if Path(path).name == "bad.jsonl":
                raise RuntimeError("a parser bug")
            return real(self, path, offset)
        monkeypatch.setattr(ClaudeWatcher, "_parse_and_adapt", flaky)
        with caplog.at_level(logging.WARNING, logger="hub"):
            scan = scan_worktree_adds(WorkspaceStore.transcript_bases)
        assert scan.files_failed == 1 and scan.files_parsed == 2
        assert len(scan.commands) == 1
        msgs = [r.getMessage() for r in caplog.records]
        assert any("RuntimeError" in m for m in msgs)
        assert not any("bad.jsonl" in m for m in msgs)   # no path in the log
