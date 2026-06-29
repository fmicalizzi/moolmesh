"""Tests for mool mcp setup command."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hub.cli import cmd_mcp_setup


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
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "python")
            cmd_mcp_setup(_make_args(target="json"))

        out = capsys.readouterr().out
        assert "not installed" in out.lower()
        assert "pip install mcp" in out or "pipx inject" in out


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
