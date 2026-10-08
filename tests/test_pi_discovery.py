"""Tests for Pi project discovery (header cwd grouping, skip_dir, fallback)."""

import json
from pathlib import Path

from hub.discovery import ProjectDiscovery
from hub.models.base import Provider


def _header(cwd: str, sid: str = "sess-1") -> str:
    return json.dumps({
        "type": "session", "version": 3, "id": sid,
        "timestamp": "2026-05-01T10:00:00.000Z", "cwd": cwd,
    }) + "\n"


def _msg(sid: str = "sess-1") -> str:
    return json.dumps({
        "type": "message", "id": "u001", "parentId": None,
        "timestamp": "2026-05-01T10:00:05.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }) + "\n"


class TestDiscoverPi:
    def test_groups_files_by_header_cwd(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        (base / "sessions" / "--home-dev-acme-web--").mkdir(parents=True)
        (base / "sessions" / "--home-dev-acme-web--" / "a.jsonl").write_text(
            _header("/home/dev/acme-web") + _msg())
        (base / "sessions" / "--home-dev-acme-web--" / "b.jsonl").write_text(
            _header("/home/dev/acme-web", "sess-2") + _msg("sess-2"))

        projects = ProjectDiscovery(pi_base=base).discover_pi()
        assert len(projects) == 1
        p = projects[0]
        assert p.provider == Provider.PI
        assert p.name == "acme-web"
        assert p.path == "/home/dev/acme-web"
        assert len(p.session_files) == 2

    def test_encoded_folder_is_not_trusted_for_cwd(self, tmp_path):
        """A lossy folder name (``/`` vs ``_`` vs ``-``) never becomes the cwd."""
        base = tmp_path / ".pi" / "agent"
        d = base / "sessions" / "--home-dev-acme-web--"
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text(_header("/home/dev/acme_web-demo") + _msg())
        projects = ProjectDiscovery(pi_base=base).discover_pi()
        assert projects[0].path == "/home/dev/acme_web-demo"

    def test_unreadable_header_falls_back_to_one_bucket(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        d = base / "sessions" / "--broken--"
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text("not json\n")
        (d / "b.jsonl").write_text("")
        projects = ProjectDiscovery(pi_base=base).discover_pi()
        assert len(projects) == 1
        assert projects[0].name == "pi-sessions"
        assert len(projects[0].session_files) == 2

    def test_missing_base_is_empty(self, tmp_path):
        assert ProjectDiscovery(pi_base=tmp_path / "nope").discover_pi() == []

    def test_skip_dir_prunes_walk(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        keep = base / "sessions" / "--a--"
        skip = base / "sessions" / "--b--"
        keep.mkdir(parents=True)
        skip.mkdir(parents=True)
        (keep / "a.jsonl").write_text(_header("/home/dev/acme-web") + _msg())
        (skip / "b.jsonl").write_text(_header("/home/dev/other") + _msg())

        projects = ProjectDiscovery(
            pi_base=base, skip_dir=lambda p: p.name == "--b--"
        ).discover_pi()
        assert [p.name for p in projects] == ["acme-web"]

    def test_discover_all_includes_pi(self, tmp_path):
        base = tmp_path / ".pi" / "agent"
        d = base / "sessions" / "--a--"
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text(_header("/home/dev/acme-web") + _msg())
        projects = ProjectDiscovery(
            claude_base=tmp_path / "nope", codex_base=tmp_path / "nope",
            qwen_base=tmp_path / "nope", opencode_base=tmp_path / "nope.db",
            cursor_base=tmp_path / "nope2", pi_base=base,
        ).discover_all()
        assert [p.provider for p in projects] == [Provider.PI]

    def test_env_override_defines_default_base(self, tmp_path, monkeypatch):
        from hub.parsers.pi_parser import default_pi_base
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "custom-agent"))
        assert default_pi_base() == tmp_path / "custom-agent"
        monkeypatch.delenv("PI_CODING_AGENT_DIR")
        assert default_pi_base() == Path.home() / ".pi" / "agent"
