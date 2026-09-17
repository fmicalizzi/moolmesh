"""Path → workspace resolver (MoolMesh #20 — Workspace axis, Phase A).

A pure, read-only resolver. Given a file path (and, for the rare relative
path, an optional ``cwd``), it walks up the directory tree to attribute the
file to the *workspace* that owns it — the project identity, independent of the
name of the session directory. This is what recovers the M:N session↔project
relationship: one session touches N files → N workspaces.

Identity ladder, resolving to the most specific (closest) marker:

  1. ``git_remote`` — nearest ancestor ``.git`` with a usable remote →
     canonical ``host/owner/repo``. Survives move/rename/clone because the
     identity is the remote, not the checkout location.
  2. ``git_root``   — nearest ancestor ``.git`` without a usable remote →
     the git root path ("exists, but no git-remote").
  3. ``path_hash``  — no ``.git`` found → a hash of the *containing directory
     string*. Computed purely from the path, so it resolves even for paths
     that no longer exist on disk.

Zero dependencies (stdlib only). It does **not** spawn ``git``: it reads
``.git/config`` directly, so it works even when the git binary is absent. It
never writes anything — the only filesystem access is ``stat``/``read`` while
walking up looking for a ``.git`` marker.

NOTE: git's ``.git/config`` indents keys with a leading TAB, and
``configparser`` treats leading-whitespace lines as value continuations, which
misparses real configs. We therefore hand-roll a tiny line parser instead of
using ``configparser`` (deliberate deviation from the issue's suggestion).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceIdentity:
    """The workspace a path was attributed to.

    ``key`` is the stable, unique identity (a ``workspaces.workspace_key``).
    The evidence fields are populated per rung: ``remote_url`` for git_remote,
    ``root_path`` for git_remote/git_root, ``dir_path`` for path_hash.
    """

    key: str
    kind: str  # git_remote | git_root | path_hash
    remote_url: str | None = None
    root_path: str | None = None
    dir_path: str | None = None


def normalize_remote(url: str) -> str | None:
    """Normalize a git remote URL to a canonical ``host/path`` (lowercased).

    Unifies the SCP-like and URL forms so that the same repository resolves to
    one identity regardless of transport:

        git@github.com:owner/repo.git        → github.com/owner/repo
        https://github.com/owner/repo.git    → github.com/owner/repo
        https://github.com/owner/repo        → github.com/owner/repo
        ssh://git@github.com/owner/repo.git   → github.com/owner/repo
        git@gitlab.com:group/sub/repo.git     → gitlab.com/group/sub/repo

    Returns ``None`` for anything that is not a host-based remote (e.g. a local
    filesystem path used as a remote), which makes the caller fall back to the
    git_root rung.
    """
    if not url:
        return None
    url = url.strip()

    host: str | None = None
    path: str | None = None

    # SCP-like: [user@]host:path  (no scheme, single colon before the path)
    m = re.match(r"^(?:[\w.+-]+@)?([\w.-]+):(?!/)(.+)$", url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # scheme://[user@]host[:port]/path
        m = re.match(r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if m:
            host, path = m.group(1), m.group(2)

    if not host or not path:
        return None

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path:
        return None

    return f"{host.lower()}/{path.lower()}"


def _parse_git_config_remotes(text: str) -> dict[str, str]:
    """Extract ``{remote_name: url}`` from a ``.git/config`` body.

    Hand-rolled to tolerate git's tab-indented keys (see module docstring).
    """
    remotes: dict[str, str] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            m = re.match(r'\[remote\s+"([^"]+)"\]', line)
            current = m.group(1) if m else None
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "url":
                remotes[current] = value.strip()
    return remotes


def _remote_from_config(config_path: Path) -> str | None:
    """Read the preferred remote URL from a git config file.

    Prefers ``origin``; otherwise the first remote in file order (deterministic).
    """
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    remotes = _parse_git_config_remotes(text)
    if not remotes:
        return None
    if "origin" in remotes:
        return remotes["origin"]
    return next(iter(remotes.values()))


def _locate_git_config(marker: Path) -> Path | None:
    """Given a ``.git`` marker (dir or file), find its ``config`` file.

    A normal repo has ``.git/`` as a directory. Worktrees and submodules write
    ``.git`` as a file containing ``gitdir: <path>``; the shared config lives at
    the common dir, which we follow via the ``commondir`` pointer.
    """
    try:
        if marker.is_dir():
            cfg = marker / "config"
            return cfg if cfg.exists() else None
    except OSError:
        return None

    # ``.git`` is a file → follow the gitdir pointer.
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"gitdir:\s*(.+)", text)
    if not m:
        return None
    gitdir = Path(m.group(1).strip())
    if not gitdir.is_absolute():
        gitdir = marker.parent / gitdir
    gitdir = Path(os.path.normpath(str(gitdir)))

    cfg = gitdir / "config"
    if cfg.exists():
        return cfg

    # Worktree: the real config is at commondir/config.
    commondir_file = gitdir / "commondir"
    try:
        if commondir_file.exists():
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            common = Path(rel) if os.path.isabs(rel) else gitdir / rel
            common = Path(os.path.normpath(str(common)))
            cfg = common / "config"
            if cfg.exists():
                return cfg
    except OSError:
        pass
    return None


def _find_git_marker(start: Path) -> tuple[Path, Path] | None:
    """Walk up from ``start`` to the nearest ancestor containing a ``.git``.

    Returns ``(owner_dir, marker_path)`` where ``owner_dir`` is the directory
    that holds the ``.git`` (the closest git root / worktree path), or ``None``
    if the filesystem root is reached with no marker. Non-existent leading
    components are simply skipped — the walk finds the nearest *existing*
    ancestor that is a git root, which is what recovers attribution for files
    whose leaf directory was deleted.
    """
    cur = start
    while True:
        marker = cur / ".git"
        try:
            if marker.exists():
                return cur, marker
        except OSError:
            pass
        parent = cur.parent
        if parent == cur:  # filesystem root
            return None
        cur = parent


def _containing_dir(file_path: str, cwd: str | None = None) -> str:
    """Absolute, normalized containing directory of ``file_path``.

    ``file_path`` is treated as a file (the Read/Edit/Write target); its parent
    is the container. Relative paths are joined with ``cwd`` when available.
    Pure string math — never touches disk, so it works for vanished paths.
    """
    p = file_path
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    p = os.path.normpath(p)
    return os.path.dirname(p)


def resolve_dir(container: str) -> WorkspaceIdentity:
    """Resolve a *containing directory* to a workspace identity via the ladder.

    Split from :func:`resolve_path` so a batch pass can memoize per directory
    (many files share a directory; many directories share a git root).
    """
    found = _find_git_marker(Path(container))
    if found is not None:
        owner_dir, marker = found
        config = _locate_git_config(marker)
        if config is not None:
            url = _remote_from_config(config)
            if url:
                canon = normalize_remote(url)
                if canon:
                    return WorkspaceIdentity(
                        key=f"git_remote:{canon}",
                        kind="git_remote",
                        remote_url=canon,
                        root_path=str(owner_dir),
                    )
        # A .git exists but has no usable remote → identity is the git root.
        return WorkspaceIdentity(
            key=f"git_root:{owner_dir}",
            kind="git_root",
            root_path=str(owner_dir),
        )

    # No .git anywhere above → hash the containing directory string.
    digest = hashlib.sha256(container.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return WorkspaceIdentity(
        key=f"path_hash:{digest}",
        kind="path_hash",
        dir_path=container,
    )


def resolve_path(file_path: str, cwd: str | None = None) -> WorkspaceIdentity:
    """Resolve a single file path to the workspace that owns it."""
    return resolve_dir(_containing_dir(file_path, cwd))
