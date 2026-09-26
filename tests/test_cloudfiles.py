"""Tests for cloud-only placeholder detection (issue #45).

No test ever opens a placeholder: flags are simulated by patching ``os.lstat``
and ``open`` is booby-trapped for the placeholder paths.
"""

from __future__ import annotations

import builtins
import os
import types
from pathlib import Path

import pytest

from hub import cloudfiles
from hub.cloudfiles import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    SF_DATALESS,
    PlaceholderSkipper,
    is_cloud_placeholder,
)
from hub.discovery import ProjectDiscovery


def _fake_stat(st_flags=None, st_file_attributes=None):
    st = types.SimpleNamespace(st_mode=0o100644, st_size=10, st_mtime=0.0)
    if st_flags is not None:
        st.st_flags = st_flags
    if st_file_attributes is not None:
        st.st_file_attributes = st_file_attributes
    return st


@pytest.fixture
def no_open(monkeypatch):
    """Fail the test if anything opens a path in ``trapped``."""
    trapped: set[str] = set()
    real_open = builtins.open

    def guarded(file, *a, **kw):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) in trapped:
            raise AssertionError(f"placeholder was opened: {file}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded)
    return trapped


def _patch_lstat(monkeypatch, mapping: dict[str, types.SimpleNamespace]):
    real_lstat = os.lstat

    def fake(p, *a, **kw):
        key = os.fspath(p)
        if key in mapping:
            return mapping[key]
        return real_lstat(p, *a, **kw)

    monkeypatch.setattr(cloudfiles.os, "lstat", fake)


class TestIsCloudPlaceholder:
    def test_macos_dataless_flag(self, tmp_path, monkeypatch, no_open):
        p = tmp_path / "session.jsonl"
        no_open.add(str(p))
        _patch_lstat(monkeypatch, {str(p): _fake_stat(st_flags=SF_DATALESS)})
        assert is_cloud_placeholder(p) is True

    def test_macos_other_flags_are_not_placeholders(self, tmp_path, monkeypatch):
        p = tmp_path / "session.jsonl"
        _patch_lstat(monkeypatch, {str(p): _fake_stat(st_flags=0x20)})  # UF_HIDDEN-ish
        assert is_cloud_placeholder(p) is False

    @pytest.mark.parametrize("attr", [
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
        FILE_ATTRIBUTE_RECALL_ON_OPEN,
        FILE_ATTRIBUTE_OFFLINE,
    ])
    def test_windows_recall_and_offline_attributes(self, tmp_path, monkeypatch, no_open, attr):
        p = tmp_path / "session.jsonl"
        no_open.add(str(p))
        # 0x20 = FILE_ATTRIBUTE_ARCHIVE, a normal bit that must not mask the check
        _patch_lstat(monkeypatch, {str(p): _fake_stat(st_file_attributes=attr | 0x20)})
        assert is_cloud_placeholder(p) is True

    def test_windows_plain_file(self, tmp_path, monkeypatch):
        p = tmp_path / "session.jsonl"
        _patch_lstat(monkeypatch, {str(p): _fake_stat(st_file_attributes=0x20)})
        assert is_cloud_placeholder(p) is False

    def test_legacy_icloud_stub_name(self, tmp_path, no_open):
        # Doesn't even need to exist: the name alone identifies it.
        p = tmp_path / ".rollout-2026-03-25.jsonl.icloud"
        no_open.add(str(p))
        assert is_cloud_placeholder(p) is True

    def test_icloud_suffix_without_leading_dot_is_normal(self, tmp_path):
        p = tmp_path / "notes.icloud"
        p.write_text("x")
        assert is_cloud_placeholder(p) is False

    def test_normal_file_is_false(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text('{"a": 1}\n')
        assert is_cloud_placeholder(p) is False

    def test_missing_path_is_false(self, tmp_path):
        assert is_cloud_placeholder(tmp_path / "nope.jsonl") is False

    def test_dataless_directory(self, tmp_path, monkeypatch):
        d = tmp_path / "proj"
        d.mkdir()
        _patch_lstat(monkeypatch, {str(d): _fake_stat(st_flags=SF_DATALESS)})
        assert is_cloud_placeholder(d) is True


class TestDiscoverySkipsDatalessDirectories:
    def _claude_tree(self, tmp_path: Path) -> Path:
        base = tmp_path / "projects"
        for name in ("-Users-x-ok", "-Users-x-cloud"):
            d = base / name
            d.mkdir(parents=True)
            (d / "s1.jsonl").write_text('{"type":"user"}\n')
        return base

    def test_dataless_project_dir_is_never_listed(self, tmp_path, monkeypatch):
        base = self._claude_tree(tmp_path)
        cloud_dir = base / "-Users-x-cloud"
        _patch_lstat(monkeypatch, {str(cloud_dir): _fake_stat(st_flags=SF_DATALESS)})

        real_iterdir = Path.iterdir

        def guarded_iterdir(self):
            if self == cloud_dir:
                raise AssertionError("dataless directory was listed")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
        skipper = PlaceholderSkipper()
        projects = ProjectDiscovery(claude_base=base, skip_dir=skipper).discover_claude()
        assert [p.encoded_name for p in projects] == ["-Users-x-ok"]
        assert skipper.skipped == [cloud_dir]

    def test_codex_walk_prunes_dataless_subdir(self, tmp_path, monkeypatch):
        codex = tmp_path / ".codex"
        good = codex / "sessions" / "2026" / "03" / "25"
        bad = codex / "sessions" / "2026" / "04"
        good.mkdir(parents=True)
        (bad / "01").mkdir(parents=True)
        meta = '{"type":"session_meta","payload":{"id":"a"}}\n'
        (good / "rollout-a.jsonl").write_text(meta)
        (bad / "01" / "rollout-b.jsonl").write_text(meta)
        _patch_lstat(monkeypatch, {str(bad): _fake_stat(st_flags=SF_DATALESS)})

        skipper = PlaceholderSkipper()
        projects = ProjectDiscovery(codex_base=codex, skip_dir=skipper).discover_codex()
        files = [f.name for p in projects for f in p.session_files]
        assert files == ["rollout-a.jsonl"]
        assert skipper.skipped == [bad]

    def test_without_skip_dir_discovery_is_unchanged(self, tmp_path, monkeypatch):
        base = self._claude_tree(tmp_path)
        _patch_lstat(monkeypatch, {
            str(base / "-Users-x-cloud"): _fake_stat(st_flags=SF_DATALESS),
        })
        projects = ProjectDiscovery(claude_base=base).discover_claude()
        assert len(projects) == 2
