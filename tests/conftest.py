"""Shared fixtures for tests."""

import logging
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Silenciar logging durante tests para no contaminar pytest output
logging.getLogger("hub").addHandler(logging.NullHandler())
logging.getLogger("hub").setLevel(logging.CRITICAL)


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
