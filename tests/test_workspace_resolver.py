"""Tests for the path→workspace resolver (issue #20 — Workspace axis, Phase A)."""

from pathlib import Path

import pytest

from hub.correlation.workspace_resolver import (
    normalize_remote,
    resolve_path,
    resolve_dir,
    _parse_git_config_remotes,
)


# ── helpers ─────────────────────────────────────────────────────────

def _mkrepo(root: Path, name: str, remote: str | None = None) -> Path:
    """Create a fake git checkout with a real .git/config (no git binary)."""
    d = root / name
    (d / ".git").mkdir(parents=True)
    cfg = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    if remote is not None:
        cfg += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (d / ".git" / "config").write_text(cfg)
    return d


class TestNormalizeRemote:
    @pytest.mark.parametrize("url,expected", [
        ("git@github.com:owner/repo.git", "github.com/owner/repo"),
        ("git@github.com:Owner/Repo.git", "github.com/owner/repo"),  # lowercased
        ("https://github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://github.com/owner/repo", "github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "github.com/owner/repo"),
        ("git@gitlab.com:group/sub/repo.git", "gitlab.com/group/sub/repo"),  # nested groups
        ("https://user@bitbucket.org/team/proj.git", "bitbucket.org/team/proj"),
        ("ssh://git@ssh.github.com:443/owner/repo.git", "ssh.github.com/owner/repo"),  # port
    ])
    def test_canonical_forms_unify(self, url, expected):
        assert normalize_remote(url) == expected

    def test_ssh_and_https_of_same_repo_collapse(self):
        assert normalize_remote("git@github.com:acme/x.git") == \
               normalize_remote("https://github.com/acme/x")

    @pytest.mark.parametrize("url", ["", "   ", "/local/path/repo.git", "file:///x/y", "notaurl"])
    def test_non_remote_returns_none(self, url):
        assert normalize_remote(url) is None


class TestGitConfigParsing:
    def test_tab_indented_config_is_parsed(self):
        """git indents with TAB; configparser would misread this — we must not."""
        text = '[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:a/b.git\n'
        assert _parse_git_config_remotes(text) == {"origin": "git@github.com:a/b.git"}

    def test_prefers_origin_over_other_remotes(self, tmp_path):
        d = tmp_path / "r"
        (d / ".git").mkdir(parents=True)
        (d / ".git" / "config").write_text(
            '[remote "upstream"]\n\turl = git@github.com:up/stream.git\n'
            '[remote "origin"]\n\turl = git@github.com:me/mine.git\n'
        )
        assert resolve_path(str(d / "f.py")).remote_url == "github.com/me/mine"

    def test_comments_ignored(self):
        text = '# a comment\n; another\n[remote "origin"]\n\turl = https://h/o/r\n'
        assert _parse_git_config_remotes(text) == {"origin": "https://h/o/r"}


class TestLadder:
    def test_git_remote_is_most_specific(self, tmp_path):
        r = _mkrepo(tmp_path, "proj", "git@github.com:acme/proj.git")
        (r / "src").mkdir()
        ident = resolve_path(str(r / "src" / "main.py"))
        assert ident.kind == "git_remote"
        assert ident.key == "git_remote:github.com/acme/proj"
        assert ident.remote_url == "github.com/acme/proj"

    def test_git_root_when_no_remote(self, tmp_path):
        r = _mkrepo(tmp_path, "proj", remote=None)
        ident = resolve_path(str(r / "file.txt"))
        assert ident.kind == "git_root"
        assert ident.key == f"git_root:{r}"
        assert ident.root_path == str(r)

    def test_path_hash_when_no_git(self, tmp_path):
        d = tmp_path / "loose" / "dir"
        d.mkdir(parents=True)
        ident = resolve_path(str(d / "note.md"))
        assert ident.kind == "path_hash"
        assert ident.key.startswith("path_hash:")
        assert ident.dir_path == str(d)

    def test_path_hash_for_nonexistent_path(self):
        """path-hash resolves purely from the string, even if nothing exists."""
        ident = resolve_path("/definitely/not/on/disk/xyz/file.py")
        assert ident.kind == "path_hash"
        # deterministic: same input → same key
        assert ident.key == resolve_path("/definitely/not/on/disk/xyz/file.py").key

    def test_nested_repo_resolves_to_closest_marker(self, tmp_path):
        outer = _mkrepo(tmp_path, "outer", "git@github.com:acme/outer.git")
        inner = _mkrepo(outer, "vendor", "https://github.com/acme/inner")
        ident = resolve_path(str(inner / "lib.c"))
        assert ident.remote_url == "github.com/acme/inner"

    def test_git_root_when_config_has_no_url(self, tmp_path):
        d = tmp_path / "r"
        (d / ".git").mkdir(parents=True)
        (d / ".git" / "config").write_text("[core]\n\tbare = false\n")
        ident = resolve_path(str(d / "f"))
        assert ident.kind == "git_root"


class TestGitFileMarker:
    def test_worktree_dotgit_file_follows_gitdir(self, tmp_path):
        """A worktree/submodule writes .git as a *file* → identity keys on the
        worktree path, and the remote is read via the gitdir pointer."""
        real = tmp_path / "realrepo"
        gitdir = real / ".git"
        gitdir.mkdir(parents=True)
        (gitdir / "config").write_text('[remote "origin"]\n\turl = git@github.com:acme/wt.git\n')

        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")

        ident = resolve_path(str(wt / "code.py"))
        assert ident.kind == "git_remote"
        assert ident.remote_url == "github.com/acme/wt"
        # closest marker is the worktree path
        assert ident.root_path == str(wt)


class TestRelativePaths:
    def test_relative_joined_with_cwd(self, tmp_path):
        r = _mkrepo(tmp_path, "proj", "git@github.com:acme/proj.git")
        ident = resolve_path("src/main.py", cwd=str(r))
        assert ident.remote_url == "github.com/acme/proj"

    def test_resolve_dir_matches_resolve_path(self, tmp_path):
        r = _mkrepo(tmp_path, "proj", "git@github.com:acme/proj.git")
        assert resolve_dir(str(r)).key == resolve_path(str(r / "f.py")).key
