"""#49.2: the portfolio freshness line only rewrites its DOM when it changes,
so ``aria-live="polite"`` does not re-announce the same text every 60 s."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "hub" / "dashboard" / "static" / "portfolio.html"


def _render_fn() -> str:
    src = PAGE.read_text(encoding="utf-8")
    m = re.search(r"function renderFreshness\(m\)\{.*?\n\}", src, re.S)
    assert m, "renderFreshness not found"
    return m.group(0)


def test_render_is_guarded_by_a_change_check():
    fn = _render_fn()
    assert "el.innerHTML = html" in fn
    # The write sits behind a comparison with the last rendered markup.
    assert re.search(r"if \(el\.dataset\.freshHtml !== html\)\{", fn)
    assert re.search(r"if \(el\.className !== cls\)", fn)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_same_state_does_not_touch_the_dom():
    fn = _render_fn()
    script = f"""
let writes = 0, html = '';
const el = {{ className: '', dataset: {{}},
  set innerHTML(v) {{ writes++; html = v; }}, get innerHTML() {{ return html; }} }};
const document = {{ getElementById: () => el }};
let state = {{ cls: 'on', html: 'Portfolio actualizado <b>hace 1 min</b>' }};
function freshnessState() {{ return state; }}
{fn}
renderFreshness({{}}); renderFreshness({{}}); renderFreshness({{}});
const afterSame = writes;
state = {{ cls: 'on', html: 'Portfolio actualizado <b>hace 2 min</b>' }};
renderFreshness({{}});
console.log(JSON.stringify([afterSame, writes]));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    assert json.loads(out) == [1, 2]
