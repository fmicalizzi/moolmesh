"""Regresión de robustez Windows — issues #31, #32, #33.

Estos tests protegen contra la recurrencia de tres bugs que sólo se
manifiestan bajo el locale por defecto de Windows (cp1252):

- #32: leer git log con mensajes no-ASCII crasheaba con UnicodeDecodeError.
- #31: imprimir salida no-ASCII (p.ej. '→' en el --help) crasheaba con
  UnicodeEncodeError.
- #33: la dep inline de mcp sin cota superior resolvía mcp 2.x y el error
  real quedaba silenciado como "not installed".

Los guards (monkeypatch/parse) fallan en cualquier plataforma sin el fix; el
test de round-trip con repo real prueba el comportamiento end-to-end.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hub import git_utils


# ── #32 · git log con mensajes no-ASCII ─────────────────────────────

_GIT = shutil.which("git")

# Todos los helpers de git_utils que ejecutan subprocess.
_SUBPROCESS_HELPERS = [
    ("is_git_repo", ("/x",)),
    ("get_remote_url", ("/x",)),
    ("git_fetch", ("/x",)),
    ("get_remote_refs", ("/x",)),
    ("git_log_range", ("/x", "a", "b")),
    ("git_log_since", ("/x", "2020-01-01")),
    ("git_log_all", ("/x",)),
]


@pytest.mark.parametrize("name,args", _SUBPROCESS_HELPERS)
def test_git_helpers_pin_utf8_decoding(monkeypatch, name, args):
    """Guard anti-recurrencia: cada helper pasa encoding/errors explícitos.

    Sin el fix (dependiendo del locale) este test falla en cualquier plataforma.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    getattr(git_utils, name)(*args)

    assert captured.get("encoding") == "utf-8", f"{name} no fija encoding utf-8"
    assert captured.get("errors") == "replace", f"{name} no fija errors=replace"


@pytest.mark.skipif(_GIT is None, reason="git no disponible")
def test_git_log_all_roundtrips_non_ascii(tmp_path: Path):
    """Repo real con acentos/ñ/→ en el mensaje de commit → decodifica bien."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "Ñoño Pérez",
        "GIT_AUTHOR_EMAIL": "n@x.dev",
        "GIT_COMMITTER_NAME": "Ñoño Pérez",
        "GIT_COMMITTER_EMAIL": "n@x.dev",
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, env=env)

    git("init", "-q")
    git("config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("hola", encoding="utf-8")
    git("add", "f.txt")
    subject = "feat: acción con ñ, á y flecha →"
    git("commit", "-q", "-m", subject)

    out = git_utils.git_log_all(str(repo))
    assert subject in out, "el mensaje no-ASCII no se decodificó correctamente"


def test_git_log_failure_is_logged_not_silent(monkeypatch, caplog):
    """Un fallo de git se loguea con contexto en vez de simular '0 commits' (§4)."""

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 128
            stdout = ""
            stderr = "fatal: bad revision"

        return R()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    import logging

    with caplog.at_level(logging.WARNING, logger="hub.GitUtils"):
        result = git_utils.git_log_range("/x", "a", "b")

    assert result is None  # fallo de git → None (distinto de "" = 0 commits, #34)
    assert any("falló" in r.message or "bad revision" in r.getMessage()
               for r in caplog.records), "el fallo de git no se logueó"


# ── #31 · CLI reconfigura stdio a UTF-8 en Windows ──────────────────

class _FakeStream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_cli_reconfigures_stdio_on_win32(monkeypatch):
    from hub import cli

    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)

    cli._configure_stdio_encoding()

    for stream in (out, err):
        assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_cli_noop_off_win32(monkeypatch):
    from hub import cli

    out = _FakeStream()
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.sys, "stdout", out)

    cli._configure_stdio_encoding()

    assert out.calls == []  # no toca stdio fuera de Windows


def test_cli_reconfigure_swallows_stream_errors(monkeypatch):
    """reconfigure que revienta no debe propagar (consola exótica)."""
    from hub import cli

    class Boom:
        def reconfigure(self, **kwargs):
            raise ValueError("no reconfigurable")

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "stdout", Boom())
    monkeypatch.setattr(cli.sys, "stderr", Boom())

    cli._configure_stdio_encoding()  # no raise


# ── #33 · dep mcp acotada + mensaje que distingue el error ──────────

def test_mcp_inline_dep_has_upper_bound():
    """La dep inline PEP 723 de mcp debe tener cota superior <2."""
    src = (Path(__file__).parent.parent / "hub" / "mcp_server.py").read_text(
        encoding="utf-8")
    assert '"mcp>=1.2.0,<2"' in src, \
        "la dep inline de mcp perdió la cota superior <2 (issue #33)"


def test_mcp_message_distinguishes_not_installed_from_incompatible():
    from hub.mcp_server import _mcp_unavailable_message

    not_installed = _mcp_unavailable_message(ModuleNotFoundError("mcp", name="mcp"))
    incompatible = _mcp_unavailable_message(
        ModuleNotFoundError("no fastmcp", name="mcp.server.fastmcp"))

    assert "not installed" in not_installed
    assert "incompatible" in incompatible
    assert "mcp.server.fastmcp" in incompatible
    assert not_installed != incompatible


def test_mcp_message_handles_none():
    from hub.mcp_server import _mcp_unavailable_message

    assert "not installed" in _mcp_unavailable_message(None)
