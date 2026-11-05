"""The README reaches PyPI and renders there (#152).

``pyproject.toml`` must declare the README, and every link or image in the
READMEs must be absolute: PyPI resolves a relative one against pypi.org, where
it breaks. In-page anchors (``#…``) are fine.
"""
from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READMES = ("README.md", "README.es.md")

# Markdown link/image targets ``](target)`` and HTML ``src=``/``href=``.
_MD_TARGET = re.compile(r"\]\(\s*([^)\s]+)")
_HTML_TARGET = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")
_ABSOLUTE = re.compile(r"^(?:https?://|mailto:|#)")


def _project():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]


def test_pyproject_declares_the_readme():
    readme = _project().get("readme")
    assert readme == "README.md"
    assert (ROOT / readme).is_file()


@pytest.mark.parametrize("name", READMES)
def test_readme_links_and_images_are_absolute(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    targets = _MD_TARGET.findall(text) + _HTML_TARGET.findall(text)
    assert targets, "no links found: the pattern is broken"
    relative = sorted({t for t in targets if not _ABSOLUTE.match(t)})
    assert relative == [], f"{name}: relative links break on PyPI: {relative}"


def test_built_metadata_carries_the_readme(tmp_path, monkeypatch):
    """The wheel METADATA has a markdown description body (the real symptom).

    Needs setuptools >= 77 (the build backend, PEP 639 license), which the
    dev venv may not carry; the project itself stays zero-dependency, so this
    skips without it.
    """
    setuptools = pytest.importorskip("setuptools")
    major = int(setuptools.__version__.split(".")[0])
    if major < 77:   # ``license = "MIT"`` (PEP 639) needs setuptools >= 77
        pytest.skip(f"setuptools {setuptools.__version__} < 77 cannot read"
                    " this pyproject (PEP 639 license)")
    import setuptools.build_meta as build_meta
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, src / name)
    shutil.copytree(ROOT / "hub", src / "hub",
                    ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "meta"
    out.mkdir()
    monkeypatch.chdir(src)
    dist_info = build_meta.prepare_metadata_for_build_wheel(str(out))
    metadata = (out / dist_info / "METADATA").read_text(encoding="utf-8")
    headers, _, body = metadata.partition("\n\n")
    assert "Description-Content-Type: text/markdown" in headers
    assert body.strip(), "empty description body"
    assert "MoolMesh" in body
    assert os.path.basename(dist_info).startswith("moolmesh-")
