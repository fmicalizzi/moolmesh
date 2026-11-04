"""Parallel tool results of Claude sessions are never hidden (issue #127).

Synthetic Acme fixtures only. Claude Code writes each parallel ``tool_use`` as
its own chained line (``a2.parentUuid = a1``) and each ``tool_result`` hangs
from the ``tool_use`` it answers, so a tool_use has two children without any
user branching. Only Pi's real ``/tree`` branches are pruned.
"""

from __future__ import annotations

from hub.cache.event_store import EventStore
from hub.linearize import active_branch

SID = "sess-acme-parallel"


def _ts(i: int) -> str:
    return f"2026-05-01T10:00:{i:02d}.000Z"


def _ev(eid, parent, i, etype, summary, provider="claude", tool=None):
    return {"provider": provider, "project": "acme", "session_id": SID,
            "event_type": etype, "timestamp": _ts(i), "summary": summary,
            "tool_name": tool, "entry_id": eid, "parent_id": parent}


def _parallel_turn(n: int, provider: str = "claude") -> list[dict]:
    """u1 → a1 → a2 … aN (chained tool_use), rK → aK, final → rN."""
    evs = [_ev("u1", None, 0, "user", "acme: read the configs", provider)]
    prev, i = "u1", 1
    for k in range(1, n + 1):
        evs.append(_ev(f"a{k}", prev, i, "tool_use", f"acme: Read cfg{k}",
                       provider, "Read"))
        prev, i = f"a{k}", i + 1
    for k in range(1, n + 1):
        evs.append(_ev(f"r{k}", f"a{k}", i, "tool_result", f"acme: cfg{k} body",
                       provider, "Read"))
        i += 1
    evs.append(_ev("f", f"r{n}", i, "assistant", "acme: both configs read",
                   provider))
    return evs


def test_two_parallel_tools_keep_every_result():
    evs = _parallel_turn(2)
    assert len(evs) == 6
    assert active_branch(evs) == evs


def test_three_parallel_tools_keep_every_result():
    evs = _parallel_turn(3)
    assert active_branch(evs) == evs


def test_parallel_tools_through_the_store(tmp_path):
    store = EventStore(db_path=tmp_path / "events.db")
    for e in _parallel_turn(3):
        store.store(dict(e))
    branch = store.get_session_events(SID, active_branch=True)
    assert [e["summary"] for e in branch] \
        == [e["summary"] for e in store.get_session_events(SID)]
    assert len(branch) == 8
    store.close()


def test_session_ending_mid_turn_hides_nothing():
    # Results written, final assistant line not yet: leaf is r2; r1 must stay.
    evs = _parallel_turn(2)[:-1]
    assert active_branch(evs) == evs
    # Only the first tool_use answered so far.
    evs = _parallel_turn(2)[:4]
    assert active_branch(evs) == evs


def test_real_pi_branch_still_hidden():
    evs = [
        _ev("u1", None, 0, "user", "root", "pi"),
        _ev("a1", "u1", 1, "assistant", "first", "pi"),
        _ev("u2", "a1", 2, "user", "abandoned", "pi"),
        _ev("a2", "u2", 3, "assistant", "abandoned reply", "pi"),
        _ev("u3", "a1", 4, "user", "active", "pi"),
        _ev("a3", "u3", 5, "assistant", "active reply", "pi"),
    ]
    assert [e["entry_id"] for e in active_branch(evs)] == ["u1", "a1", "u3", "a3"]


def test_claude_sibling_user_message_kept_in_stored_order():
    # A possible rewind is not provably abandoned for Claude: nothing hidden.
    evs = [
        _ev("u1", None, 0, "user", "root"),
        _ev("a1", "u1", 1, "assistant", "first"),
        _ev("u2", "a1", 2, "user", "maybe abandoned"),
        _ev("u3", "a1", 3, "user", "active"),
    ]
    assert active_branch(evs) == evs
