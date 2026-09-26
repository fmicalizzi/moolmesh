"""Shared fixtures for tests."""

# ── Home isolation (#44) — MUST run before anything imports the `hub` package ──
#
# Several modules derive ~/.moolmesh paths AT IMPORT TIME (config.CONFIG_DIR,
# log.LOG_DIR, daemon.CONFIG_DIR, the stores' DEFAULT_DB_PATH, mcp_server's DB
# constants), and DashboardServer() opens the default stores. A suite run with
# the developer's real HOME therefore opened — and migrated — the real
# ~/.moolmesh/*.db. Point HOME (and USERPROFILE, which Path.home() and
# expanduser use on Windows) at a throwaway directory before the first `hub`
# import, so every import-time and runtime home lookup lands there.
#
# Tests that read real data on purpose are opt-in (MOOLMESH_REAL_DATA_TESTS=1)
# and get the captured real home via MOOLMESH_TEST_REAL_HOME — read-only.
import atexit
import os
import shutil
import sys
import tempfile

if any(m == "hub" or m.startswith("hub.") for m in sys.modules):
    raise RuntimeError(
        "tests/conftest.py: the `hub` package was imported before the home "
        "override — tests would touch the real ~/.moolmesh. Import order broke."
    )

REAL_HOME = os.path.expanduser("~")
TEST_HOME = tempfile.mkdtemp(prefix="moolmesh-test-home-")
os.environ["MOOLMESH_TEST_REAL_HOME"] = REAL_HOME
os.environ["HOME"] = TEST_HOME
os.environ["USERPROFILE"] = TEST_HOME
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)

import logging
import signal
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _home_derived_paths() -> dict[str, str]:
    """Every import-time ~/.moolmesh path the package computes."""
    from hub import config, daemon, log, mcp_server
    from hub.cache import event_store, git_store, workspace_store

    return {
        "config.CONFIG_DIR": str(config.CONFIG_DIR),
        "log.LOG_DIR": str(log.LOG_DIR),
        "daemon.CONFIG_DIR": str(daemon.CONFIG_DIR),
        "event_store.DEFAULT_DB_PATH": str(event_store.DEFAULT_DB_PATH),
        "GitStore.DEFAULT_DB_PATH": str(git_store.GitStore.DEFAULT_DB_PATH),
        "WorkspaceStore.DEFAULT_DB_PATH": str(
            workspace_store.WorkspaceStore.DEFAULT_DB_PATH
        ),
        "mcp_server.EVENTS_DB": mcp_server.EVENTS_DB,
        "mcp_server.GITHUB_DB": mcp_server.GITHUB_DB,
        "mcp_server.WORKSPACE_DB": mcp_server.WORKSPACE_DB,
    }


def _outside_test_home() -> dict[str, str]:
    root = os.path.realpath(TEST_HOME)
    return {
        name: path
        for name, path in _home_derived_paths().items()
        if os.path.commonpath([root, os.path.realpath(path)]) != root
    }


@pytest.fixture(scope="session", autouse=True)
def _guard_isolated_home():
    """Fail the whole run if any store/config/log path escaped the temp home."""
    leaked = _outside_test_home()
    if leaked:
        pytest.exit(
            f"home isolation broken — paths outside {TEST_HOME}: {leaked}",
            returncode=3,
        )
    yield

# Silenciar logging durante tests para no contaminar pytest output
logging.getLogger("hub").addHandler(logging.NullHandler())
logging.getLogger("hub").setLevel(logging.CRITICAL)


# ── Per-test wall-clock guardrail (stdlib-only, zero-dep) ──
#
# A test that leaks a subprocess/pipe or blocks on a socket can hang the whole
# suite — in CI that means the "Run tests" step runs to the 6h job timeout and
# silently blocks the release (see the v1.8.5/v1.9.0 preflight hangs). Rather
# than add `pytest-timeout` (would break the zero-dep invariant), we arm a
# SIGALRM around each test's full protocol (setup + call + teardown) and turn a
# hang into a *named* failure instead of an indefinite block.
#
# SIGALRM is Unix-only: guard on it so this is a transparent no-op on Windows
# (and any platform without it), and only interrupts the main thread.
_TEST_TIMEOUT_SECONDS = 60

if hasattr(signal, "SIGALRM"):

    class TestTimeout(Exception):
        """Raised by the SIGALRM guardrail when a test exceeds its time budget."""

    def _on_test_timeout(signum, frame):
        raise TestTimeout(
            f"Test exceeded the {_TEST_TIMEOUT_SECONDS}s wall-clock guardrail "
            "(likely a hung subprocess, unclosed pipe, or blocking socket)."
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        previous = signal.signal(signal.SIGALRM, _on_test_timeout)
        signal.alarm(_TEST_TIMEOUT_SECONDS)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def claude_sample_path() -> Path:
    return FIXTURES_DIR / "claude_sample.jsonl"


@pytest.fixture
def sample_user_dict() -> dict:
    return {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/Users/test/project-alpha",
        "sessionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "version": "2.1.42",
        "type": "user",
        "message": {"role": "user", "content": "Show me the main config file"},
        "uuid": "u001",
        "timestamp": "2026-03-15T10:00:00.000Z",
    }


@pytest.fixture
def sample_assistant_dict() -> dict:
    return {
        "parentUuid": "u001",
        "isSidechain": False,
        "cwd": "/Users/test/project-alpha",
        "sessionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read the config file for you."},
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "Read",
                    "input": {"file_path": "/Users/test/project-alpha/config.yaml"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 45,
                "cache_creation_input_tokens": 8000,
                "cache_read_input_tokens": 0,
            },
        },
        "type": "assistant",
        "uuid": "u002",
        "timestamp": "2026-03-15T10:00:02.500Z",
    }
