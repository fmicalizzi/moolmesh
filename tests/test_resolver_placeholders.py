"""#52: the workspace resolver never reads a cloud placeholder under ``.git``."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hub.correlation import workspace_resolver as wr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.invalid/acme/proj.git\n'
    )
    (root / "src").mkdir()
    return root


def _flag(monkeypatch, names: set[str]) -> list[Path]:
    """Flag paths whose basename is in ``names`` as placeholders; spy on reads."""
    monkeypatch.setattr(wr, "is_cloud_placeholder", lambda p: Path(p).name in names)
    read: list[Path] = []
    orig = Path.read_text

    def spy(self, *a, **kw):
        read.append(self)
        return orig(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    return read


def test_config_placeholder_is_not_read(tmp_path, monkeypatch, caplog):
    root = _repo(tmp_path)
    read = _flag(monkeypatch, {"config"})
    with caplog.at_level(logging.DEBUG, logger=wr.__name__):
        ident = wr.resolve_dir(str(root / "src"))
    assert ident.kind == "git_root"
    assert ident.root_path == str(root)
    assert not any(p.name == "config" for p in read)
    assert "cloud placeholder" in caplog.text


def test_dataless_git_dir_is_not_entered(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    read = _flag(monkeypatch, {".git"})
    ident = wr.resolve_dir(str(root / "src"))
    assert ident.kind == "git_root"
    assert read == []


def test_worktree_gitdir_file_placeholder(tmp_path, monkeypatch):
    main = _repo(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    read = _flag(monkeypatch, {".git"})
    ident = wr.resolve_dir(str(wt))
    assert ident.kind == "git_root" and ident.root_path == str(wt)
    assert read == []


def test_commondir_placeholder(tmp_path, monkeypatch):
    main = _repo(tmp_path)
    gd = main / ".git" / "worktrees" / "wt"
    gd.mkdir(parents=True)
    (gd / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gd}\n")
    # Without placeholders the worktree resolves through commondir to the remote.
    assert wr.resolve_dir(str(wt)).kind == "git_remote"
    read = _flag(monkeypatch, {"commondir"})
    ident = wr.resolve_dir(str(wt))
    assert ident.kind == "git_root"
    assert not any(p.name in ("commondir", "config") for p in read)


def test_no_placeholder_still_resolves_remote(tmp_path):
    root = _repo(tmp_path)
    ident = wr.resolve_dir(str(root / "src"))
    assert ident.kind == "git_remote"
    assert ident.remote_url and ident.remote_url.endswith("acme/proj")
