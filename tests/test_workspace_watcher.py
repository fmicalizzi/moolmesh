"""Tests for the filesystem workspace watcher (issue #21 — Phase B).

Covers the DoD: a git/agent-less folder becomes visible, excludes are
effective, max_depth is respected, the mtime cursor is incremental (no
re-emit), opt-in (no roots → zero touches), and the events.db hot path is
never touched.
"""

import os
import sqlite3
import time
from pathlib import Path

from hub.cache.workspace_store import WorkspaceStore
from hub.config import WorkspaceRoot
from hub.watchers.workspace_watcher import WorkspaceWatcher


def _root(path: Path, max_depth: int = 6, excludes=None) -> WorkspaceRoot:
    return WorkspaceRoot(path=str(path), max_depth=max_depth, excludes=excludes or [])


def _touch(path: Path, when: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    if when is not None:
        os.utime(path, (when, when))


def test_gitless_folder_becomes_visible(tmp_path):
    """A folder with no .git and no session is made visible by a touch."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "materials"
    _touch(root / "brief.md")

    w = WorkspaceWatcher(store, [_root(root)])
    emitted = w.scan_root(w._roots[0])
    assert emitted == 1

    wss = store.list_workspaces()
    assert len(wss) == 1
    assert wss[0]["touches"] == 1
    # No git → path_hash rung.
    assert wss[0]["kind"] == "path_hash"
    touches = store.get_workspace_touches(wss[0]["workspace_key"])
    assert touches[0]["source"] == "filesystem"
    assert touches[0]["path"].endswith("brief.md")
    store.close()


def test_excludes_are_effective(tmp_path):
    """Built-in default excludes AND a config-supplied extra exclude both prune."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "proj"
    _touch(root / "index.js")
    _touch(root / "node_modules" / "dep" / "index.js")  # default exclude
    _touch(root / ".git" / "config")                     # default exclude (VCS)
    _touch(root / "scratch" / "junk.txt")                # config-supplied exclude

    w = WorkspaceWatcher(store, [_root(root, excludes=["scratch"])])
    emitted = w.scan_root(w._roots[0])
    assert emitted == 1  # only index.js

    all_paths = [
        t["path"]
        for ws in store.list_workspaces()
        for t in store.get_workspace_touches(ws["workspace_key"])
    ]
    assert any(p.endswith("index.js") and "node_modules" not in p for p in all_paths)
    assert not any("node_modules" in p for p in all_paths)
    assert not any("scratch" in p for p in all_paths)  # extra exclude plumbing works
    store.close()


def test_max_depth_respected(tmp_path):
    """A file below max_depth is emitted; a file one level deeper is not."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    # containing-dir depth: at-limit = max_depth, too-deep = max_depth + 1
    _touch(root / "a" / "at_limit.txt")            # dirpath depth 1
    _touch(root / "a" / "b" / "too_deep.txt")      # dirpath depth 2

    w = WorkspaceWatcher(store, [_root(root, max_depth=1)])
    w.scan_root(w._roots[0])

    all_paths = [
        t["path"]
        for ws in store.list_workspaces()
        for t in store.get_workspace_touches(ws["workspace_key"])
    ]
    assert any(p.endswith("at_limit.txt") for p in all_paths)
    assert not any(p.endswith("too_deep.txt") for p in all_paths)
    store.close()


def test_mtime_cursor_no_reemit(tmp_path):
    """A second scan re-emits nothing; only a genuinely new file is picked up."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    # Older than the scan_start-1s cursor, so it will not re-emit next scan.
    _touch(root / "first.txt", when=time.time() - 100)

    w = WorkspaceWatcher(store, [_root(root)])
    assert w.scan_root(w._roots[0]) == 1
    assert w.scan_root(w._roots[0]) == 0  # no change → zero re-emits

    # A newly written file (real, current mtime) is picked up on the next scan.
    _touch(root / "second.txt")
    assert w.scan_root(w._roots[0]) == 1
    store.close()


def test_mtime_cursor_anchored_to_scan_start(tmp_path):
    """The cursor is scan_start-1s, NOT max(mtime seen).

    Discriminates the two policies: file A gets a future mtime (simulating a
    file modified mid-scan). A ``max(mtime)`` cursor would jump to A's mtime and
    then DROP a later file B whose mtime is below A's — the exact touch-loss bug
    the design avoids. With the ``scan_start-1s`` cursor, B is emitted. Under
    ``max(mtime)`` this scan would emit 0.
    """
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    t0 = time.time()
    _touch(root / "a.txt", when=t0 + 10)   # future mtime → "modified mid-scan"

    w = WorkspaceWatcher(store, [_root(root)])
    assert w.scan_root(w._roots[0]) == 1   # A emitted; cursor → scan_start-1s

    _touch(root / "b.txt", when=t0 + 5)    # below A's mtime, above the cursor
    emitted = w.scan_root(w._roots[0])
    assert emitted >= 1                     # max(mtime) policy would give 0
    paths = [
        t["path"]
        for ws in store.list_workspaces()
        for t in store.get_workspace_touches(ws["workspace_key"])
    ]
    assert any(p.endswith("b.txt") for p in paths)
    store.close()


def test_opt_in_no_roots_zero_touches(tmp_path):
    """With no marked roots the watcher observes nothing (strict opt-in)."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    w = WorkspaceWatcher(store, [])
    w.start()  # must be a no-op with no roots
    assert w._thread is None
    assert store.list_workspaces() == []
    store.close()


def test_symlinked_dir_not_followed(tmp_path):
    """A symlinked directory under the root is not descended (loop-safe)."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    target = tmp_path / "outside"
    _touch(target / "secret.txt")
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, root / "link")
    except (OSError, NotImplementedError):
        store.close()
        return  # symlinks unsupported on this platform
    _touch(root / "own.txt")

    w = WorkspaceWatcher(store, [_root(root)])
    w.scan_root(w._roots[0])
    all_paths = [
        t["path"]
        for ws in store.list_workspaces()
        for t in store.get_workspace_touches(ws["workspace_key"])
    ]
    assert any(p.endswith("own.txt") for p in all_paths)
    assert not any("secret.txt" in p for p in all_paths)
    store.close()


def test_events_db_hot_path_untouched(tmp_path):
    """A full watcher cycle leaves events.db byte-for-byte unchanged.

    A regression guard, not a measurement: the watcher is never handed an
    events.db path, so this can only fail if someone later wires EventStore into
    the filesystem layer. The real architectural evidence is the companion
    test_watcher_imports_no_event_store (grep-asserted: no EventStore/SSE import).
    """
    events_db = tmp_path / "events.db"
    c = sqlite3.connect(events_db)
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, x TEXT)")
    c.execute("INSERT INTO events (x) VALUES ('hot')")
    c.commit()
    before_dv = c.execute("PRAGMA data_version").fetchone()[0]
    c.close()
    before_stat = events_db.stat()

    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    _touch(root / "f.txt")
    w = WorkspaceWatcher(store, [_root(root)])
    w.scan_root(w._roots[0])
    w.scan_root(w._roots[0])
    store.close()

    after_stat = events_db.stat()
    c = sqlite3.connect(events_db)
    after_dv = c.execute("PRAGMA data_version").fetchone()[0]
    c.close()
    assert before_dv == after_dv
    assert before_stat.st_mtime == after_stat.st_mtime
    assert before_stat.st_size == after_stat.st_size


def test_watcher_imports_no_event_store():
    """The watcher module must not import the events hot-path store.

    The absence of this import is the cleanest grep-assertable evidence that
    the filesystem layer can never write to events.db / the SSE buffer.
    """
    src = Path(
        "hub/watchers/workspace_watcher.py"
    ).read_text()
    import_lines = [
        ln for ln in src.splitlines()
        if ln.startswith(("import ", "from "))
    ]
    joined = "\n".join(import_lines)
    assert "event_store" not in joined
    assert "EventStore" not in joined
    assert "sse" not in joined.lower()
    # And it is genuinely importable without the events store loaded.
    import hub.watchers.workspace_watcher as mod
    assert mod is not None


def test_mcp_surface_exposes_touches(tmp_path):
    """The MCP read surface returns filesystem touches + a touch count."""
    from hub.mcp_server import _get_workspace_touches, _list_workspaces

    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "materials"
    _touch(root / "notes.md")
    w = WorkspaceWatcher(store, [_root(root)])
    w.scan_root(w._roots[0])
    store.close()

    db = str(tmp_path / "workspace.db")
    wss = _list_workspaces(db)
    assert wss and wss[0]["touches"] == 1
    key = wss[0]["workspace_key"]
    touches = _get_workspace_touches(db, key)
    assert len(touches) == 1
    assert touches[0]["source"] == "filesystem"
    assert touches[0]["path"].endswith("notes.md")


def test_mcp_touches_absent_db_returns_empty():
    """Reading touches before any watcher run yields [] (no explosion)."""
    from hub.mcp_server import _get_workspace_touches
    assert _get_workspace_touches("/nonexistent/workspace.db", "path_hash:abc") == []


def test_touch_self_heals_on_git_appearing(tmp_path):
    """One path → one workspace: adding a .git re-attributes the same path."""
    store = WorkspaceStore(tmp_path / "workspace.db")
    root = tmp_path / "r"
    _touch(root / "f.txt", when=time.time() - 50)

    w = WorkspaceWatcher(store, [_root(root)])
    w.scan_root(w._roots[0])
    first = store.list_workspaces()
    assert first[0]["kind"] == "path_hash"

    # Root gains a git identity; the same file, newer mtime, re-scanned.
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:owner/repo.git\n'
    )
    _touch(root / "f.txt")  # real, current mtime → past the cursor
    w.scan_root(w._roots[0])

    # Exactly one touch row for f.txt, now on the git_remote workspace.
    n = 0
    for ws in store.list_workspaces():
        for t in store.get_workspace_touches(ws["workspace_key"]):
            if t["path"].endswith("f.txt"):
                n += 1
                assert ws["kind"] == "git_remote"
    assert n == 1
    store.close()
