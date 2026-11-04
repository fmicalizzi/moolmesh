"""Active-branch view of a tree-shaped session over stored events (#67).

Pi sessions are trees (``/tree`` lets the user go back to an earlier entry and
continue from there) and Claude Code lines carry ``parentUuid``; both adapters
store the native ids in ``events.entry_id`` / ``events.parent_id``. This module
turns a session's stored events into the branch the user is on now, for the
transcript views (``mool export``, the MCP ``get_session_events`` tool).

Design (conservative — never hide what is not provably abandoned):

* Sessions with no ``parent_id`` at all are returned unchanged (same order).
* The leaf is the last event (in stored order) that has an ``entry_id``: the
  append-only tip of the file, which is where the conversation continues.
* Only events in a *proven* abandoned branch are left out: walking from the
  leaf to the root over known links, every other child of a node on that
  path, plus everything below it. A link whose parent was never stored as an
  event (provider bookkeeping lines the adapter skips, rows written before
  ``parent_id`` existed) stops the walk there; whatever hangs above that gap
  is kept, because it cannot be placed on or off the branch.
* Events without an ``entry_id`` (legacy rows, synthetic events) are kept.
* Order is the stored order — the input order — never re-sorted.

Nothing is deleted: abandoned branches stay in events.db and in every other
view (search, counters, tokens); only the transcript shows the active branch.
"""

from __future__ import annotations

from typing import Any


def has_tree(events: list[dict[str, Any]]) -> bool:
    """True when any event carries a native parent id."""
    return any(e.get("parent_id") for e in events)


def active_branch(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events of the active branch, in input order (see module doc)."""
    if not has_tree(events):
        return list(events)
    ids = {e["entry_id"] for e in events if e.get("entry_id")}
    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    for e in events:
        eid, pid = e.get("entry_id"), e.get("parent_id")
        if not eid or not pid or pid not in ids or eid in parent_of:
            continue
        parent_of[eid] = pid
        children.setdefault(pid, []).append(eid)
    leaf = next((e["entry_id"] for e in reversed(events) if e.get("entry_id")), None)
    if leaf is None:
        return list(events)

    path: list[str] = []
    seen: set[str] = set()
    node: str | None = leaf
    while node and node not in seen:
        seen.add(node)
        path.append(node)
        node = parent_of.get(node)

    on_path = set(path)
    hidden: set[str] = set()
    stack = [c for p in path for c in children.get(p, ()) if c not in on_path]
    while stack:
        c = stack.pop()
        if c in hidden or c in on_path:
            continue
        hidden.add(c)
        stack.extend(children.get(c, ()))
    return [e for e in events if e.get("entry_id") not in hidden]
