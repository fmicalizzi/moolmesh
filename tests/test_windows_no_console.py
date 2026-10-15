"""git/gh subprocesses never open a console window under pythonw (#78)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import hub.git_utils as gu
from hub.config import HubConfig, get_github_token

CREATE_NO_WINDOW = 0x08000000

GIT_CALLS = [
    ("is_git_repo", lambda: gu.is_git_repo("/Users/u/work/proj")),
    ("get_remote_url", lambda: gu.get_remote_url("/Users/u/work/proj")),
    ("git_fetch", lambda: gu.git_fetch("/Users/u/work/proj")),
    ("get_remote_refs", lambda: gu.get_remote_refs("/Users/u/work/proj")),
    ("git_log_range", lambda: gu.git_log_range("/Users/u/work/proj", "a" * 40, "b" * 40)),
    ("git_log_since", lambda: gu.git_log_since("/Users/u/work/proj", "2026-01-01")),
    ("git_log_all", lambda: gu.git_log_all("/Users/u/work/proj")),
]


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return seen


def _gh_token():
    return get_github_token(HubConfig())


@pytest.mark.parametrize("name, call", GIT_CALLS + [("gh auth token", _gh_token)])
def test_windows_children_get_create_no_window(name, call, calls, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    call()
    assert calls, name
    assert all(c.get("creationflags") == CREATE_NO_WINDOW for c in calls), name


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("name, call", GIT_CALLS + [("gh auth token", _gh_token)])
def test_other_platforms_pass_no_creationflags(name, call, platform, calls, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    call()
    assert calls, name
    assert all("creationflags" not in c for c in calls), name


def test_git_fetch_disables_terminal_prompt_and_keeps_environment(calls, monkeypatch):
    monkeypatch.setenv("MOOLMESH_TEST_MARKER", "kept")
    gu.git_fetch("/Users/u/work/proj")
    env = calls[0]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["MOOLMESH_TEST_MARKER"] == "kept"
    assert env["PATH"] == os.environ["PATH"]


def test_git_calls_keep_utf8_decoding(calls, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    gu.git_log_all("/Users/u/work/proj")
    assert calls[0]["encoding"] == "utf-8" and calls[0]["errors"] == "replace"
