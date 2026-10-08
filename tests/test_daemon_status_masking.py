"""``mool daemon status`` masks repo names under ``hide_project_names``."""

from __future__ import annotations

import json
from types import SimpleNamespace

import hub.cli as cli
import hub.config as config_mod
import hub.daemon as daemon_mod

REPOS = [
    SimpleNamespace(owner="acme", repo="secret-app"),
    SimpleNamespace(owner="acme", repo="internal-tools"),
]


def _config(hide: bool):
    return SimpleNamespace(repos=REPOS, hide_project_names=hide)


def _daemon(monkeypatch, hide: bool) -> None:
    monkeypatch.setattr(
        daemon_mod, "daemon_status",
        lambda: {"pid": 1, "uptime_seconds": 5, "log_size": 0},
    )
    monkeypatch.setattr(config_mod, "load_config", lambda: _config(hide))

    def _no_health(*_a, **_k):
        raise OSError("no daemon")

    monkeypatch.setattr("urllib.request.urlopen", _no_health)


def test_text_status_masks_repo_names(monkeypatch, capsys):
    _daemon(monkeypatch, hide=True)
    assert cli._print_daemon_status() == 0
    out = capsys.readouterr().out
    assert "acme/secret-app" not in out
    assert "acme/internal-tools" not in out
    assert "hidden:" in out


def test_text_status_shows_names_when_not_hiding(monkeypatch, capsys):
    _daemon(monkeypatch, hide=False)
    cli._print_daemon_status()
    out = capsys.readouterr().out
    assert "acme/secret-app" in out


def test_json_status_masks_repo_names(monkeypatch, capsys):
    _daemon(monkeypatch, hide=True)
    assert cli._print_daemon_status_json() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["repos"] and all(r.startswith("hidden:") for r in data["repos"])


def test_json_status_shows_names_when_not_hiding(monkeypatch, capsys):
    _daemon(monkeypatch, hide=False)
    cli._print_daemon_status_json()
    data = json.loads(capsys.readouterr().out)
    assert data["repos"] == ["acme/secret-app", "acme/internal-tools"]
