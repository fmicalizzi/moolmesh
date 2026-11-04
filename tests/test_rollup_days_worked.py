"""Rollup views report days worked next to touches (#123, follow-up of #114).

Synthetic fixtures only (``acme`` repos, invented sessions), no network. The
new fields are additive: ``sessions_worked`` / ``days_worked`` (distinct, never
a sum of days) and ``days_worked_basis``; the existing ``session_touches`` /
``active_days`` keep their meaning.
"""
from __future__ import annotations

import pytest

import hub.cache.workspace_store as ws
from hub.cache.portfolio_clients import group_by_client, strip_internal
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_dir
from hub.mcp_server import _get_portfolio
from tests.test_edge_active_days import (
    D, D1, TOKYO, _insert, _make_pre_114, _two_day_session, env,  # noqa: F401
)
from tests.test_workspace_store import _mkrepo


def _build(tmp_path, events):
    s = WorkspaceStore(tmp_path / "workspace.db")
    try:
        s.attribute_incremental(events)
        s.build_rollup()
        s.classify_workspaces(events)
    finally:
        s.close()


def _store(tmp_path):
    return WorkspaceStore(tmp_path / "workspace.db")


def _flat_row(rows, repo):
    key = resolve_dir(str(repo)).key
    return next(r for r in rows if r["workspace_key"] == key)


def _project(grouped, repo):
    key = resolve_dir(str(repo)).key
    return next(p for p in grouped["projects"] if p["project_key"] == key)


def _only_long(events, repo):
    _insert(events, "long", str(repo / "a.py"), f"{D1}T10:00:00Z")
    _insert(events, "long", str(repo / "a.py"), f"{D}T09:00:00Z")


class TestProject:
    def test_session_on_two_days_is_two_days_one_session(self, env):
        tmp_path, events, repo = env
        _only_long(events, repo)
        _build(tmp_path, events)
        s = _store(tmp_path)
        try:
            row = _flat_row(s.get_portfolio(), repo)
            grouped = s.get_portfolio_grouped()
        finally:
            s.close()
        assert row["days_worked"] == 2
        assert row["sessions_worked"] == 1
        assert row["days_worked_basis"] == "edge_active_days"
        p = _project(grouped, repo)
        assert (p["days_worked"], p["sessions_worked"]) == (2, 1)
        # Existing fields keep their meaning: one edge-touch dated by its clock.
        assert p["active_days"] == row["active_days"]
        assert "_worked_set" in p
        strip_internal(grouped)
        assert "_worked_set" not in _project(grouped, repo)

    def test_mcp_get_portfolio_carries_the_fields(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _build(tmp_path, events)
        row = _flat_row(_get_portfolio(str(tmp_path / "workspace.db")), repo)
        assert row["days_worked"] == 2
        assert row["sessions_worked"] == 2      # long + short, never 3
        assert "workspace_id" not in row

    def test_since_window_counts_only_its_days(self, env):
        tmp_path, events, repo = env
        _two_day_session(events, repo)
        _build(tmp_path, events)
        s = _store(tmp_path)
        try:
            row = _flat_row(s.get_portfolio(since=D), repo)
        finally:
            s.close()
        assert (row["days_worked"], row["sessions_worked"]) == (1, 2)


class TestClient:
    def test_shared_session_across_two_projects_is_one(self, env, tmp_path):
        tmp_path, events, repo = env
        api = _mkrepo(tmp_path, "api", "git@github.com:acme/api.git")
        _insert(events, "long", str(repo / "a.py"), f"{D1}T10:00:00Z")
        _insert(events, "long", str(api / "x.py"), f"{D}T10:00:00Z")
        _build(tmp_path, events)
        s = _store(tmp_path)
        try:
            grouped = s.get_portfolio_grouped()
        finally:
            s.close()
        h = group_by_client(grouped["projects"], client_orgs={"acme"},
                            personal_orgs=set(), overrides={})
        assert len(h["clients"]) == 1
        c = h["clients"][0]
        assert c["sessions_worked"] == 1        # union, not 1 + 1
        assert c["days_worked"] == 2            # D-1 (site) ∪ D (api)
        assert c["days_worked_basis"] == "edge_active_days"


class TestFallback:
    def test_other_zone_falls_back_to_the_rollup(self, env, monkeypatch):
        tmp_path, events, repo = env
        _only_long(events, repo)
        _build(tmp_path, events)
        monkeypatch.setattr(ws, "user_timezone", lambda: TOKYO)
        s = _store(tmp_path)
        try:
            row = _flat_row(s.get_portfolio(), repo)
            p = _project(s.get_portfolio_grouped(), repo)
        finally:
            s.close()
        for r in (row, p):
            assert r["days_worked_basis"] == "rollup"
            assert r["sessions_worked"] is None
            assert r["days_worked"] == r["active_days"]
        mrow = _flat_row(_get_portfolio(str(tmp_path / "workspace.db")), repo)
        assert mrow["days_worked_basis"] == "rollup"

    def test_pre_114_db_does_not_break(self, env):
        tmp_path, events, repo = env
        _only_long(events, repo)
        _build(tmp_path, events)
        _make_pre_114(tmp_path)
        import sqlite3
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("DROP TABLE edge_active_days")
        c.commit()
        c.close()
        rows = _get_portfolio(str(tmp_path / "workspace.db"))
        row = _flat_row(rows, repo)
        assert row["days_worked_basis"] == "rollup"
        assert row["days_worked"] == row["active_days"]

    def test_rows_of_a_gone_edge_are_not_counted(self, env):
        tmp_path, events, repo = env
        _only_long(events, repo)
        _build(tmp_path, events)
        import sqlite3
        c = sqlite3.connect(tmp_path / "workspace.db")
        c.execute("DELETE FROM path_attributions")
        c.commit()
        c.close()
        row = _flat_row(_get_portfolio(str(tmp_path / "workspace.db")), repo)
        assert (row["days_worked"], row["sessions_worked"]) == (0, 0)


def test_cli_suffix():
    from hub.cli import _worked_suffix
    assert _worked_suffix({"days_worked_basis": "rollup", "days_worked": 3}) == ""
    assert _worked_suffix({"days_worked_basis": "edge_active_days",
                           "days_worked": 2, "sessions_worked": 1}) \
        == " (2d/1ses worked)"
