"""Client/org hierarchy over the portfolio — MoolMesh #29 (Unit 3, epic #26).

A pure **read-layer** projection that hangs a third tier ABOVE the Stage-1
project grouping: **client/org → project → materials/subdirs**. The projects and
their nested children are already built by ``WorkspaceStore.get_portfolio_grouped``
(Stage 1, #24); this module only decides, per project, WHICH client it belongs to
and folds effort/outcome/state up to the client node. It never touches the
resolver/watcher/``events.db``/``github.db`` writes, adds no dependency, and runs
entirely over data already in the grouped payload (+ a read-only auto-seed scan).

The client attribution **ladder** (first match wins), per project:

  1. **Manual override** (``client_overrides``: ``project_key → client``) — for
     the borders the automatic rungs get wrong.
  2. **Git-remote owner (primary)** — ``github.com/<org>/<repo>`` → org ``<org>``:
       * org ∈ ``personal_orgs`` → **personal**: the project stays LOOSE (no
         client node — the owner's own org, decided not to group).
       * org ∈ ``client_orgs`` → a **client node** grouping the org's projects.
       * otherwise → the **externos / referencia** drawer (cloned deps/repos:
         homebrew, alfio-event, …).
  3. **Parent-folder convention (gitless fallback)** — no remote: the first
     meaningful folder below the container prefix (``~/Downloads/Claude/<X>/…``,
     via :func:`portfolio_classifier.anchor_path`). ``<X>`` normalized:
       * ∈ ``personal_orgs`` → personal/loose.
       * ∈ ``client_orgs`` → that client node (so a non-git materials folder like
         ``_eventsmx`` reconciles onto the SAME client as the git products under
         it — the headline reconciliation).
       * otherwise → LOOSE, tagged ``shared`` (a shared workspace of the owner's
         own, e.g. ``PRODUCCIONES`` — NEVER forced under a client; it holds
         possibly-many clients' work and splitting it would need re-anchoring,
         which is out of scope here).

**Case-insensitivity.** Orgs are compared through :func:`_norm_org`
(``lstrip('_')`` + ``lower()``). The resolver already lowercases every
``remote_url`` (``github.com/EventsMX/…`` → ``…/eventsmx/…``) and the parent
folder ``_eventsmx`` normalizes to ``eventsmx`` — so all three feeds land on one
key regardless of the on-disk casing.

**Flat by default.** With no client configuration AND no known owner identity the
projection is a NO-OP: every project stays in ``projects`` and there are no client
nodes — byte-for-byte the pre-#29 shape. The hierarchy only activates once the
owner's identity (``personal_orgs``) or an explicit ``client_orgs``/override is
known — you cannot tell "my org" from "a client's org" without knowing who you are.

**Rollup to the client (contributor-agnostic).** A client aggregates its
projects' effort (session/fs/git touches summed; ``active_days`` UNIONed over the
real day sets, never summed — cross-repo same-day work would double-count),
outcome (merged-PRs / closed-issues / open-issues summed — a FACT from
``github.db``, all authors summed, none surfaced), and inherits the **hottest**
project state (activo ≻ enfriándose ≻ estancado ≻ entregado ≻ pausado). Like the
per-project production strip, ``session_touches`` is NOT additive across projects
(a cross-project session counts once per project it touched); the client total is
an at-a-glance sum, not a deduplicated headcount.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from typing import Any

from hub.cache.portfolio_classifier import anchor_path

# Hottest-first ordering for the inherited client state (#28 states). A client is
# as hot as its liveliest project: recent work outranks a quiet delivery.
_STATE_RANK = {
    "activo": 5, "caliente": 5,
    "enfriandose": 4,
    "estancado": 3,
    "entregado": 2,
    "pausado": 1,
}


def suggest_client_orgs(
    workspace_db_path: str, github_db_path: str,
) -> list[str]:
    """Auto-seed a sensible ``client_orgs`` from the two stores (read-only).

    An org is a suggested client when EITHER feed vouches for it:

      * it is a registered repo owner in ``github.db`` (``repos.owner``), or
      * it owns ≥2 distinct projects in ``workspace.db`` (``remote_url``),

    normalized and de-duplicated. ``personal_orgs`` is NOT subtracted here — the
    ladder gives personal precedence anyway, so the owner's own org appearing in
    the seed is harmless and keeps the suggestion pure. Missing DB/table → the
    other feed still contributes; both missing → ``[]`` (portfolio stays flat).
    Returned sorted, so the persisted config is stable/diffable.
    """
    orgs: set[str] = set()
    # Feed 1: github.db registered repo owners.
    if os.path.exists(github_db_path):
        try:
            gc = sqlite3.connect(f"file:{github_db_path}?mode=ro", uri=True, timeout=5)
            try:
                for (owner,) in gc.execute("SELECT owner FROM repos"):
                    n = _norm_org(owner)
                    if n:
                        orgs.add(n)
            finally:
                gc.close()
        except sqlite3.OperationalError:
            pass
    # Feed 2: workspace.db orgs with ≥2 distinct git projects.
    if os.path.exists(workspace_db_path):
        try:
            wc = sqlite3.connect(f"file:{workspace_db_path}?mode=ro", uri=True, timeout=5)
            try:
                counts: Counter[str] = Counter()
                for (ru,) in wc.execute(
                    "SELECT DISTINCT remote_url FROM workspaces "
                    "WHERE remote_url IS NOT NULL"
                ):
                    org = _org_from_remote(ru)
                    if org:
                        counts[_norm_org(org)] += 1
                orgs.update(o for o, c in counts.items() if c >= 2)
            finally:
                wc.close()
        except sqlite3.OperationalError:
            pass
    return sorted(orgs)


def _norm_org(name: str | None) -> str:
    """Normalize an org/folder token for case- and underscore-insensitive match.

    ``_eventsmx`` → ``eventsmx``; ``EventsMX`` → ``eventsmx``. Mirrors the
    resolver's ``remote_url`` lowercasing so filesystem folder, git org, and
    github.db owner all collapse to one key.
    """
    return (name or "").strip().lstrip("_").lower()


def _org_from_remote(remote_url: str | None) -> str | None:
    """Owner org from a normalized ``github.com/<owner>/<repo>`` remote."""
    if not remote_url:
        return None
    parts = [p for p in remote_url.split("/") if p]
    # host / owner / repo  → owner is index 1 (host is present after normalize).
    if len(parts) >= 3:
        return parts[1]
    if len(parts) == 2:  # defensive: owner/repo without host
        return parts[0]
    return None


def _org_from_project_key(project_key: str | None) -> str | None:
    """Fallback org extraction straight from a ``git_remote:`` project key."""
    if project_key and project_key.startswith("git_remote:"):
        return _org_from_remote(project_key[len("git_remote:"):])
    return None


def _folder_client(anchor: str | None) -> str | None:
    """The parent-folder client candidate for a gitless project (RAW basename).

    The first meaningful folder below the container prefix (``Downloads``,
    ``Claude``, …) — the same anchor the classifier uses — is the client/workspace
    folder. Returns the raw basename (``_eventsmx``, ``PRODUCCIONES``) so the
    caller can both normalize it (for client matching) and display it verbatim
    (for a shared-workspace label). ``None`` for a degenerate/container root.
    """
    if not anchor:
        return None
    a = anchor_path(anchor)
    if not a:
        return None
    return os.path.basename(a.rstrip("/")) or None


def resolve_client(
    project_key: str | None,
    remote_url: str | None,
    anchor: str | None,
    *,
    client_orgs: set[str],
    personal_orgs: set[str],
    overrides: dict[str, str],
) -> dict[str, Any]:
    """Run the attribution ladder for ONE project.

    ``client_orgs``/``personal_orgs`` are pre-normalized sets. Returns a dict:
    ``{"bucket": ..., "client_key": ..., "client_label": ...}`` where bucket ∈
    ``{client, personal, shared, external}``. ``client_key``/``client_label`` are
    set only for ``bucket == "client"``.
    """
    # 1. Manual override wins outright.
    if project_key and project_key in overrides:
        name = overrides[project_key]
        return {"bucket": "client", "client_key": f"client:{_norm_org(name)}",
                "client_label": name}

    # 2. Git-remote owner (primary).
    org = _org_from_remote(remote_url) or _org_from_project_key(project_key)
    if org:
        norm = _norm_org(org)
        if norm in personal_orgs:
            return {"bucket": "personal", "client_key": None, "client_label": None}
        if norm in client_orgs:
            return {"bucket": "client", "client_key": f"client:{norm}",
                    "client_label": org}
        return {"bucket": "external", "client_key": None, "client_label": None}

    # 3. Parent-folder convention (gitless fallback).
    folder = _folder_client(anchor)
    if folder:
        norm = _norm_org(folder)
        if norm in personal_orgs:
            return {"bucket": "personal", "client_key": None, "client_label": None}
        if norm in client_orgs:
            return {"bucket": "client", "client_key": f"client:{norm}",
                    "client_label": folder}
        # A grouping folder of the owner's own that is not a known client:
        # a shared workspace (PRODUCCIONES). Loose, never forced under a client.
        return {"bucket": "shared", "client_key": None, "client_label": None}

    # No evidence at all (synthetic/encoded project) → loose personal.
    return {"bucket": "personal", "client_key": None, "client_label": None}


def _hotter(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick the hotter of two project states (see ``_STATE_RANK``)."""
    if not a:
        return b
    if not b:
        return a
    ra = _STATE_RANK.get(a.get("state", ""), 0)
    rb = _STATE_RANK.get(b.get("state", ""), 0)
    return a if ra >= rb else b


def _client_state(members: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Inherit the hottest member state + fold the outcome counts to the client.

    Carries evidence (``basis``, max ``last_activity``, summed counts) so the
    client chip stays "a read of evidence, never a bare flag" — and holds NO
    labels, so it passes masking untouched.

    CAVEAT: the outcome counts are reused from each project's derived state, and
    a project earns a state only when it has real LOCAL activity. A repo with
    merged PRs but no local touches yields no state and contributes 0 here — the
    client outcome reflects delivery on projects the owner actually worked, not
    every PR that ever landed. Acceptable: the grouped view only surfaces
    projects with rollup activity in the first place.
    """
    hottest: dict[str, Any] | None = None
    merged = closed = open_ = 0
    measurable = False
    last = ""
    for p in members:
        st = p.get("state")
        if st:
            hottest = _hotter(hottest, st)
            merged += int(st.get("merged_prs", 0) or 0)
            closed += int(st.get("closed_issues", 0) or 0)
            open_ += int(st.get("open_issues", 0) or 0)
            measurable = measurable or bool(st.get("outcome_measurable"))
            la = st.get("last_activity") or ""
            if la > last:
                last = la
    if hottest is None:
        return None
    return {
        "state": hottest.get("state"),
        "basis": "client_hottest",
        "last_activity": last or hottest.get("last_activity"),
        "outcome_measurable": measurable,
        "merged_prs": merged,
        "closed_issues": closed,
        "open_issues": open_,
    }


def _project_outcome(p: dict[str, Any]) -> dict[str, int]:
    """Per-project outcome counts, reused from the derived state (#28) if present.

    ``derive_project_states`` already folds ``github.db`` outcome onto the
    canonical project and hands back the counts, so the client rollup reuses them
    rather than paying a second github.db scan. Absent state → zeros.
    """
    st = p.get("state") or {}
    return {
        "merged_prs": int(st.get("merged_prs", 0) or 0),
        "closed_issues": int(st.get("closed_issues", 0) or 0),
        "open_issues": int(st.get("open_issues", 0) or 0),
    }


def _aggregate_client(client_key: str, label: str,
                      members: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a client's member projects into the client node (effort+outcome+state)."""
    s = f = g = 0
    days: set[str] = set()
    last = ""
    merged = closed = open_ = 0
    harness = 0
    for p in members:
        s += p.get("session_touches", 0) or 0
        f += p.get("fs_touches", 0) or 0
        g += p.get("git_touches", 0) or 0
        days.update(p.get("_day_set") or [])
        la = p.get("last_activity") or ""
        if la > last:
            last = la
        oc = _project_outcome(p)
        merged += oc["merged_prs"]
        closed += oc["closed_issues"]
        open_ += oc["open_issues"]
        harness += p.get("collapsed_harness", 0) or 0
    from hub.cache.workspace_store import _lit_sources
    node = {
        "client_key": client_key,
        "client_label": label,
        "bucket": "client",
        "session_touches": s, "fs_touches": f, "git_touches": g,
        "active_days": len(days),
        "last_activity": last or None,
        "sources": _lit_sources(s, f, g),
        "outcome": {"merged_prs": merged, "closed_issues": closed,
                    "open_issues": open_},
        "collapsed_harness": harness,
        "projects": members,
    }
    st = _client_state(members)
    if st:
        node["state"] = st
    return node


def group_by_client(
    projects: list[dict[str, Any]],
    *,
    client_orgs: set[str],
    personal_orgs: set[str],
    overrides: dict[str, str],
) -> dict[str, Any]:
    """Split the flat project list into client nodes + loose + external drawer.

    Flat no-op when there is no configuration at all (all three inputs empty):
    ``projects`` passes through and ``clients``/``external`` are empty. Member
    projects keep their full per-project shape (label, children, state, …).
    """
    if not client_orgs and not personal_orgs and not overrides:
        return {"clients": [], "projects": projects, "external": []}

    clients: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    loose: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []

    for p in projects:
        ref = resolve_client(
            p.get("project_key"), p.get("_remote_url"), p.get("_anchor_path"),
            client_orgs=client_orgs, personal_orgs=personal_orgs,
            overrides=overrides,
        )
        bucket = ref["bucket"]
        if bucket == "client":
            ck = ref["client_key"]
            if ck not in clients:
                clients[ck] = {"label": ref["client_label"], "members": []}
                order.append(ck)
            clients[ck]["members"].append(p)
        elif bucket == "external":
            external.append(p)
        else:  # personal | shared → loose, tagged for the UI
            p2 = dict(p)
            p2["client_bucket"] = bucket
            loose.append(p2)

    nodes = [_aggregate_client(ck, clients[ck]["label"], clients[ck]["members"])
             for ck in order]
    # Hottest / most-active client first (mirror the project sort discipline).
    nodes.sort(
        key=lambda c: (c.get("last_activity") or "",
                       c["session_touches"] + c["fs_touches"] + c["git_touches"]),
        reverse=True,
    )
    external.sort(key=lambda p: (p.get("last_activity") or ""), reverse=True)
    return {"clients": nodes, "projects": loose, "external": external}


def strip_internal(payload: dict[str, Any]) -> None:
    """Remove the internal ``_``-prefixed evidence fields from a grouped payload.

    Recurses clients→projects→children, loose projects/children, external, and
    unclassified. Called just before the payload leaves the read wrapper so the
    join-only fields (``_day_set``, ``_remote_url``, ``_anchor_path``) never ship.
    """
    def _clean(rows: list[dict[str, Any]] | None) -> None:
        for r in rows or []:
            for k in ("_day_set", "_remote_url", "_anchor_path"):
                r.pop(k, None)
            _clean(r.get("children"))

    for c in payload.get("clients", []):
        members = c.get("projects", [])
        _clean(members)
    _clean(payload.get("projects"))
    _clean(payload.get("external"))
    _clean(payload.get("unclassified"))
