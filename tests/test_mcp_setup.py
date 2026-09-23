"""Tests for mool mcp setup command."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hub.cli import cmd_mcp_setup

MODULE_ARGS = ["-m", "hub.mcp_server"]


def _make_args(**kwargs):
    import argparse
    defaults = {
        "target": "json",
        "install_mcp": False,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestMcpSetupDetection:
    def test_json_target_prints_config(self, capsys):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="json"))

        out = capsys.readouterr().out
        assert "mcpServers" in out
        assert "moolmesh" in out
        # Extract and validate JSON
        json_start = out.index("{")
        json_end = out.rindex("}") + 1
        parsed = json.loads(out[json_start:json_end])
        server = parsed["mcpServers"]["moolmesh"]
        assert "command" in server
        assert "args" in server

    def test_missing_mcp_shows_install_hint(self, capsys):
        with patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value=None):
            mock_run.side_effect = subprocess.CalledProcessError(1, "python")
            cmd_mcp_setup(_make_args(target="json"))

        out = capsys.readouterr().out
        assert "not installed" in out.lower()
        assert "pip install mcp" in out or "pipx inject" in out

    def test_uv_present_still_uses_module_form_and_checks_mcp(self, capsys):
        with patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value="/usr/bin/uv"):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="json"))

        out = capsys.readouterr().out
        assert "resolved by uv run" not in out.lower()
        # The `import mcp` check runs with this interpreter even when uv exists
        mock_run.assert_any_call(
            [sys.executable, "-c", "import mcp"],
            capture_output=True, check=True, timeout=10,
        )
        server = _parse_printed_json(out)["mcpServers"]["moolmesh"]
        assert server["command"] == sys.executable
        assert server["args"] == MODULE_ARGS


def _parse_printed_json(out: str) -> dict:
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


class TestMcpSetupModuleCommand:
    """#38 — every target gets `[sys.executable, "-m", "hub.mcp_server"]`."""

    def test_json_target(self, capsys):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="json"))

        out = capsys.readouterr().out
        server = _parse_printed_json(out)["mcpServers"]["moolmesh"]
        assert server["command"] == sys.executable
        assert server["args"] == MODULE_ARGS
        assert not any(a.endswith("mcp_server.py") for a in server["args"])
        assert not any("uv" in a for a in server["args"])
        assert f"Server:    {sys.executable} -m hub.mcp_server" in out

    @pytest.mark.parametrize("target,rel", [
        ("claude-desktop", ("Library", "Application Support", "Claude", "claude_desktop_config.json")),
        ("cursor", (".cursor", "mcp.json")),
    ])
    def test_json_config_clients(self, tmp_path, target, rel):
        config_path = tmp_path.joinpath(*rel)
        config_path.parent.mkdir(parents=True)

        with patch("subprocess.run") as mock_run, \
             patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target=target))

        server = json.loads(config_path.read_text())["mcpServers"]["moolmesh"]
        assert server == {"command": sys.executable, "args": MODULE_ARGS}

    def test_opencode(self, tmp_path):
        config_path = tmp_path / ".config" / "opencode" / "opencode.json"
        config_path.parent.mkdir(parents=True)

        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="opencode"))

        block = json.loads(config_path.read_text())["mcp"]["moolmesh"]
        assert block["command"] == [sys.executable] + MODULE_ARGS

    def test_codex_fresh_config(self, tmp_path):
        config_path = tmp_path / ".codex" / "config.toml"
        config_path.parent.mkdir(parents=True)

        with patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="codex"))

        content = config_path.read_text()
        assert "[mcp_servers.moolmesh]" in content
        entry = tomllib.loads(content)["mcp_servers"]["moolmesh"]
        assert entry == {"command": sys.executable, "args": MODULE_ARGS}

    @pytest.mark.parametrize("python", [
        r"C:\Users\x\pipx\venvs\moolmesh\Scripts\python.exe",
        "/home/x/.local/pipx/venvs/moolmesh/bin/python",
    ])
    def test_codex_toml_round_trips(self, tmp_path, python):
        from hub.cli import _write_codex_mcp
        config_path = tmp_path / "config.toml"
        config_path.write_text('model = "o3"\n')
        server_cmd = [python, "-m", "hub.mcp_server"]

        _write_codex_mcp(config_path, server_cmd, _make_args(target="codex"))

        data = tomllib.loads(config_path.read_text())
        assert data["model"] == "o3"
        entry = data["mcp_servers"]["moolmesh"]
        assert entry["command"] == python
        assert entry["args"] == MODULE_ARGS

    def test_codex_dry_run_prints_valid_toml(self, capsys):
        from hub.cli import _write_codex_mcp
        python = r"C:\Users\x\pipx\venvs\moolmesh\Scripts\python.exe"
        _write_codex_mcp(Path("unused.toml"), [python, "-m", "hub.mcp_server"],
                         _make_args(target="codex", dry_run=True))

        out = capsys.readouterr().out
        block = out[out.index("[mcp_servers.moolmesh]"):]
        entry = tomllib.loads(block)["mcp_servers"]["moolmesh"]
        assert entry["command"] == python

    def test_claude_code(self, tmp_path):
        with patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            cmd_mcp_setup(_make_args(target="claude-code"))

        add_calls = [c.args[0] for c in mock_run.call_args_list
                     if c.args and c.args[0][1:3] == ["mcp", "add"]]
        assert len(add_calls) == 1
        assert add_calls[0][-3:] == [sys.executable] + MODULE_ARGS


class TestMcpServerModuleSmoke:
    def test_python_m_hub_mcp_server_imports_hub(self):
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "hub.mcp_server"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, cwd=repo_root,
        )
        assert "No module named 'hub'" not in result.stderr
        try:
            import mcp  # noqa: F401
        except ImportError:
            assert result.returncode == 1
            assert "mcp package not installed" in result.stderr
        else:
            # Starts, then exits cleanly on stdin EOF
            assert result.returncode == 0, result.stderr
            assert "MoolMesh MCP Server starting" in result.stderr


class TestMcpSetupDesktop:
    def test_dry_run_does_not_write(self, tmp_path, capsys):
        appdir = tmp_path / "Library" / "Application Support" / "Claude"
        appdir.mkdir(parents=True)

        with patch("subprocess.run") as mock_run, \
             patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="claude-desktop", dry_run=True))

        assert not (appdir / "claude_desktop_config.json").exists()

    def test_writes_desktop_config(self, tmp_path, capsys):
        appdir = tmp_path / "Library" / "Application Support" / "Claude"
        appdir.mkdir(parents=True)

        with patch("subprocess.run") as mock_run, \
             patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="claude-desktop"))

        config_path = appdir / "claude_desktop_config.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "mcpServers" in data
        assert "moolmesh" in data["mcpServers"]

    def test_warns_when_already_configured(self, tmp_path, capsys):
        appdir = tmp_path / "Library" / "Application Support" / "Claude"
        appdir.mkdir(parents=True)
        config_path = appdir / "claude_desktop_config.json"
        config_path.write_text(json.dumps({
            "mcpServers": {"moolmesh": {"command": "old-python", "args": ["old.py"]}},
        }))

        with patch("subprocess.run") as mock_run, \
             patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="claude-desktop"))

        out = capsys.readouterr().out
        assert "already exists" in out.lower()
        data = json.loads(config_path.read_text())
        assert data["mcpServers"]["moolmesh"]["command"] != "old-python"

    def test_merges_with_existing_config(self, tmp_path, capsys):
        appdir = tmp_path / "Library" / "Application Support" / "Claude"
        appdir.mkdir(parents=True)
        config_path = appdir / "claude_desktop_config.json"
        config_path.write_text(json.dumps({
            "mcpServers": {"other-server": {"command": "node", "args": ["server.js"]}},
            "customSetting": True,
        }))

        with patch("subprocess.run") as mock_run, \
             patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_mcp_setup(_make_args(target="claude-desktop"))

        data = json.loads(config_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "moolmesh" in data["mcpServers"]
        assert data["customSetting"] is True
