"""MCP server registry + the ``mool doctor`` "MCP servers" section (#70).

Each ``hub.mcp_server`` records itself in ``~/.moolmesh/run/mcp-<pid>.json``;
doctor lists the live ones, deletes the files of the dead, and flags the ones
running a version other than the installed one — as a warning, never a failed
check, and without touching any process.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hub
from hub import mcp_registry
from hub.cli import _print_mcp_servers
from hub.mcp_registry import format_age, scan_run_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLED = "1.26.2"
NONE_MSG = "none registered (servers older than 1.26.2 don't register)"


def _dead_pid() -> int:
    """A PID that existed a moment ago and is now gone (reaped, so not a zombie)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write(directory: Path, pid, version="1.26.2", *, ppid=4242, started_at=None,
           name=None, raw=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"mcp-{pid}.json")
    if raw is not None:
        path.write_bytes(raw)
        return path
    started_at = started_at or (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps({
        "pid": pid, "ppid": ppid, "version": version,
        "started_at": started_at, "python": "/opt/u/venv/bin/python",
    }), encoding="utf-8")
    return path


# ── scan_run_dir ────────────────────────────────────────────────────

class TestScan:
    def test_a_missing_directory_is_none(self, tmp_path):
        assert scan_run_dir(tmp_path / "nope", INSTALLED) is None

    def test_live_dead_and_stale(self, tmp_path):
        d = tmp_path / "run"
        me = _write(d, os.getpid(), INSTALLED)
        old = _write(d, os.getppid(), "1.26.1")
        dead_pid = _dead_pid()
        dead = _write(d, dead_pid, "1.26.1")

        scan = scan_run_dir(d, INSTALLED)

        assert {s.pid: s.stale for s in scan.servers} == {os.getpid(): False,
                                                          os.getppid(): True}
        assert scan.stale == 1
        assert scan.removed == 1 and not dead.exists()
        assert me.exists() and old.exists()                 # live files untouched

    def test_without_a_comparable_installed_version_nothing_is_stale(self, tmp_path):
        _write(tmp_path, os.getpid(), "0.0.1")
        for installed in (None, "", "dev"):
            scan = scan_run_dir(tmp_path, installed)
            assert [s.stale for s in scan.servers] == [False]

    @pytest.mark.parametrize("raw", [
        b"not json at all",
        b'{"pid": "12"}',                       # pid not an int
        b'{"pid": 0}',                          # 0 / negative would signal a process group
        b'{"pid": -1}',
        b'{"pid": true}',                       # bool is an int subclass
        b"[1, 2, 3]",                           # not an object
        b'{"pid": 1, "v": "\xff\xfe"}',         # not UTF-8
    ])
    def test_malformed_files_are_skipped_and_left_alone(self, tmp_path, raw):
        bad = _write(tmp_path, 0, raw=raw, name="mcp-bad.json")
        scan = scan_run_dir(tmp_path, INSTALLED)
        assert scan.servers == [] and scan.removed == 0
        assert scan.skipped == ["mcp-bad.json"]
        assert bad.read_bytes() == raw

    def test_only_mcp_run_files_are_considered(self, tmp_path):
        (tmp_path / "other.json").write_text("{}", encoding="utf-8")
        (tmp_path / f"mcp-{os.getpid()}.json.tmp").write_text("{}", encoding="utf-8")
        scan = scan_run_dir(tmp_path, INSTALLED)
        assert scan.servers == [] and scan.skipped == [] and scan.removed == 0

    def test_never_signals_a_process(self, tmp_path, monkeypatch):
        """Liveness goes through the injected probe (hub.daemon._pid_alive in
        production) — no os.kill here, which on Windows would terminate it."""
        _write(tmp_path, 99999999)
        monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill called"))
        scan = scan_run_dir(tmp_path, INSTALLED, is_alive=lambda pid: True)
        assert [s.pid for s in scan.servers] == [99999999]

    def test_servers_sort_oldest_first(self, tmp_path):
        _write(tmp_path, os.getpid(), started_at="2026-02-01T00:00:00+00:00")
        _write(tmp_path, os.getppid(), started_at="2026-01-01T00:00:00+00:00")
        scan = scan_run_dir(tmp_path, INSTALLED)
        assert [s.pid for s in scan.servers] == [os.getppid(), os.getpid()]


class TestFormatAge:
    NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=30), "0m"), (timedelta(minutes=12), "12m"),
        (timedelta(hours=5, minutes=59), "5h"), (timedelta(days=44, hours=3), "44d"),
    ])
    def test_units(self, delta, expected):
        assert format_age((self.NOW - delta).isoformat(), self.NOW) == expected

    @pytest.mark.parametrize("value", [None, "", "yesterday", "2099-01-01T00:00:00+00:00"])
    def test_unknown_or_future_is_a_question_mark(self, value):
        assert format_age(value, self.NOW) == "?"

    def test_a_naive_timestamp_is_read_as_utc(self):
        assert format_age("2026-10-11T09:00:00", self.NOW) == "3h"


# ── the doctor section ──────────────────────────────────────────────

class TestDoctorSection:
    def _run(self, directory, capsys, installed=INSTALLED):
        _print_mcp_servers(directory, installed)
        return capsys.readouterr().out

    def test_no_directory(self, tmp_path, capsys):
        out = self._run(tmp_path / "run", capsys)
        assert f"MCP servers: {NONE_MSG}" in out

    def test_an_empty_directory(self, tmp_path, capsys):
        (tmp_path / "run").mkdir()
        assert NONE_MSG in self._run(tmp_path / "run", capsys)

    def test_live_current_live_stale_dead_and_malformed(self, tmp_path, capsys):
        d = tmp_path / "run"
        current = _write(d, os.getpid(), INSTALLED, ppid=1111)
        stale = _write(d, os.getppid(), "1.26.1", ppid=2222,
                       started_at=(datetime.now(timezone.utc) - timedelta(days=44)).isoformat())
        dead = _write(d, _dead_pid(), "1.26.1")
        bad = _write(d, 0, raw=b"{oops", name="mcp-garbage.json")

        out = self._run(d, capsys)

        assert f"PID {os.getpid()}  v{INSTALLED}" in out and "parent 1111" in out
        stale_line = next(l for l in out.splitlines() if f"PID {os.getppid()} " in l)
        assert "v1.26.1" in stale_line and "up 44d" in stale_line
        assert "parent 2222" in stale_line and "stale" in stale_line
        assert "stale" not in next(l for l in out.splitlines() if f"PID {os.getpid()} " in l)
        assert "2 MCP servers, 1 running old code — restart those agent sessions" in out
        assert "removed 1 run file(s) of servers that exited" in out
        assert "skipped unreadable run file: mcp-garbage.json" in out
        assert not dead.exists()                            # dead PID: file deleted
        assert current.exists() and stale.exists() and bad.exists()

    def test_all_current_says_zero_old(self, tmp_path, capsys):
        _write(tmp_path, os.getpid(), INSTALLED)
        out = self._run(tmp_path, capsys)
        assert "1 MCP server, 0 running old code" in out
        assert "restart" not in out

    def test_only_dead_servers_reads_as_none_and_cleans_up(self, tmp_path, capsys):
        dead = _write(tmp_path, _dead_pid(), "1.26.1")
        out = self._run(tmp_path, capsys)
        assert NONE_MSG in out and "removed 1 run file(s)" in out
        assert not dead.exists()

    def test_it_only_reports_and_never_signals(self, tmp_path, monkeypatch, capsys):
        import hub.daemon
        _write(tmp_path, os.getppid(), "1.26.1")
        # Doctor must not kill: record every os.kill and allow only the signal-0 probe.
        real_kill, sent = os.kill, []

        def spy(pid, sig):
            sent.append(sig)
            return real_kill(pid, sig)

        monkeypatch.setattr(os, "kill", spy)
        monkeypatch.setattr(hub.daemon, "_IS_WINDOWS", False)
        self._run(tmp_path, capsys)
        assert set(sent) <= {0}


class TestDoctorCommand:
    """The whole ``mool doctor`` in a fresh process with an isolated HOME: stale
    servers are a warning, not a failed check, and the exit code is unchanged."""

    def _doctor(self, home: Path):
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run([sys.executable, "-m", "hub.cli", "doctor"], cwd=_REPO_ROOT,
                              env=env, capture_output=True, text=True, timeout=120)

    @staticmethod
    def _tally(out: str) -> str:
        """Doctor's closing verdict line — "All checks passed." or "N passed, M failed"."""
        return next(l.strip() for l in reversed(out.splitlines())
                    if "passed" in l)

    def test_stale_servers_change_neither_exit_code_nor_the_verdict(self, tmp_path):
        clean_home = tmp_path / "clean"
        clean_home.mkdir()
        baseline = self._doctor(clean_home)

        home = tmp_path / "home"
        run = home / ".moolmesh" / "run"
        _write(run, os.getpid(), "9.9.9")                # stale vs the installed version
        _write(run, os.getppid(), hub.__version__)       # current
        dead = _write(run, _dead_pid(), "9.9.9")
        result = self._doctor(home)

        assert result.returncode == baseline.returncode == 0
        assert self._tally(result.stdout) == self._tally(baseline.stdout)
        assert "MCP servers" in result.stdout
        assert "2 MCP servers, 1 running old code — restart those agent sessions" in result.stdout
        assert "stale — running old code" in result.stdout
        assert not dead.exists()
        assert NONE_MSG in baseline.stdout               # no run dir at all
        assert "Traceback" not in result.stderr
