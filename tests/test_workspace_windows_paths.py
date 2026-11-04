"""Drive-letter / UNC paths attribute on any host (issue #47.2).

The attribution pass used to accept only ``file_path LIKE '/%'``, so Windows
paths (which the Codex adapter already normalizes with ``ntpath``) were counted
as "skipped non-absolute" and never reached a workspace. The predicate is now
portable; POSIX behavior is unchanged. All paths here are synthetic.
"""

import sqlite3

import pytest

from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import (
    SQL_PORTABLE_ABS,
    _containing_dir,
    is_portable_abs,
    resolve_dir,
)
from tests.test_workspace_attribution import _add_event, _make_events_db
from tests.test_workspace_cwd_fallback import _edges


class TestPortablePredicate:
    @pytest.mark.parametrize("p", [
        "/home/u/x.py", "C:\\work\\app\\main.py", "c:/work/app/main.py",
        "\\\\server\\share\\f.txt",
    ])
    def test_absolute(self, p):
        assert is_portable_abs(p)

    @pytest.mark.parametrize("p", [
        "", "src/x.py", "C:relative.txt", "ls -la", "CC:\\x", "./a",
    ])
    def test_not_absolute(self, p):
        assert not is_portable_abs(p)

    def test_sql_twin_matches_python(self):
        c = sqlite3.connect(":memory:")
        cases = ["/a/b", "C:\\w\\f", "d:/w/f", "\\\\srv\\s\\f", "rel/x",
                 "C:rel", "ls -la", "CC:\\x", "1:\\x"]
        where = SQL_PORTABLE_ABS.format(col="?")
        for p in cases:
            got = bool(c.execute(f"SELECT {where}", (p, p, p)).fetchone()[0])
            assert got == is_portable_abs(p), p
        c.close()


class TestContainingDir:
    def test_windows_file_uses_ntpath(self):
        assert _containing_dir("C:\\work\\app\\src\\main.py") == "C:\\work\\app\\src"

    def test_windows_relative_joined_with_windows_cwd(self):
        assert _containing_dir("src\\a.py", "C:\\work\\app") == "C:\\work\\app\\src"

    def test_posix_unchanged(self):
        assert _containing_dir("/a/b/../c/f.py") == "/a/c"
        assert _containing_dir("x/f.py", "/w") == "/w/x"

    def test_foreign_path_never_walks_the_process_cwd(self, tmp_path, monkeypatch):
        # A relative-looking "C:\..." must not find the .git of the process cwd.
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        ident = resolve_dir("C:\\work\\app")
        assert ident.kind == "path_hash"
        assert ident.dir_path == "C:\\work\\app"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    events = _make_events_db(tmp_path / "events.db")
    s = WorkspaceStore(tmp_path / "workspace.db")
    yield s, events, tmp_path
    s.close()


class TestAttributionPass:
    def test_drive_letter_file_attributes(self, env):
        s, events, _ = env
        _add_event(events, "w1", "C:\\work\\app\\main.py", provider="codex")
        _add_event(events, "w1", "C:\\work\\app\\util.py", provider="codex")
        r = s.attribute_incremental(events)
        assert r["attributed"] == 2
        assert r["skipped_non_absolute"] == 0
        keys = {e[4] for e in _edges(s)}
        assert len(keys) == 1 and next(iter(keys)).startswith("path_hash:")
        assert {e[3] for e in _edges(s)} == {"file"}

    def test_unc_and_posix_side_by_side(self, env):
        s, events, tmp_path = env
        _add_event(events, "w2", "\\\\srv\\share\\doc.txt", provider="codex")
        _add_event(events, "w2", str(tmp_path / "plain" / "f.py"))
        _add_event(events, "w2", "ls -la")  # still skipped
        r = s.attribute_incremental(events)
        assert r["attributed"] == 2
        assert r["skipped_non_absolute"] == 1

    def test_drive_letter_cwd_fallback(self, env):
        s, events, _ = env
        _add_event(events, "w3", None, cwd="C:\\work\\app\\", provider="codex")
        _add_event(events, "w3", None, cwd="C:\\work\\app", provider="codex")
        r = s.attribute_incremental(events)
        assert r["cwd_attributed"] == 1
        (edge,) = _edges(s)
        assert edge[2] == "C:\\work\\app" and edge[3] == "cwd"

    def test_drive_root_cwd_does_not_qualify(self, env):
        s, events, _ = env
        _add_event(events, "w4", None, cwd="C:\\", provider="codex")
        assert s.attribute_incremental(events)["cwd_attributed"] == 0
