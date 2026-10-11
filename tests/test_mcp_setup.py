"""Tests for mool mcp setup command."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hub.cli import MCP_SPEC, cmd_mcp_setup

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
        # #41: every hint carries the bounded spec (quoted: `>`/`<` are shell syntax)
        assert MCP_SPEC in out
        assert "pip install" in out or "pipx inject" in out

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


# ── #71 · claude-code: dry-run never mutates, `--` before the server command ──

FAKE_CLAUDE = "/fake/bin/claude"


class _FakeClaude:
    """subprocess.run stand-in: records every call and scripts claude's answers."""

    def __init__(self, add_rc=0, add_err="", add_exc=None, remove_rc=0):
        self.add_rc, self.add_err = add_rc, add_err
        self.add_exc, self.remove_rc = add_exc, remove_rc
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if argv[1:3] == ["mcp", "add"]:
            if self.add_exc:
                raise self.add_exc
            return subprocess.CompletedProcess(argv, self.add_rc, "", self.add_err)
        if argv[1:3] == ["mcp", "remove"]:
            return subprocess.CompletedProcess(argv, self.remove_rc, b"", b"")
        return subprocess.CompletedProcess(argv, 0, "", "")  # `import mcp` probe

    def claude(self, verb):
        return [c for c in self.calls
                if c[0] == FAKE_CLAUDE and c[1:3] == ["mcp", verb]]


def _run_claude_code(tmp_path, fake, *, registered=False, dry_run=False):
    """Run `mcp setup claude-code` with a fake claude and an isolated HOME."""
    if registered:
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"moolmesh": {}}}))
    which = lambda name: FAKE_CLAUDE if name == "claude" else None  # noqa: E731
    with patch("subprocess.run", side_effect=fake), \
         patch("shutil.which", side_effect=which), \
         patch("pathlib.Path.home", return_value=tmp_path):
        cmd_mcp_setup(_make_args(target="claude-code", dry_run=dry_run))


EXPECTED_ADD = [FAKE_CLAUDE, "mcp", "add", "--scope", "user", "--transport",
                "stdio", "moolmesh", "--", sys.executable, "-m", "hub.mcp_server"]
MANUAL_ADD = ('claude mcp add --scope user --transport stdio moolmesh -- '
              f'"{sys.executable}" -m hub.mcp_server')


class TestClaudeCodeDryRun:
    def test_dry_run_with_existing_registration_runs_nothing(self, tmp_path, capsys):
        fake = _FakeClaude()
        _run_claude_code(tmp_path, fake, registered=True, dry_run=True)

        assert fake.claude("remove") == [] and fake.claude("add") == []
        assert [c for c in fake.calls if c[0] == FAKE_CLAUDE] == []
        out = capsys.readouterr().out
        assert "Dry run — no changes made" in out
        assert f"claude mcp remove moolmesh -s user" in out
        assert MANUAL_ADD in out
        server = _parse_printed_json(out)["mcpServers"]["moolmesh"]
        assert server == {"command": sys.executable, "args": MODULE_ARGS}

    def test_dry_run_without_registration_shows_only_the_add(self, tmp_path, capsys):
        fake = _FakeClaude()
        _run_claude_code(tmp_path, fake, registered=False, dry_run=True)

        assert [c for c in fake.calls if c[0] == FAKE_CLAUDE] == []
        out = capsys.readouterr().out
        assert "Dry run — no changes made" in out
        assert "claude mcp remove" not in out
        assert MANUAL_ADD in out

    def test_dry_run_leaves_claude_json_untouched(self, tmp_path):
        before = json.dumps({"mcpServers": {"moolmesh": {"command": "old"}}})
        (tmp_path / ".claude.json").write_text(before)
        _run_claude_code(tmp_path, _FakeClaude(), registered=False, dry_run=True)
        assert (tmp_path / ".claude.json").read_text() == before


class TestClaudeCodeAddArgv:
    def test_add_argv_has_double_dash_right_before_the_server_command(self, tmp_path):
        fake = _FakeClaude()
        _run_claude_code(tmp_path, fake)

        (add,) = fake.claude("add")
        assert add == EXPECTED_ADD
        dash = add.index("--")
        assert add[dash + 1] == sys.executable
        assert add.index("-m") > dash
        # every claude option precedes the server name; nothing but `--` follows it
        assert add.index("moolmesh") < dash
        assert dash == add.index("moolmesh") + 1

    def test_registering_via_shows_the_real_command_with_double_dash(self, tmp_path, capsys):
        _run_claude_code(tmp_path, _FakeClaude())
        out = capsys.readouterr().out
        assert f"Registering via: {MANUAL_ADD}" in out

    def test_happy_path_without_registration_is_one_add_and_no_remove(self, tmp_path, capsys):
        fake = _FakeClaude()
        _run_claude_code(tmp_path, fake, registered=False)

        assert len(fake.claude("add")) == 1
        assert fake.claude("remove") == []
        assert "registered globally" in capsys.readouterr().out

    def test_existing_registration_is_removed_before_the_add(self, tmp_path, capsys):
        fake = _FakeClaude()
        _run_claude_code(tmp_path, fake, registered=True)

        verbs = [c[2] for c in fake.calls if c[0] == FAKE_CLAUDE]
        assert verbs == ["remove", "add"]
        assert fake.claude("remove")[0] == [FAKE_CLAUDE, "mcp", "remove",
                                            "moolmesh", "-s", "user"]
        assert "updated globally" in capsys.readouterr().out


class TestClaudeCodeAddFailure:
    @pytest.mark.parametrize("fake", [
        _FakeClaude(add_rc=1, add_err="error: boom"),
        _FakeClaude(add_exc=subprocess.TimeoutExpired("claude", 30)),
        _FakeClaude(add_exc=FileNotFoundError("claude")),
    ], ids=["nonzero-exit", "timeout", "vanished"])
    def test_failed_add_after_remove_warns_prints_manual_command_and_exits_1(
            self, tmp_path, capsys, fake):
        with pytest.raises(SystemExit) as exc:
            _run_claude_code(tmp_path, fake, registered=True)

        assert exc.value.code == 1
        assert len(fake.claude("remove")) == 1
        out = capsys.readouterr().out
        assert "the previous registration was removed and could not be re-added" in out
        assert MANUAL_ADD in out.split("Re-add it manually:")[1]
        assert "--" in MANUAL_ADD and f'"{sys.executable}"' in out
        server = _parse_printed_json(out)["mcpServers"]["moolmesh"]
        assert server["command"] == sys.executable

    def test_failed_add_shows_claudes_own_error(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _run_claude_code(tmp_path, _FakeClaude(add_rc=1, add_err="error: boom"),
                             registered=True)
        assert "error: boom" in capsys.readouterr().out

    def test_failed_add_without_prior_registration_keeps_the_soft_path(
            self, tmp_path, capsys):
        """Nothing was removed → the user lost nothing: warn, show JSON, no exit."""
        fake = _FakeClaude(add_rc=1, add_err="error: boom")
        _run_claude_code(tmp_path, fake, registered=False)

        out = capsys.readouterr().out
        assert fake.claude("remove") == []
        assert "claude mcp add returned: error: boom" in out
        assert "could not be re-added" not in out
        assert "mcpServers" in out

    def test_failed_remove_is_not_reported_as_a_lost_registration(
            self, tmp_path, capsys):
        fake = _FakeClaude(remove_rc=1, add_rc=1, add_err="already exists")
        _run_claude_code(tmp_path, fake, registered=True)  # no SystemExit

        out = capsys.readouterr().out
        assert "Could not remove the existing registration" in out
        assert "could not be re-added" not in out


class TestShellDisplay:
    def test_spec_with_comparison_operators_is_quoted(self):
        from hub.cli import _display_cmd
        assert _display_cmd(["pipx", "inject", "moolmesh", "mcp>=1.2.0,<2"]) == \
            'pipx inject moolmesh "mcp>=1.2.0,<2"'

    def test_plain_and_windows_args_stay_bare_unless_they_have_spaces(self):
        from hub.cli import _display_cmd
        assert _display_cmd(["python", "-m", "pip"]) == "python -m pip"
        assert _display_cmd([r"C:\Users\x\py.exe"]) == r"C:\Users\x\py.exe"
        assert _display_cmd([r"C:\Program Files\py.exe"]) == r'"C:\Program Files\py.exe"'


# ── #41 · uv tool installs: detection, install hint, bounded spec ──────────

POSIX_TOOLS = "/home/u/.local/share/uv/tools"
WIN_TOOLS = r"C:\Users\u\AppData\Roaming\uv\tools"


@pytest.fixture
def prefix(monkeypatch):
    """Point sys.prefix at a fake venv (base_prefix elsewhere ⇒ "in a venv")."""
    def _set(value):
        monkeypatch.setattr(sys, "prefix", value)
        monkeypatch.setattr(sys, "base_prefix", "/usr/local")
    return _set


class TestUvToolDetection:
    def test_receipt_in_prefix_means_uv_tool(self, tmp_path, prefix):
        from hub.cli import _detect_install_method
        env = tmp_path / "uv" / "tools" / "moolmesh"
        env.mkdir(parents=True)
        (env / "uv-receipt.toml").write_text("[tool]\n")
        prefix(str(env))
        with patch("hub.cli._uv_tool_dir",
                   side_effect=AssertionError("receipt is enough")):
            assert _detect_install_method() == "uv-tool"

    def test_has_uv_receipt_checks_the_receipt_file(self, tmp_path):
        from hub.cli import _has_uv_receipt
        assert _has_uv_receipt(str(tmp_path)) is False
        (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
        assert _has_uv_receipt(str(tmp_path)) is True

    def test_windows_prefix_with_receipt(self, prefix):
        from hub.cli import _detect_install_method
        win = WIN_TOOLS + r"\moolmesh"
        prefix(win)
        seen = []
        with patch("hub.cli._has_uv_receipt",
                   side_effect=lambda p: seen.append(p) or p == win):
            assert _detect_install_method() == "uv-tool"
        assert seen == [win]

    def test_receipt_wins_over_a_pipx_substring(self, prefix):
        from hub.cli import _detect_install_method
        prefix("/home/u/pipx-stuff/uv/tools/moolmesh")
        with patch("hub.cli._has_uv_receipt", return_value=True):
            assert _detect_install_method() == "uv-tool"

    @pytest.mark.parametrize("env,tools", [
        (POSIX_TOOLS + "/moolmesh", POSIX_TOOLS),
        (WIN_TOOLS + r"\moolmesh", WIN_TOOLS),
        (WIN_TOOLS.lower() + "\\moolmesh", WIN_TOOLS),       # drive/dir case
        (POSIX_TOOLS + "/moolmesh", POSIX_TOOLS + "/"),      # trailing separator
        (WIN_TOOLS + r"\moolmesh", WIN_TOOLS.replace("\\", "/")),  # mixed seps
    ])
    def test_prefix_inside_uv_tool_dir_without_receipt(self, prefix, env, tools):
        from hub.cli import _detect_install_method
        prefix(env)
        with patch("hub.cli._has_uv_receipt", return_value=False), \
             patch("hub.cli._uv_tool_dir", return_value=tools):
            assert _detect_install_method() == "uv-tool"

    @pytest.mark.parametrize("env,tools", [
        ("/home/u/work/widget/.venv", POSIX_TOOLS),
        (POSIX_TOOLS + "-extra/moolmesh", POSIX_TOOLS),   # sibling, not a child
        (POSIX_TOOLS, POSIX_TOOLS),                       # the dir itself
        (r"C:\work\widget\.venv", WIN_TOOLS),
        ("/home/u/work/widget/.venv", None),              # uv not installed
    ])
    def test_other_venvs_stay_venv(self, prefix, env, tools):
        from hub.cli import _detect_install_method
        prefix(env)
        with patch("hub.cli._has_uv_receipt", return_value=False), \
             patch("hub.cli._uv_tool_dir", return_value=tools):
            assert _detect_install_method() == "venv"

    @pytest.mark.parametrize("env", [
        "/home/u/.local/pipx/venvs/moolmesh",
        r"C:\Users\u\pipx\venvs\moolmesh",
    ])
    def test_pipx_still_detected(self, prefix, env):
        from hub.cli import _detect_install_method
        prefix(env)
        with patch("hub.cli._has_uv_receipt", return_value=False):
            assert _detect_install_method() == "pipx"

    def test_system_python_is_pip_and_never_asks_uv(self, monkeypatch):
        from hub.cli import _detect_install_method
        monkeypatch.setattr(sys, "prefix", "/usr/local")
        monkeypatch.setattr(sys, "base_prefix", "/usr/local")
        with patch("hub.cli._has_uv_receipt", return_value=False), \
             patch("hub.cli._uv_tool_dir",
                   side_effect=AssertionError("not a venv")):
            assert _detect_install_method() == "pip"


class TestUvToolDirLookup:
    def _which(self, found=True):
        return patch("shutil.which", return_value="/usr/bin/uv" if found else None)

    def test_returns_the_printed_directory(self):
        from hub.cli import _uv_tool_dir
        with self._which(), patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, f"{POSIX_TOOLS}\n", "")
            assert _uv_tool_dir() == POSIX_TOOLS
        assert run.call_args.args[0] == ["/usr/bin/uv", "tool", "dir"]

    @pytest.mark.parametrize("effect", [
        FileNotFoundError("uv"),
        subprocess.TimeoutExpired("uv", 10),
        subprocess.CalledProcessError(1, "uv"),
    ])
    def test_uv_errors_are_ignored(self, effect):
        from hub.cli import _uv_tool_dir
        with self._which(), patch("subprocess.run", side_effect=effect):
            assert _uv_tool_dir() is None

    def test_uv_not_installed_or_failing_or_mocked(self):
        from hub.cli import _uv_tool_dir
        with self._which(found=False):
            assert _uv_tool_dir() is None
        with self._which(), patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, "x\n", "boom")
            assert _uv_tool_dir() is None
            run.return_value = MagicMock(returncode=0)     # stdout is not a str
            assert _uv_tool_dir() is None


class TestMcpSpec:
    def test_single_bounded_spec(self):
        assert MCP_SPEC == "mcp>=1.2.0,<2"

    def test_matches_the_server_inline_dependency(self):
        src = (Path(__file__).parent.parent / "hub" / "mcp_server.py").read_text(
            encoding="utf-8")
        assert f'"{MCP_SPEC}"' in src


PY = "/opt/env/bin/python"


class TestInstallPlan:
    def test_pipx(self):
        from hub.cli import _mcp_install_plan
        assert _mcp_install_plan("pipx", PY) == (
            ["pipx", "inject", "moolmesh", MCP_SPEC], None)

    def test_uv_tool_with_uv_installs_into_the_tool_env(self):
        from hub.cli import _mcp_install_plan
        with patch("shutil.which", return_value="/usr/bin/uv"):
            cmd, blocker = _mcp_install_plan("uv-tool", PY)
        assert cmd == ["uv", "pip", "install", "--python", PY, MCP_SPEC]
        assert blocker is None

    def test_uv_tool_without_uv_suggests_reinstall_with(self):
        from hub.cli import _mcp_install_plan
        with patch("shutil.which", return_value=None):
            cmd, blocker = _mcp_install_plan("uv-tool", PY)
        assert cmd == ["uv", "tool", "install", "--with", MCP_SPEC,
                       "moolmesh", "--force"]
        assert "not in PATH" in blocker

    @pytest.mark.parametrize("method", ["venv", "pip"])
    def test_pip_methods_use_python_m_pip(self, method):
        from hub.cli import _mcp_install_plan
        with patch("hub.cli._has_pip", return_value=True):
            assert _mcp_install_plan(method, PY) == (
                [PY, "-m", "pip", "install", MCP_SPEC], None)

    def test_venv_without_pip_explains_instead_of_suggesting_python_m_pip(self):
        from hub.cli import _mcp_install_plan
        with patch("hub.cli._has_pip", return_value=False):
            cmd, blocker = _mcp_install_plan("venv", PY)
        assert cmd == ["uv", "pip", "install", "--python", PY, MCP_SPEC]
        assert "no pip" in blocker and "ensurepip" in blocker


def _setup(monkeypatch_method, install=False, dry_run=False, *, mcp_missing=True,
           pip=True, uv=True, install_exc=None, with_code=False):
    """Run `mcp setup json` with a given install method; return (calls, output).

    With ``with_code=True`` returns ``(calls, output, exit_code)`` instead (0 when
    the command returned normally). Without it a ``SystemExit`` propagates, so a
    test that doesn't expect a failing exit can't hide one (#72).
    """
    import io
    import contextlib
    calls = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        if argv[1:3] == ["-c", "import mcp"]:
            if mcp_missing:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if install_exc:
            raise install_exc
        return subprocess.CompletedProcess(argv, 0, "", "")

    which = lambda name: f"/bin/{name}" if (name == "uv" and uv) else None  # noqa: E731
    buf = io.StringIO()
    code = 0
    with patch("hub.cli._detect_install_method", return_value=monkeypatch_method), \
         patch("hub.cli._has_pip", return_value=pip), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("shutil.which", side_effect=which), \
         contextlib.redirect_stdout(buf):
        try:
            cmd_mcp_setup(_make_args(target="json", install_mcp=install, dry_run=dry_run))
        except SystemExit as exc:
            if not with_code:
                raise
            code = exc.code
    installs = [c for c in calls if c[1:3] != ["-c", "import mcp"]]
    if with_code:
        return installs, buf.getvalue(), code
    return installs, buf.getvalue()


EXPECTED_INSTALL = {
    "pipx": ["pipx", "inject", "moolmesh", MCP_SPEC],
    "uv-tool": ["uv", "pip", "install", "--python", sys.executable, MCP_SPEC],
    "venv": [sys.executable, "-m", "pip", "install", MCP_SPEC],
    "pip": [sys.executable, "-m", "pip", "install", MCP_SPEC],
}


class TestInstallDeps:
    @pytest.mark.parametrize("method", list(EXPECTED_INSTALL))
    def test_hint_shows_the_bounded_spec_quoted(self, method):
        from hub.cli import _display_cmd
        installs, out = _setup(method)
        assert installs == []                       # a hint never installs
        assert MCP_SPEC in out
        assert f"Run:  {_display_cmd(EXPECTED_INSTALL[method])}" in out
        assert f'"{MCP_SPEC}"' in out               # pasteable: > and < are quoted
        assert "Use --install-deps" in out

    @pytest.mark.parametrize("method", list(EXPECTED_INSTALL))
    def test_install_deps_runs_the_method_command(self, method):
        installs, out = _setup(method, install=True)
        assert installs == [EXPECTED_INSTALL[method]]
        assert installs[0][-1] == MCP_SPEC          # one argv item, no literal quotes
        assert not any('"' in a for a in installs[0])
        assert "mcp installed successfully" in out
        assert '"mcpServers"' in out                # carried on to the target

    def test_venv_without_pip_fails_with_the_cause_and_installs_nothing(self):
        installs, out, code = _setup("venv", install=True, pip=False, with_code=True)
        assert code == 1                            # #72: a failed install is not success
        assert installs == []
        assert "cannot install here" in out
        assert "no pip" in out and "uv-created" in out
        assert '"mcpServers"' not in out

    def test_venv_without_pip_hint_explains_without_offering_install_deps(self):
        installs, out = _setup("venv", pip=False)
        assert installs == []
        assert MCP_SPEC in out and "no pip" in out
        assert "Use --install-deps" not in out

    def test_uv_tool_without_uv_on_path(self):
        installs, out, code = _setup("uv-tool", install=True, uv=False, with_code=True)
        assert code == 1
        assert installs == []
        assert "'uv' is not in PATH" in out
        assert f'uv tool install --with "{MCP_SPEC}" moolmesh --force' in out

    def test_install_failure_is_reported_without_traceback(self):
        installs, out, code = _setup(
            "uv-tool", install=True,
            install_exc=subprocess.CalledProcessError(2, ["uv"]), with_code=True)
        assert code == 1
        assert len(installs) == 1 and "Failed" in out
        assert '"mcpServers"' not in out

    def test_missing_installer_binary_says_so(self):
        _, out, code = _setup("pipx", install=True,
                              install_exc=FileNotFoundError("pipx"), with_code=True)
        assert code == 1
        assert "'pipx' not found in PATH" in out

    def test_install_timeout_exits_non_zero_too(self):
        _, out, code = _setup(
            "pipx", install=True, with_code=True,
            install_exc=subprocess.TimeoutExpired(["pipx"], 120))
        assert code == 1
        assert "Failed" in out and '"mcpServers"' not in out

    @pytest.mark.parametrize("method", list(EXPECTED_INSTALL))
    def test_hint_without_install_deps_still_exits_zero(self, method):
        """The hint is informational: only an attempted --install-deps can fail."""
        _, _, code = _setup(method, with_code=True)
        assert code == 0
        _, _, code = _setup("venv", pip=False, with_code=True)   # blocked, but just a hint
        assert code == 0

    def test_successful_install_deps_exits_zero(self):
        _, _, code = _setup("pipx", install=True, with_code=True)
        assert code == 0

    def test_dry_run_with_install_deps_runs_nothing_but_shows_the_plan(self):
        installs, out = _setup("uv-tool", install=True, dry_run=True)
        assert installs == []
        assert "Dry run — would run:" in out and MCP_SPEC in out
        assert '"mcpServers"' in out                # registration plan still shown

    def test_mcp_present_skips_everything(self):
        installs, out = _setup("uv-tool", install=True, mcp_missing=False)
        assert installs == [] and "not installed" not in out


# ── #72 · UTF-8 client configs; never overwrite what can't be decoded ──────

import hashlib


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


BLOCK = {"command": "/opt/u/venv/bin/python", "args": ["-m", "hub.mcp_server"]}
OC_BLOCK = {"type": "local", "command": ["/opt/u/venv/bin/python", "-m", "hub.mcp_server"],
            "enabled": True}
# Non-ASCII on purpose: names and paths users really have.
NON_ASCII = {"mcpServers": {"otro-ñandú": {"command": "/Users/u/Proyección/bin/srv"}},
             "nota": "configuración — año 2026 ✓"}
BAD_BYTES = b'{"mcpServers": {"x": "\xff\xfe\x81 not utf-8"}}'


def _write_utf8(path: Path, data: dict) -> None:
    path.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _run_writer(writer, *a):
    """Run a config writer, returning (exit_code, stdout)."""
    import contextlib
    import io
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            writer(*a)
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue()


def _args(**kw):
    return _make_args(**kw)


class TestUserConfigEncoding:
    def test_json_writer_keeps_non_ascii_content_intact(self, tmp_path):
        from hub.cli import _write_mcp_json_config
        cfg = tmp_path / "mcp.json"
        _write_utf8(cfg, NON_ASCII)

        code, _ = _run_writer(_write_mcp_json_config, cfg, BLOCK, "Cursor", _args())

        assert code == 0
        written = json.loads(cfg.read_text(encoding="utf-8"))
        assert written["nota"] == NON_ASCII["nota"]
        assert written["mcpServers"]["otro-ñandú"] == NON_ASCII["mcpServers"]["otro-ñandú"]
        assert written["mcpServers"]["moolmesh"] == BLOCK

    def test_opencode_writer_keeps_non_ascii_content_intact(self, tmp_path):
        from hub.cli import _write_opencode_mcp
        cfg = tmp_path / "opencode.json"
        _write_utf8(cfg, {"mcp": {"ñandú": {"type": "local"}}, "tema": "oscuro ✓"})

        code, _ = _run_writer(_write_opencode_mcp, cfg, OC_BLOCK, _args())

        assert code == 0
        written = json.loads(cfg.read_text(encoding="utf-8"))
        assert written["tema"] == "oscuro ✓" and "ñandú" in written["mcp"]
        assert written["mcp"]["moolmesh"] == OC_BLOCK

    def test_codex_writer_appends_to_a_non_ascii_toml(self, tmp_path):
        from hub.cli import _write_codex_mcp
        cfg = tmp_path / "config.toml"
        cfg.write_bytes('model = "gpt"\n# configuración de año ✓\n'.encode("utf-8"))

        code, _ = _run_writer(_write_codex_mcp, cfg, [BLOCK["command"], *BLOCK["args"]], _args())

        assert code == 0
        parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert parsed["model"] == "gpt"
        assert parsed["mcp_servers"]["moolmesh"]["args"] == BLOCK["args"]
        assert "configuración de año ✓" in cfg.read_text(encoding="utf-8")

    def test_writers_pass_utf8_explicitly(self, tmp_path, monkeypatch):
        """Every read/write of a client config names its encoding: the locale
        default (cp1252 on Windows) would corrupt non-ASCII content."""
        from hub.cli import _write_codex_mcp, _write_mcp_json_config, _write_opencode_mcp
        calls = []
        real_read, real_write = Path.read_text, Path.write_text

        def read(self, *a, **kw):
            calls.append(("read", kw.get("encoding")))
            return real_read(self, *a, **kw)

        def write(self, data, *a, **kw):
            calls.append(("write", kw.get("encoding")))
            return real_write(self, data, *a, **kw)

        monkeypatch.setattr(Path, "read_text", read)
        monkeypatch.setattr(Path, "write_text", write)
        j, o, t = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.toml"
        for f in (j, o):
            _write_utf8(f, {})
        t.write_text("", encoding="utf-8")
        calls.clear()

        _run_writer(_write_mcp_json_config, j, BLOCK, "Cursor", _args())
        _run_writer(_write_opencode_mcp, o, OC_BLOCK, _args())
        _run_writer(_write_codex_mcp, t, [BLOCK["command"], *BLOCK["args"]], _args())

        assert {k for k, _ in calls} == {"read", "write"}
        assert all(enc == "utf-8" for _, enc in calls), calls

    def test_claude_code_check_reads_non_ascii_json(self, tmp_path, monkeypatch):
        from hub.cli import _claude_code_has_mcp_user
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        _write_utf8(tmp_path / ".claude.json",
                    {"mcpServers": {"moolmesh": {}, "ñandú": {}}, "x": "año ✓"})
        assert _claude_code_has_mcp_user("claude", "moolmesh") is True
        assert _claude_code_has_mcp_user("claude", "ñandú") is True
        assert _claude_code_has_mcp_user("claude", "absent") is False

    def test_claude_code_check_survives_invalid_bytes(self, tmp_path, monkeypatch):
        from hub.cli import _claude_code_has_mcp_user
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / ".claude.json").write_bytes(BAD_BYTES)
        assert _claude_code_has_mcp_user("claude", "moolmesh") is False   # no traceback


class TestUndecodableConfigIsNeverOverwritten:
    """A config we can't read is the user's, not ours to rewrite: leave it
    byte-for-byte, show the block to add by hand, exit non-zero."""

    @pytest.mark.parametrize("content", [BAD_BYTES, b'{"mcpServers": {broken'],
                             ids=["invalid-utf8", "invalid-json"])
    def test_json_clients(self, tmp_path, content):
        from hub.cli import _write_mcp_json_config
        cfg = tmp_path / "mcp.json"
        cfg.write_bytes(content)
        before = _sha(cfg)

        code, out = _run_writer(_write_mcp_json_config, cfg, BLOCK, "Cursor", _args())

        assert code == 1
        assert _sha(cfg) == before
        assert "Cannot parse" in out and "Add manually" in out
        assert '"mcpServers"' in out and BLOCK["command"] in out     # the block to paste

    @pytest.mark.parametrize("content", [BAD_BYTES, b'{"mcp": {broken'],
                             ids=["invalid-utf8", "invalid-json"])
    def test_opencode(self, tmp_path, content):
        from hub.cli import _write_opencode_mcp
        cfg = tmp_path / "opencode.json"
        cfg.write_bytes(content)
        before = _sha(cfg)

        code, out = _run_writer(_write_opencode_mcp, cfg, OC_BLOCK, _args())

        assert code == 1
        assert _sha(cfg) == before
        assert "Cannot parse" in out
        assert '"mcp"' in out and "hub.mcp_server" in out            # manual block (new)

    def test_codex_toml(self, tmp_path):
        from hub.cli import _write_codex_mcp
        cfg = tmp_path / "config.toml"
        cfg.write_bytes(b'model = "\xff\xfe\x81"\n')
        before = _sha(cfg)

        code, out = _run_writer(_write_codex_mcp, cfg, [BLOCK["command"], *BLOCK["args"]], _args())

        assert code == 1
        assert _sha(cfg) == before
        assert "Cannot read" in out
        assert "[mcp_servers.moolmesh]" in out                       # the TOML entry, not JSON
        assert "mcpServers" not in out

    def test_dry_run_still_touches_nothing_and_exits_zero(self, tmp_path):
        from hub.cli import _write_mcp_json_config
        cfg = tmp_path / "mcp.json"
        cfg.write_bytes(BAD_BYTES)
        before = _sha(cfg)
        code, _ = _run_writer(_write_mcp_json_config, cfg, BLOCK, "Cursor", _args(dry_run=True))
        assert code == 0 and _sha(cfg) == before
