"""``mool mcp setup antigravity`` (issue #77.2).

Writes ``~/.gemini/config/mcp_config.json`` → ``mcpServers.moolmesh`` (stdio),
idempotently. A JSONC file (comments) is never rewritten: clear message, the
manual block, exit 1. HOME is a temp dir; the mcp import probe is mocked.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from hub.cli import cmd_mcp_setup
from tests.test_mcp_setup import MODULE_ARGS, _make_args

EXPECTED = {"command": sys.executable, "args": MODULE_ARGS}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _cfg(home):
    return home / ".gemini" / "config" / "mcp_config.json"


def _run(**kw):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        cmd_mcp_setup(_make_args(target="antigravity", **kw))


def test_parser_accepts_antigravity_target():
    import hub.cli as cli
    with patch.object(sys, "argv", ["mool", "mcp", "setup", "antigravity", "--dry-run"]), \
         patch.object(cli, "cmd_mcp_setup") as fake:
        cli.main()
    assert fake.call_args[0][0].target == "antigravity"


def test_fresh_home_creates_config(home, capsys):
    _run()
    data = json.loads(_cfg(home).read_text())
    assert data == {"mcpServers": {"moolmesh": EXPECTED}}
    assert "Written to" in capsys.readouterr().out


def test_idempotent_second_run_does_not_rewrite(home, capsys):
    _run()
    os.utime(_cfg(home), ns=(1, 1))
    _run()
    assert _cfg(home).stat().st_mtime_ns == 1  # untouched
    assert "Already configured" in capsys.readouterr().out


def test_merges_with_other_servers(home):
    _cfg(home).parent.mkdir(parents=True)
    _cfg(home).write_text(json.dumps(
        {"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    _run()
    data = json.loads(_cfg(home).read_text())
    assert data["keep"] == 1
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["moolmesh"] == EXPECTED


def test_stale_entry_is_updated(home, capsys):
    _cfg(home).parent.mkdir(parents=True)
    _cfg(home).write_text(json.dumps(
        {"mcpServers": {"moolmesh": {"command": "/old/python", "args": []}}}))
    _run()
    assert json.loads(_cfg(home).read_text())["mcpServers"]["moolmesh"] == EXPECTED
    assert "Updated" in capsys.readouterr().out


def test_jsonc_file_is_left_untouched(home, capsys):
    _cfg(home).parent.mkdir(parents=True)
    original = '{\n  // my servers\n  "mcpServers": {}\n}\n'
    _cfg(home).write_text(original)
    with pytest.raises(SystemExit) as exc:
        _run()
    assert exc.value.code == 1
    assert _cfg(home).read_text() == original
    out = capsys.readouterr().out
    assert "JSONC" in out
    assert '"moolmesh"' in out and "mcpServers" in out


def test_unexpected_structure_is_left_untouched(home, capsys):
    _cfg(home).parent.mkdir(parents=True)
    _cfg(home).write_text('{"mcpServers": []}')
    with pytest.raises(SystemExit):
        _run()
    assert _cfg(home).read_text() == '{"mcpServers": []}'


def test_dry_run_writes_nothing(home, capsys):
    _run(dry_run=True)
    assert not _cfg(home).exists()
    assert "mcp_config.json" in capsys.readouterr().out


def test_target_flag_is_an_alias_of_the_positional():
    import hub.cli as cli
    with patch.object(sys, "argv", ["mool", "mcp", "setup", "--target", "antigravity"]), \
         patch.object(cli, "cmd_mcp_setup") as fake:
        cli.main()
    assert fake.call_args[0][0].target == "antigravity"
