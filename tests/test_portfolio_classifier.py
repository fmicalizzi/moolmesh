"""Tests for portfolio classification + grouped read (issue #24, Stage 1).

Covers the 4-category taxonomy (A collapse, B subdir, C materials, D1 config /
D2 orphan), the A>D2 precedence boundary, the filesystem-validated decode that
must not split a hyphenated name, and the grouped read (harness folding at read
time, collapsed_harness count, nested children, recursive masking).
"""

import sqlite3

import pytest

from hub.cache.portfolio_classifier import (
    Anchor,
    anchor_path,
    classify,
    fs_decode,
    index_real_dir,
    path_encode,
)
from hub.cache.workspace_store import WorkspaceStore
from hub.correlation.workspace_resolver import resolve_path
from hub.mcp_server import (
    _get_portfolio_grouped,
    _get_portfolio_production,
    _mask_grouped,
    _portfolio_production,
)

HOME = "/Users/tester"
CLAUDE = f"{HOME}/Downloads/Claude"
UUID = "005784b6-3bf5-423a-a467-6b7d1d86b7a1"


def _classify(kind, dir_path=None, root_path=None, remote_url=None,
              session_cwds=None, enc_index=None):
    return classify(
        kind, remote_url, root_path, dir_path,
        session_cwds=session_cwds or {}, enc_index=enc_index or {}, home=HOME,
    )


# ── path_encode: forward encoding matches Claude's ground truth ──────────

class TestPathEncode:
    def test_slash_and_underscore_both_become_dash(self):
        assert (path_encode(f"{CLAUDE}/_eventsmx/fiestados")
                == "-Users-tester-Downloads-Claude--eventsmx-fiestados")

    def test_hyphenated_name_encodes_stably(self):
        assert (path_encode(f"{CLAUDE}/PRODUCCIONES/LACNIC")
                == "-Users-tester-Downloads-Claude-PRODUCCIONES-LACNIC")


class TestAnchorPath:
    def test_first_meaningful_component_below_container(self):
        assert anchor_path(f"{CLAUDE}/salvadorgimenez/apps/web/src") == f"{CLAUDE}/salvadorgimenez"

    def test_pure_container_is_degenerate(self):
        assert anchor_path(CLAUDE) is None
        assert anchor_path(HOME) is None
        assert anchor_path("/") is None
        assert anchor_path("/tmp") is None


# ── Category A — harness collapse ───────────────────────────────────────

class TestCollapseA:
    def test_scratchpad_collapses_via_session_cwd(self):
        # Primary path: the scratchpad embeds the session uuid; the session's
        # real cwd (a deep dir) resolves to the project anchor.
        d = f"/private/tmp/claude-501/-Users-tester-Downloads-Claude-myproj/{UUID}/scratchpad"
        cwd = f"{CLAUDE}/myproj/deep/nested"
        c = _classify("path_hash", dir_path=d, session_cwds={UUID: cwd})
        assert c.category == "A" and c.role == "collapse"
        assert c.resolved_via == "session_cwd"
        assert c.project_key == f"path_hash:{_hash(f'{CLAUDE}/myproj')}"

    def test_scratchpad_falls_back_to_encode_match(self):
        # No session cwd → exact match of the encoded segment against a known dir.
        d = f"/private/tmp/claude-501/-Users-tester-Downloads-Claude-myproj/{UUID}/scratchpad"
        enc_index: dict = {}
        index_real_dir(f"{CLAUDE}/myproj", enc_index)
        c = _classify("path_hash", dir_path=d, enc_index=enc_index)
        assert c.category == "A" and c.resolved_via == "encode_match"
        assert c.project_key == f"path_hash:{_hash(f'{CLAUDE}/myproj')}"

    def test_session_storage_collapses_via_encode_match(self):
        d = f"{HOME}/.claude/projects/-Users-tester-Downloads-Claude-myproj/memory"
        enc_index: dict = {}
        index_real_dir(f"{CLAUDE}/myproj", enc_index)
        c = _classify("path_hash", dir_path=d, enc_index=enc_index)
        assert c.category == "A" and c.role == "collapse"
        assert c.resolved_via == "encode_match"

    def test_unresolvable_harness_still_collapses_never_orphaned(self):
        d = f"/private/tmp/claude-501/-some-encoded-name/{UUID}/scratchpad"
        c = _classify("path_hash", dir_path=d)  # no cwd, no index, path absent
        assert c.category == "A" and c.role == "collapse"
        # Synthetic 'encoded:' project — a namespace that never collides with a
        # real resolver key (git_remote:/git_root:/path_hash:), so its activity
        # stays folded into its own group instead of being orphaned.
        assert c.project_key.startswith("encoded:") and c.resolved_via == "unresolved"


# ── A > D2 precedence boundary ──────────────────────────────────────────

class TestPrecedence:
    def test_session_storage_is_A_not_home_config(self):
        d = f"{HOME}/.claude/projects/-Users-tester-Downloads-Claude-myproj"
        enc_index: dict = {}
        index_real_dir(f"{CLAUDE}/myproj", enc_index)
        c = _classify("path_hash", dir_path=d, enc_index=enc_index)
        assert c.category == "A"

    def test_claude_plugins_is_D2_tooling_not_A(self):
        d = f"{HOME}/.claude/plugins/cache/openai-codex/codex/1.0.3/scripts"
        c = _classify("path_hash", dir_path=d)
        assert c.role == "orphan" and c.subtype == "home_config"

    def test_bundled_skills_scratch_is_D2_not_A(self):
        d = "/private/tmp/claude-501/bundled-skills/2.1.220/abc/dataviz/references"
        c = _classify("path_hash", dir_path=d)
        assert c.role == "orphan" and c.resolved_via == "tool"


# ── Categories B / C / D1 / D2 + project root ───────────────────────────

class TestNesting:
    def test_git_workspace_is_project_root(self):
        c = _classify("git_remote", root_path="/x/repo", remote_url="github.com/me/repo")
        assert c.category == "root" and c.role == "project"
        assert c.project_key == "git_remote:github.com/me/repo"

    def test_path_hash_project_root(self):
        c = _classify("path_hash", dir_path=f"{CLAUDE}/myproj")
        assert c.category == "root" and c.role == "project"

    def test_deep_subdir_nests_as_B(self):
        c = _classify("path_hash", dir_path=f"{CLAUDE}/myproj/apps/web/src")
        assert c.category == "B" and c.role == "nest" and c.subtype == "subdir"
        assert c.project_key == f"path_hash:{_hash(f'{CLAUDE}/myproj')}"

    def test_materials_folder_nests_as_C(self):
        c = _classify("path_hash", dir_path=f"{CLAUDE}/myproj/reportes/inprocess")
        assert c.category == "C" and c.role == "nest" and c.subtype == "materials"

    def test_ops_folder_nests_as_C(self):
        c = _classify("path_hash", dir_path=f"{CLAUDE}/ddtyi/yaahub-ops/memory")
        assert c.category == "C" and c.subtype == "materials"

    def test_dotfolder_inside_project_is_D1_config(self):
        c = _classify("path_hash", dir_path=f"{CLAUDE}/myproj/.obsidian/plugins")
        assert c.category == "D" and c.role == "nest" and c.subtype == "config"
        assert c.project_key == f"path_hash:{_hash(f'{CLAUDE}/myproj')}"

    def test_home_dotfolder_is_D2_orphan(self):
        c = _classify("path_hash", dir_path=f"{HOME}/.config/opencode")
        assert c.role == "orphan" and c.subtype == "home_config"

    def test_degenerate_root_is_D2_orphan(self):
        for d in (CLAUDE, HOME, "/", "/tmp", "/usr"):
            c = _classify("path_hash", dir_path=d)
            assert c.role == "orphan" and c.subtype == "degenerate", d


# ── fs_decode: hyphenated names must not be split ────────────────────────

class TestFsDecode:
    def test_hyphenated_name_not_split(self, tmp_path):
        # Only `coep-services` exists — never a `coep/services` tree.
        real = tmp_path / "coep-services" / "webinstitucional"
        real.mkdir(parents=True)
        encoded = path_encode(str(real))
        decoded = fs_decode(encoded)
        assert decoded == str(real)
        assert (tmp_path / "coep").exists() is False

    def test_longest_match_wins_over_prefix(self, tmp_path):
        (tmp_path / "coep").mkdir()
        (tmp_path / "coep-services").mkdir()
        encoded = path_encode(str(tmp_path / "coep-services"))
        assert fs_decode(encoded) == str(tmp_path / "coep-services")

    def test_missing_leaf_returns_none_but_parent_decodes(self, tmp_path):
        parent = tmp_path / "realdir"
        parent.mkdir()
        # The walk reaches an existing parent (proves it descends), then the
        # missing leaf — not a dead walk higher up — is what fails.
        assert fs_decode(path_encode(str(parent))) == str(parent)
        assert fs_decode(path_encode(str(parent / "ghost-leaf"))) is None


# ── Grouped read: folding, counts, children, masking ────────────────────

def _mkrepo(root, name, remote=None):
    d = root / name
    (d / ".git").mkdir(parents=True)
    cfg = "[core]\n\tbare = false\n"
    if remote is not None:
        cfg += f'[remote "origin"]\n\turl = {remote}\n'
    (d / ".git" / "config").write_text(cfg)
    return d


def _make_sessions_db(path, rows):
    """rows: (session_id, cwd)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sessions (id TEXT, provider TEXT, cwd TEXT)")
    c.executemany(
        "INSERT INTO sessions (id, provider, cwd) VALUES (?, 'claude', ?)", rows
    )
    c.commit()
    c.close()
    return path


class TestGroupedRead:
    def test_harness_folds_into_project_at_read_time(self, tmp_path):
        repo = _mkrepo(tmp_path, "proj", "git@github.com:me/proj.git")
        events = _make_sessions_db(tmp_path / "events.db", [(UUID, str(repo))])
        store = WorkspaceStore(tmp_path / "workspace.db")
        # A session touched a file in the repo, and its scratchpad harness dir.
        store.record_attribution(
            "sessA", "claude", str(repo / "main.py"),
            resolve_path(str(repo / "main.py")),
        )
        scratch = f"/private/tmp/claude-501/-enc-proj/{UUID}/scratchpad"
        store.record_attribution(
            "sessA", "claude", scratch + "/note.md",
            resolve_path(scratch + "/note.md"),
        )
        store.build_rollup()
        store.classify_workspaces(events)

        g = store.get_portfolio_grouped()
        keys = {p["project_key"] for p in g["projects"]}
        assert "git_remote:github.com/me/proj" in keys
        proj = next(p for p in g["projects"]
                    if p["project_key"] == "git_remote:github.com/me/proj")
        assert proj["collapsed_harness"] == 1          # harness folded, not deleted
        assert proj["session_touches"] >= 2            # repo + harness activity
        assert g["summary"]["collapsed_harness"] == 1
        store.close()

    def test_grouped_empty_when_never_classified(self, tmp_path):
        store = WorkspaceStore(tmp_path / "workspace.db")
        g = store.get_portfolio_grouped()
        assert g == {"projects": [], "unclassified": [],
                     "summary": {"projects": 0, "collapsed_harness": 0,
                                 "children": 0, "unclassified": 0}}
        store.close()


class TestMasking:
    def test_mask_recurses_into_children_and_labels(self):
        grouped = {
            "projects": [{
                "project_key": "git_remote:github.com/me/proj",
                "project_label": "github.com/me/proj",
                "children": [{
                    "workspace_key": "path_hash:abc",
                    "dir_path": "/Users/tester/Downloads/Claude/proj/apps",
                    "remote_url": None, "root_path": None,
                }],
            }],
            "unclassified": [{
                "workspace_key": "path_hash:def",
                "dir_path": "/Users/tester/.config/x",
                "remote_url": None, "root_path": None,
            }],
            "summary": {},
        }
        out = _mask_grouped(grouped, hide=True)
        p = out["projects"][0]
        assert p["project_label"] != "github.com/me/proj"   # masked
        assert p["project_key"] == "git_remote:github.com/me/proj"  # join handle kept
        assert p["children"][0]["dir_path"] is None          # raw name blanked
        assert p["children"][0]["label"]                     # stable masked label
        assert out["unclassified"][0]["dir_path"] is None

    def test_mcp_grouped_absent_db_returns_empty(self, tmp_path):
        assert _get_portfolio_grouped(str(tmp_path / "nope.db")) == {
            "projects": [], "unclassified": [], "summary": {}
        }


def _hash(directory):
    import hashlib
    return hashlib.sha256(directory.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _epoch(y, m, d, h=12):
    """Local-time epoch for a calendar day — matches _local_day bucketing."""
    import datetime
    return datetime.datetime(y, m, d, h, 0, 0).timestamp()


def _make_events_db(path, sessions, events):
    """Minimal events.db: a `sessions` table (cwd for classify) + an `events`
    table (created_at for ingestion dating).

    sessions: (id, provider, cwd). events: (session_id, provider, created_at,
    timestamp_iso). The original `timestamp` is deliberately separable from the
    ingestion `created_at` so a test can prove dating uses created_at.
    """
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE sessions (id TEXT, provider TEXT, cwd TEXT)")
    c.executemany(
        "INSERT INTO sessions (id, provider, cwd) VALUES (?, ?, ?)", sessions
    )
    c.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, project TEXT,
            event_type TEXT, timestamp TEXT, summary TEXT, session_id TEXT,
            created_at REAL NOT NULL)"""
    )
    c.executemany(
        "INSERT INTO events (provider, project, event_type, timestamp, summary,"
        " session_id, created_at) VALUES (?, 'proj', 'user', ?, '', ?, ?)",
        [(prov, ts, sid, ca) for (sid, prov, ca, ts) in events],
    )
    c.commit()
    c.close()
    return path


class TestProduction:
    """Production-over-time metric (issue #24, Stage 2): sessions dated by
    ingestion (MAX events.created_at), aggregated over the canonical
    project_key, segmented by provider, deliverables from path_touches."""

    def _project(self, tmp_path, session_rows, event_rows, touches=None):
        """Build workspace.db (one git project) + events.db from the real
        store pipeline. session_rows feed attributions+sessions; event_rows feed
        the events table. Returns (events_db, workspace_db, project_key)."""
        repo = _mkrepo(tmp_path, "proj", "git@github.com:me/proj.git")
        store = WorkspaceStore(tmp_path / "workspace.db")
        sess_meta = []
        for sid, prov in session_rows:
            store.record_attribution(
                sid, prov, str(repo / f"{sid}.py"),
                resolve_path(str(repo / f"{sid}.py")),
            )
            sess_meta.append((sid, prov, str(repo)))
        for (path, mtime) in (touches or []):
            store.record_touch(
                str(repo / path), mtime,
                resolve_path(str(repo / path)),
            )
        store.build_rollup()
        events_db = _make_events_db(
            tmp_path / "events.db", sess_meta, event_rows
        )
        store.classify_workspaces(events_db)
        store.close()
        return (str(events_db), str(tmp_path / "workspace.db"),
                "git_remote:github.com/me/proj")

    def test_dates_by_ingestion_not_original_timestamp(self, tmp_path):
        # created_at is recent; the original ISO timestamp is months old (as a
        # resumed session would be). Dating MUST use created_at → recent day.
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "2026-01-05T10:00:00")],
        )
        d = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert "2026-09-17" in proj["days"]
        assert "2026-01-05" not in proj["days"]      # original ts ignored
        assert proj["sessions"] == 1

    def test_window_excludes_older_sessions(self, tmp_path):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude"), ("sB", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "x"),
             ("sB", "claude", _epoch(2026, 9, 1), "x")],   # 17 days before today
        )
        d = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert proj["sessions"] == 1                 # only sA is in the 7d window
        assert proj["active_days"] == 1

    def test_aggregates_over_canonical_project_and_segments_by_provider(self, tmp_path):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude"), ("sB", "opencode")],
            [("sA", "claude", _epoch(2026, 9, 17), "x"),
             ("sB", "opencode", _epoch(2026, 9, 17), "x")],
        )
        d = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert proj["sessions"] == 2 and proj["active_days"] == 1
        assert proj["days"]["2026-09-17"] == {"claude": 1, "opencode": 1}
        assert d["providers"] == ["claude", "opencode"]

    def test_deliverables_zero_and_unmeasurable_without_touches(self, tmp_path):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "x")],
        )
        d = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        assert d["deliverables_measurable"] is False
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert proj["deliverables"] == 0

    def test_deliverables_count_image_video_when_touches_present(self, tmp_path):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "x")],
            touches=[("out/final.png", 1.0), ("clip.mp4", 2.0),
                     ("notes.txt", 3.0)],           # .txt is not a deliverable
        )
        d = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        assert d["deliverables_measurable"] is True
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert proj["deliverables"] == 2            # png + mp4, not txt

    def test_labels_masked_on_and_unmasked_off(self, tmp_path):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "x")],
        )
        on = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=True)
        p_on = next(p for p in on["projects"] if p["project_key"] == pk)
        assert p_on["project_label"].startswith("hidden:")
        assert "github.com/me/proj" not in p_on["project_label"]

        off = _portfolio_production(edb, wdb, days=7, today="2026-09-18", hide=False)
        p_off = next(p for p in off["projects"] if p["project_key"] == pk)
        assert p_off["project_label"] == "github.com/me/proj"

    def test_get_wrapper_resolves_hide_flag(self, tmp_path, monkeypatch):
        edb, wdb, pk = self._project(
            tmp_path, [("sA", "claude")],
            [("sA", "claude", _epoch(2026, 9, 17), "x")],
        )
        import hub.mcp_server as m
        monkeypatch.setattr(m, "_hide_project_names", lambda: True)
        # Pin 'today' through the wrapper so the fixture day is inside the window
        # → positively assert the wrapper resolves hide=True and masks a real row.
        d = _get_portfolio_production(edb, wdb, days=7, today="2026-09-18")
        proj = next(p for p in d["projects"] if p["project_key"] == pk)
        assert proj["project_label"].startswith("hidden:")
        assert "github.com/me/proj" not in proj["project_label"]

    def test_absent_db_returns_empty(self, tmp_path):
        empty = {"window_days": 7, "providers": [], "projects": [],
                 "deliverables_measurable": False}
        assert _portfolio_production(
            str(tmp_path / "no.db"), str(tmp_path / "no2.db"),
            days=7, today="2026-09-18") == empty
