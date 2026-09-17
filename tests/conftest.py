"""Shared fixtures for tests."""

import logging
import signal
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

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
