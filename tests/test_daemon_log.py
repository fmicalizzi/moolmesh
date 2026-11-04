"""Bounded daemon.log and a clean child environment (#76)."""

from __future__ import annotations

import os
import sys

import pytest

from hub import daemon


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "daemon.log"
    monkeypatch.setattr(daemon, "LOG_FILE", path)
    monkeypatch.setattr(daemon, "LOG_MAX_BYTES", 1000)
    return path


class TestRotation:
    def test_big_log_is_rotated(self, log):
        log.write_bytes(b"x" * 1500)
        assert daemon.rotate_log_if_needed() is True
        assert not log.exists()
        assert (log.parent / "daemon.log.1").read_bytes() == b"x" * 1500

    def test_small_log_is_left_alone(self, log):
        log.write_bytes(b"x" * 999)
        assert daemon.rotate_log_if_needed() is False
        assert log.read_bytes() == b"x" * 999
        assert not (log.parent / "daemon.log.1").exists()

    def test_previous_generation_is_replaced(self, log):
        (log.parent / "daemon.log.1").write_bytes(b"old")
        log.write_bytes(b"new" * 500)
        assert daemon.rotate_log_if_needed() is True
        assert (log.parent / "daemon.log.1").read_bytes() == b"new" * 500
        assert len(list(log.parent.glob("daemon.log*"))) == 1

    def test_missing_log_is_fine(self, log):
        assert daemon.rotate_log_if_needed() is False

    def test_default_threshold_is_10_mb(self, monkeypatch):
        monkeypatch.undo()  # drop the fixture-less patches, read the real constants
        assert daemon.LOG_MAX_BYTES == 10 * 1024 * 1024
        assert daemon.LOG_CHECK_INTERVAL == 600.0

    def test_daemonize_rotates_at_start(self, log, monkeypatch):
        log.write_bytes(b"x" * 1500)
        monkeypatch.setattr(daemon, "_run_foreground", lambda *a, **k: 1)
        daemon.daemonize("localhost", 5200, None, None, foreground=True)
        assert (log.parent / "daemon.log.1").exists() and not log.exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fd re-pointing is Unix-only")
class TestReopenStdio:
    def test_writer_on_the_rotated_file_is_repointed(self, log):
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, b"a" * 1500)
            assert daemon.rotate_log_if_needed() is True
            assert daemon.reopen_stdio(fds=(fd,)) is True
            os.write(fd, b"after\n")
        finally:
            os.close(fd)
        assert log.read_bytes() == b"after\n"
        assert (log.parent / "daemon.log.1").read_bytes() == b"a" * 1500

    def test_other_descriptors_are_untouched(self, log, tmp_path):
        other = tmp_path / "terminal.txt"
        fd = os.open(str(other), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            log.write_bytes(b"a" * 1500)
            daemon.rotate_log_if_needed()
            assert daemon.reopen_stdio(fds=(fd,)) is False
            os.write(fd, b"still here")
        finally:
            os.close(fd)
        assert other.read_bytes() == b"still here"


class TestChildEnvironment:
    def test_malloc_stack_logging_variables_are_removed(self):
        env = {"PATH": "/usr/bin", "MallocStackLogging": "0",
               "MallocStackLoggingNoCompact": "1", "HOME": "/Users/u"}
        removed = daemon.scrub_child_env(env)
        assert sorted(removed) == ["MallocStackLogging", "MallocStackLoggingNoCompact"]
        assert env == {"PATH": "/usr/bin", "HOME": "/Users/u"}

    def test_daemonize_scrubs_the_environment_children_inherit(self, log, monkeypatch):
        monkeypatch.setenv("MallocStackLogging", "0")
        seen = {}
        monkeypatch.setattr(daemon, "_run_foreground",
                            lambda *a, **k: seen.update(os.environ) or 1)
        daemon.daemonize("localhost", 5200, None, None, foreground=True)
        assert "MallocStackLogging" not in seen
        assert "MallocStackLogging" not in os.environ
        assert daemon._scrubbed_env == ["MallocStackLogging"]


class TestForegroundUnderPythonw:
    def test_missing_stdio_goes_to_the_log(self, log, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "moolmesh.pid")
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        def fake_server(*a, **k):
            print("serving")
            sys.stdout.flush()
        monkeypatch.setattr(daemon, "_run_server", fake_server)
        daemon._run_foreground("localhost", 5200, None, None)
        handle = sys.stdout
        monkeypatch.undo()
        handle.close()
        assert "serving" in log.read_text()
