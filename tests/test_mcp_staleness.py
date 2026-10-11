"""Stale MCP-server detection (#70).

An agent session keeps its ``hub.mcp_server`` alive until it closes, so after
``pipx upgrade`` it keeps serving the code it started with. The server now
remembers what it started as, re-reads what is installed on every call, and
says so (``get_server_info``, a ``stale_server`` key on dict results, one
warning in the log) and registers itself in ``~/.moolmesh/run`` for
``mool doctor`` (see ``test_mcp_registry.py``).

``mcp`` is not installed in the test environment, so the tool layer is
exercised through a stub ``mcp.server.fastmcp`` (identity decorators that record
what gets registered) loaded under an alias — the real module is never reloaded.
"""

import importlib.util
import inspect
import logging
import os
import runpy
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hub import mcp_registry, mcp_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_PY = _REPO_ROOT / "hub" / "mcp_server.py"


# ── helpers ─────────────────────────────────────────────────────────

def _fake_dist(root: Path, version: str) -> Path:
    """A ``moolmesh-<version>.dist-info`` — what pip leaves in site-packages."""
    d = root / f"moolmesh-{version}.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: moolmesh\nVersion: {version}\n", encoding="utf-8")
    return d


@pytest.fixture
def server(monkeypatch):
    """hub.mcp_server with a fixed running version and a patchable installed one."""
    monkeypatch.setattr(mcp_server, "RUNNING_VERSION", "1.26.2")
    monkeypatch.setattr(mcp_server, "_stale_logged", False)
    installed = {"v": "1.26.2"}
    monkeypatch.setattr(mcp_server, "_installed_version", lambda: installed["v"])
    return types.SimpleNamespace(mod=mcp_server, installed=installed)


def _load_with_stub_fastmcp(monkeypatch, *, accepts_instructions=True):
    """Execute hub/mcp_server.py under an alias against a recording FastMCP stub.

    Returns ``(module, registry)``; ``registry`` holds ``init`` (the FastMCP
    constructor args), ``tools`` / ``resources`` (name → function as registered)
    and ``ran`` (``run(transport=...)`` calls).
    """
    registry = {"init": None, "tools": {}, "resources": {}, "ran": []}

    class StubFastMCP:
        def __init__(self, name, **kwargs):
            if kwargs and not accepts_instructions:
                raise TypeError("unexpected keyword argument 'instructions'")
            registry["init"] = (name, kwargs)

        def tool(self):
            def deco(fn):
                registry["tools"][fn.__name__] = fn
                return fn
            return deco

        def resource(self, uri):
            def deco(fn):
                registry["resources"][uri] = fn
                return fn
            return deco

        def run(self, transport=None):
            registry["ran"].append(transport)

    for name in ("mcp", "mcp.server", "mcp.server.fastmcp"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["mcp.server.fastmcp"].FastMCP = StubFastMCP

    spec = importlib.util.spec_from_file_location("_mcp_server_stubbed", _SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, registry


def _set_home(monkeypatch, home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# ── installed version: re-read on every call ────────────────────────

class TestInstalledVersion:
    def test_upgrade_is_seen_without_restarting_the_process(self, tmp_path, monkeypatch):
        """The real upgrade shape: pip removes the old dist-info and creates the new
        one. No ``invalidate_caches`` between the two reads — that is the point."""
        old = _fake_dist(tmp_path, "1.0.0")
        monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
        assert mcp_server._installed_version() == "1.0.0"

        shutil.rmtree(old)
        _fake_dist(tmp_path, "2.0.0")

        assert mcp_server._installed_version() == "2.0.0"

    def test_in_place_metadata_rewrite_is_seen(self, tmp_path, monkeypatch):
        d = _fake_dist(tmp_path, "1.0.0")
        monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
        assert mcp_server._installed_version() == "1.0.0"

        (d / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: moolmesh\nVersion: 3.0.0\n", encoding="utf-8")

        assert mcp_server._installed_version() == "3.0.0"

    def test_a_missing_distribution_is_unverifiable_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "path", [str(tmp_path)])      # no moolmesh anywhere
        assert mcp_server._installed_version() is None

    def test_never_raises_whatever_the_lookup_does(self, monkeypatch):
        def boom(name):
            raise RuntimeError("half-finished upgrade")
        monkeypatch.setattr("importlib.metadata.version", boom)
        assert mcp_server._installed_version() is None

    def test_empty_version_is_unverifiable(self, monkeypatch):
        monkeypatch.setattr("importlib.metadata.version", lambda name: "")
        assert mcp_server._installed_version() is None


# ── _staleness ──────────────────────────────────────────────────────

class TestStaleness:
    def test_same_version_is_none(self, server):
        assert server.mod._staleness() is None

    def test_different_version_describes_both_sides(self, server):
        server.installed["v"] = "1.27.0"
        stale = server.mod._staleness()
        assert stale == {
            "running": "1.26.2",
            "installed": "1.27.0",
            "started_at": server.mod.STARTED_AT,
            "hint": "This MoolMesh MCP server is running old code. Restart this agent "
                    "session (or its MCP server) to load the installed version.",
        }

    def test_unverifiable_installed_version_is_none(self, server):
        server.installed["v"] = None
        assert server.mod._staleness() is None

    def test_a_dev_running_version_cannot_be_compared(self, server, monkeypatch):
        monkeypatch.setattr(server.mod, "RUNNING_VERSION", "dev")
        server.installed["v"] = "1.27.0"
        assert server.mod._staleness() is None

    def test_started_at_is_iso_utc(self):
        from datetime import datetime
        parsed = datetime.fromisoformat(mcp_server.STARTED_AT)
        assert parsed.utcoffset().total_seconds() == 0

    def test_running_version_is_the_package_version_at_import(self):
        import hub
        assert mcp_server.RUNNING_VERSION == hub.__version__


class TestWarnsOncePerProcess:
    def test_one_warning_however_many_times_it_is_detected(self, server, caplog):
        server.installed["v"] = "1.27.0"
        with caplog.at_level(logging.WARNING, logger="moolmesh.mcp_server"):
            for _ in range(5):
                server.mod._staleness()
            server.mod._get_server_info()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "1.26.2" in warnings[0].getMessage() and "1.27.0" in warnings[0].getMessage()

    def test_no_warning_while_versions_match(self, server, caplog):
        with caplog.at_level(logging.WARNING, logger="moolmesh.mcp_server"):
            server.mod._staleness()
            server.mod._get_server_info()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_the_warning_waits_for_the_first_detection(self, server, caplog):
        """Fresh at first, stale after an upgrade: the one warning fires then."""
        with caplog.at_level(logging.WARNING, logger="moolmesh.mcp_server"):
            server.mod._staleness()
            server.installed["v"] = "1.27.0"
            server.mod._staleness()
            server.mod._staleness()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ── get_server_info ─────────────────────────────────────────────────

class TestGetServerInfo:
    KEYS = {"running_version", "installed_version", "stale", "started_at", "pid"}

    def test_fresh(self, server):
        info = server.mod._get_server_info()
        assert set(info) == self.KEYS
        assert info["running_version"] == info["installed_version"] == "1.26.2"
        assert info["stale"] is False
        assert info["pid"] == os.getpid()
        assert info["started_at"] == server.mod.STARTED_AT

    def test_stale(self, server):
        server.installed["v"] = "1.27.0"
        info = server.mod._get_server_info()
        assert self.KEYS <= set(info)
        assert info["stale"] is True
        assert (info["running_version"], info["installed_version"]) == ("1.26.2", "1.27.0")
        assert "Restart" in info["hint"]

    def test_unverifiable_reports_null_and_not_stale(self, server):
        server.installed["v"] = None
        info = server.mod._get_server_info()
        assert info["installed_version"] is None
        assert info["stale"] is False


# ── the MCP tool layer ──────────────────────────────────────────────

class TestToolLayer:
    @pytest.fixture
    def loaded(self, monkeypatch):
        module, registry = _load_with_stub_fastmcp(monkeypatch)
        monkeypatch.setattr(module, "RUNNING_VERSION", "1.26.2")
        installed = {"v": "1.26.2"}
        monkeypatch.setattr(module, "_installed_version", lambda: installed["v"])
        return types.SimpleNamespace(mod=module, tools=registry["tools"],
                                     registry=registry, installed=installed)

    def test_get_server_info_is_registered_and_tells_the_agent_when_to_call_it(self, loaded):
        tool = loaded.tools["get_server_info"]
        assert "get_server_info" in loaded.tools
        assert inspect.signature(tool).parameters == {}
        assert "raros" in tool.__doc__ or "inconsistent" in tool.__doc__
        info = tool()
        assert {"running_version", "installed_version", "stale", "started_at", "pid"} <= set(info)

    def test_fastmcp_gets_an_instructions_line_about_get_server_info(self, loaded):
        name, kwargs = loaded.registry["init"]
        assert name == "moolmesh"
        assert kwargs["instructions"] == (
            "If results look inconsistent, call get_server_info: "
            "a stale server must be restarted.")

    def test_an_sdk_without_instructions_still_imports(self, monkeypatch):
        """mcp>=1.2.0 is the floor; a TypeError from FastMCP must not break the import."""
        module, registry = _load_with_stub_fastmcp(monkeypatch, accepts_instructions=False)
        assert registry["init"] == ("moolmesh", {})
        assert module._mcp is not None and "get_server_info" in registry["tools"]

    def test_dict_tools_are_exactly_the_stale_aware_ones(self, loaded):
        """Any tool returning a dict carries the flag; no list tool is wrapped."""
        dict_tools = {n for n, f in loaded.tools.items()
                      if str(f.__annotations__.get("return", "")).startswith("dict")}
        wrapped = {n for n, f in loaded.tools.items() if hasattr(f, "__wrapped__")}
        # get_server_info reports staleness itself; it is not a wrapped data tool.
        assert dict_tools - {"get_server_info"} == wrapped
        assert wrapped == {"get_project_activity", "get_session_detail",
                           "get_portfolio_grouped", "get_portfolio_production"}

    def test_wrapping_keeps_name_signature_and_docstring_for_fastmcp(self, loaded):
        fn = loaded.tools["get_portfolio_production"]
        assert fn.__name__ == "get_portfolio_production"
        assert list(inspect.signature(fn).parameters) == ["days"]
        assert "Producción por proyecto" in fn.__doc__

    @pytest.mark.parametrize("tool,target,args,payload", [
        ("get_project_activity", "_get_project_activity", ("x",), {"events": 3}),
        ("get_session_detail", "_get_session_detail", ("s1",), {"session_id": "s1"}),
        ("get_portfolio_grouped", "_get_portfolio_grouped", (), {"projects": []}),
        ("get_portfolio_production", "_get_portfolio_production", (), {"projects": []}),
    ])
    def test_flag_appears_only_when_the_server_is_stale(
            self, loaded, monkeypatch, tool, target, args, payload):
        monkeypatch.setattr(loaded.mod, target, lambda *a, **k: dict(payload))
        fn = loaded.tools[tool]

        assert fn(*args) == payload                       # fresh: untouched, no key
        assert "stale_server" not in fn(*args)

        loaded.installed["v"] = "1.27.0"
        result = fn(*args)
        assert result["stale_server"]["running"] == "1.26.2"
        assert result["stale_server"]["installed"] == "1.27.0"
        assert "Restart" in result["stale_server"]["hint"]
        assert {k: v for k, v in result.items() if k != "stale_server"} == payload

        loaded.installed["v"] = None                      # unverifiable: no flag
        assert "stale_server" not in fn(*args)

    def test_the_not_found_fallback_of_get_session_detail_carries_it_too(self, loaded, monkeypatch):
        monkeypatch.setattr(loaded.mod, "_get_session_detail", lambda *a, **k: None)
        loaded.installed["v"] = "1.27.0"
        result = loaded.tools["get_session_detail"]("nope")
        assert result["error"] == "Session nope not found"
        assert result["stale_server"]["installed"] == "1.27.0"

    def test_list_tools_keep_their_type_even_when_stale(self, loaded, monkeypatch):
        monkeypatch.setattr(loaded.mod, "_get_recent_events", lambda *a, **k: [{"id": 1}])
        loaded.installed["v"] = "1.27.0"
        assert loaded.tools["get_recent_events"]() == [{"id": 1}]


# ── the run file: written at startup, never at import ───────────────

class TestRunFile:
    def test_importing_the_module_registers_nothing(self, tmp_path, monkeypatch):
        home = _set_home(monkeypatch, tmp_path / "home")
        _load_with_stub_fastmcp(monkeypatch)
        assert not (home / ".moolmesh" / "run").exists()

    def test_startup_writes_the_file_before_serving(self, tmp_path, monkeypatch):
        home = _set_home(monkeypatch, tmp_path / "home")
        seen = {}

        def run(self, transport=None):
            seen["files"] = sorted(p.name for p in (home / ".moolmesh" / "run").iterdir())

        _load_with_stub_fastmcp(monkeypatch)
        sys.modules["mcp.server.fastmcp"].FastMCP.run = run
        monkeypatch.setattr("atexit.register", lambda *a, **k: None)   # keep pytest's own atexit clean
        runpy.run_path(str(_SERVER_PY), run_name="__main__")

        assert seen["files"] == [f"mcp-{os.getpid()}.json"]

    def test_file_content(self, tmp_path, monkeypatch):
        import json
        home = _set_home(monkeypatch, tmp_path / "home")
        monkeypatch.setattr("atexit.register", lambda *a, **k: None)
        path = mcp_server._register_run_file()
        assert path == home / ".moolmesh" / "run" / f"mcp-{os.getpid()}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {
            "pid": os.getpid(), "ppid": os.getppid(),
            "version": mcp_server.RUNNING_VERSION,
            "started_at": mcp_server.STARTED_AT,
            "python": sys.executable,
        }

    def test_write_is_atomic_tmp_then_replace(self, tmp_path, monkeypatch):
        calls = []
        real_replace = os.replace

        def spy(src, dst, *a, **k):
            # at replace time the content is complete under the tmp name, and the
            # final name does not exist yet — a reader can never see half a file
            assert Path(src).name.endswith(".tmp") and Path(src).read_text(encoding="utf-8")
            assert not Path(dst).exists()
            calls.append((Path(src).name, Path(dst).name))
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", spy)
        path = mcp_registry.write_run_file("1.26.2", "2026-01-01T00:00:00+00:00", tmp_path)

        assert calls == [(f"mcp-{os.getpid()}.json.tmp", f"mcp-{os.getpid()}.json")]
        assert path.exists()
        assert [p.name for p in tmp_path.iterdir()] == [path.name]     # no tmp left behind

    def test_a_failed_replace_leaves_no_tmp_and_raises_oserror(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise PermissionError("denied")
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            mcp_registry.write_run_file("1.26.2", "t", tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_atexit_hook_removes_the_file(self, tmp_path, monkeypatch):
        _set_home(monkeypatch, tmp_path / "home")
        hooks = []
        monkeypatch.setattr("atexit.register", lambda fn, *a, **k: hooks.append((fn, a)))
        path = mcp_server._register_run_file()
        assert path.exists() and len(hooks) == 1
        fn, args = hooks[0]
        assert fn is mcp_registry.remove_run_file and args == (path,)
        fn(*args)
        assert not path.exists()
        fn(*args)                                   # idempotent: atexit must never raise

    def test_a_real_process_cleans_up_after_itself_on_exit(self, tmp_path):
        """No mocks: a fresh interpreter registers, exits normally, atexit deletes."""
        home = tmp_path / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
        code = ("import hub.mcp_server as m; p = m._register_run_file(); "
                "assert p is not None and p.exists(); print(p)")
        out = subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, env=env,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        written = Path(out.stdout.strip().splitlines()[-1])
        assert written.parent == home / ".moolmesh" / "run"
        assert not written.exists()                 # gone: atexit ran at interpreter exit

    def test_a_write_error_is_logged_and_does_not_raise(self, tmp_path, monkeypatch, caplog):
        home = _set_home(monkeypatch, tmp_path / "home")
        (home / ".moolmesh").mkdir()
        (home / ".moolmesh" / "run").write_text("a file where the dir should be")
        hooks = []
        monkeypatch.setattr("atexit.register", lambda fn, *a, **k: hooks.append(fn))

        with caplog.at_level(logging.WARNING, logger="moolmesh.mcp_server"):
            assert mcp_server._register_run_file() is None

        assert any("run file" in r.getMessage() for r in caplog.records)
        assert hooks == []                          # nothing to clean up

    def test_a_registry_failure_does_not_stop_the_server_from_starting(
            self, tmp_path, monkeypatch):
        home = _set_home(monkeypatch, tmp_path / "home")
        (home / ".moolmesh").mkdir()
        (home / ".moolmesh" / "run").write_text("blocked")
        _load_with_stub_fastmcp(monkeypatch)
        calls = []
        sys.modules["mcp.server.fastmcp"].FastMCP.run = (
            lambda self, transport=None: calls.append(transport))

        runpy.run_path(str(_SERVER_PY), run_name="__main__")

        assert calls == ["stdio"]                   # the server still started
