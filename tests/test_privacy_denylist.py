"""Private denylist guard (#68): the public tree must not carry the owner's
real portfolio names in examples.

The terms live in a LOCAL file OUTSIDE the repo (one term per line) whose path
is passed through ``MOOLMESH_PRIVACY_DENYLIST``; that file is never versioned.
Without the variable the guard skips. Failure messages reference terms by
INDEX only — the list itself never appears in code, logs or reports.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ENV = "MOOLMESH_PRIVACY_DENYLIST"
REPO = Path(__file__).resolve().parents[1]


def _terms() -> list[str]:
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        pytest.skip(f"{ENV} not set — private denylist guard disabled")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{ENV} points to a missing file")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _versioned_files() -> list[str]:
    """Every file tracked by git (the tree that will be published)."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, check=True,
    )
    return [f for f in out.stdout.decode("utf-8", "surrogatepass").split("\0") if f]


def test_no_denylisted_terms_in_versioned_tree() -> None:
    terms = _terms()
    needles = [
        (i + 1, t.lower().encode("utf-8", "surrogatepass"))
        for i, t in enumerate(terms)
    ]
    hits: list[str] = []
    for rel in _versioned_files():
        try:
            data = (REPO / rel).read_bytes().lower()
        except OSError:
            continue
        for idx, needle in needles:
            if needle and needle in data:
                hits.append(f"{rel}: term #{idx}")
    assert not hits, (
        "denylisted terms present in the versioned tree "
        "(index only, never the term): " + ", ".join(hits)
    )
