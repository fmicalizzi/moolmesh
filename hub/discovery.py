"""Auto-discover AI agent projects across all providers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from hub.models.base import Provider
from hub.log import get as get_logger

_log = get_logger("Discovery")


@dataclass(slots=True)
class DiscoveredProject:
    name: str  # decoded project name (last path component)
    path: str  # original CWD path (decoded or from JSONL)
    provider: Provider
    session_dir: Path  # directory containing session files
    session_files: list[Path] = field(default_factory=list)
    encoded_name: str = ""  # the -Users-foo-bar encoded name


@dataclass(slots=True)
class DiscoveredSession:
    file_path: Path
    session_id: str
    provider: Provider
    project_name: str
    has_subagents: bool = False
    subagent_files: list[Path] = field(default_factory=list)


class ProjectDiscovery:
    """Discovers projects from Claude, Codex, Qwen, and OpenCode CLI data directories."""

    def __init__(
        self,
        claude_base: Path | None = None,
        codex_base: Path | None = None,
        qwen_base: Path | None = None,
        opencode_base: Path | None = None,
    ):
        home = Path.home()
        self.claude_base = claude_base or home / ".claude" / "projects"
        self.codex_base = codex_base or home / ".codex"
        self.qwen_base = qwen_base or home / ".qwen" / "projects"
        self.opencode_db = opencode_base or (
            home / ".local" / "share" / "opencode" / "opencode.db"
        )

    # ── Public API ──────────────────────────────────────────────

    def discover_all(self) -> list[DiscoveredProject]:
        projects: list[DiscoveredProject] = []
        projects.extend(self.discover_claude())
        projects.extend(self.discover_codex())
        projects.extend(self.discover_qwen())
        projects.extend(self.discover_opencode())
        return projects

    def discover_claude(self) -> list[DiscoveredProject]:
        projects: list[DiscoveredProject] = []
        if not self.claude_base.is_dir():
            return projects

        for project_dir in sorted(self.claude_base.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            encoded = project_dir.name
            decoded = self.decode_project_path(encoded)
            name = self.extract_project_name(decoded)

            # Collect JSONL files (session files are at project_dir level
            # and inside session-id subdirectories)
            session_files: list[Path] = []
            for item in project_dir.iterdir():
                if item.is_file() and item.suffix == ".jsonl":
                    session_files.append(item)
                elif item.is_dir():
                    # Session directory — may contain JSONL and subagents/
                    for sub_item in item.iterdir():
                        if sub_item.is_file() and sub_item.suffix == ".jsonl":
                            session_files.append(sub_item)

            if session_files:
                projects.append(
                    DiscoveredProject(
                        name=name,
                        path=decoded,
                        provider=Provider.CLAUDE,
                        session_dir=project_dir,
                        session_files=sorted(session_files),
                        encoded_name=encoded,
                    )
                )
        return projects

    def discover_codex(self) -> list[DiscoveredProject]:
        """Discover Codex projects by reading CWD from state_5.sqlite.

        Threads with a real CWD (not '/') are grouped by project name,
        so their rollouts merge with same-named Claude/Qwen projects.
        Threads with CWD='/' go into a 'codex-sessions' fallback bucket.
        """
        projects: list[DiscoveredProject] = []
        sessions_dir = self.codex_base / "sessions"
        if not sessions_dir.is_dir():
            return projects

        # Build rollout_path -> cwd mapping from SQLite
        # Only include threads that actually did work (tokens > 0 or non-exec source)
        rollout_cwd: dict[str, str] = {}
        active_rollouts: set[str] = set()
        state_db = self.codex_base / "state_5.sqlite"
        if state_db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(state_db))
                rows = conn.execute(
                    """SELECT rollout_path, cwd, tokens_used, source
                       FROM threads"""
                ).fetchall()
                conn.close()
                for rpath, cwd, tokens_used, source in rows:
                    if not rpath:
                        continue
                    rollout_cwd[rpath] = cwd or "/"
                    # Skip noise: exec sessions with 0 tokens (health checks)
                    if source == "exec" and (tokens_used or 0) == 0 and cwd == "/":
                        continue
                    active_rollouts.add(rpath)
            except Exception:
                _log.warning("No se pudo leer SQLite de Codex", exc_info=True)

        # Collect all rollout files, filtering out noise if we have SQLite data
        all_rollouts: list[Path] = []
        for root, _dirs, files in os.walk(sessions_dir):
            for f in files:
                if f.startswith("rollout-") and f.endswith(".jsonl"):
                    full = Path(root) / f
                    # If we have SQLite data, only include active rollouts
                    if active_rollouts and str(full) not in active_rollouts:
                        continue
                    all_rollouts.append(full)

        if not all_rollouts:
            return projects

        # Group rollouts by project name
        from collections import defaultdict
        by_project: dict[str, list[Path]] = defaultdict(list)

        for rollout_path in all_rollouts:
            cwd = rollout_cwd.get(str(rollout_path), "/")
            if cwd and cwd != "/":
                name = self.extract_project_name(cwd)
            else:
                name = "codex-sessions"
            by_project[name].append(rollout_path)

        # Create one DiscoveredProject per group
        for name, rollouts in by_project.items():
            # Use the CWD of the first rollout with a real path
            path = str(self.codex_base)
            for r in rollouts:
                cwd = rollout_cwd.get(str(r), "/")
                if cwd and cwd != "/":
                    path = cwd
                    break

            projects.append(
                DiscoveredProject(
                    name=name,
                    path=path,
                    provider=Provider.CODEX,
                    session_dir=sessions_dir,
                    session_files=sorted(rollouts),
                    encoded_name=f"codex-{name}",
                )
            )
        return projects

    def discover_qwen(self) -> list[DiscoveredProject]:
        projects: list[DiscoveredProject] = []
        if not self.qwen_base.is_dir():
            return projects

        for project_dir in sorted(self.qwen_base.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            encoded = project_dir.name
            decoded = self.decode_project_path(encoded)
            name = self.extract_project_name(decoded)

            chats_dir = project_dir / "chats"
            session_files: list[Path] = []
            if chats_dir.is_dir():
                session_files = sorted(
                    p for p in chats_dir.iterdir()
                    if p.is_file() and p.suffix == ".jsonl"
                )

            if session_files:
                projects.append(
                    DiscoveredProject(
                        name=name,
                        path=decoded,
                        provider=Provider.QWEN,
                        session_dir=project_dir,
                        session_files=session_files,
                        encoded_name=encoded,
                    )
                )
        return projects

    def discover_opencode(self) -> list[DiscoveredProject]:
        """Discover OpenCode projects from SQLite database.

        Each unique session.directory is treated as a project.
        """
        projects: list[DiscoveredProject] = []
        if not self.opencode_db.exists():
            return projects

        try:
            import sqlite3
            conn = sqlite3.connect(str(self.opencode_db), timeout=5)
            rows = conn.execute("""
                SELECT DISTINCT
                    s.directory,
                    p.name AS project_name,
                    COUNT(DISTINCT s.id) AS session_count
                FROM session s
                LEFT JOIN project p ON s.project_id = p.id
                WHERE s.directory IS NOT NULL AND s.directory != ''
                GROUP BY s.directory
                HAVING session_count > 0
                ORDER BY MAX(s.time_updated) DESC
            """).fetchall()
            conn.close()

            for directory, project_name, _session_count in rows:
                name = project_name or self.extract_project_name(directory)
                projects.append(
                    DiscoveredProject(
                        name=name,
                        path=directory,
                        provider=Provider.OPENCODE,
                        session_dir=self.opencode_db.parent,
                        session_files=[self.opencode_db],
                        encoded_name=f"opencode-{self.encode_project_path(directory)}",
                    )
                )
        except Exception:
            _log.warning("No se pudo leer SQLite de OpenCode", exc_info=True)

        return projects

    def find_active_sessions(
        self, provider: Provider | None = None, minutes: int = 10
    ) -> list[DiscoveredSession]:
        """Find sessions with files modified in the last N minutes."""
        cutoff = time.time() - (minutes * 60)
        sessions: list[DiscoveredSession] = []

        projects = self.discover_all()
        for proj in projects:
            if provider and proj.provider != provider:
                continue
            for f in proj.session_files:
                try:
                    if f.stat().st_mtime >= cutoff:
                        sid = f.stem  # session ID is typically the filename
                        subagent_files = (
                            self.discover_claude_subagents(f.parent)
                            if proj.provider == Provider.CLAUDE
                            else []
                        )
                        sessions.append(
                            DiscoveredSession(
                                file_path=f,
                                session_id=sid,
                                provider=proj.provider,
                                project_name=proj.name,
                                has_subagents=bool(subagent_files),
                                subagent_files=subagent_files,
                            )
                        )
                except OSError:
                    continue
        return sessions

    @staticmethod
    def discover_claude_subagents(session_dir: Path) -> list[Path]:
        """Find agent-*.jsonl files in a session's subagents/ directory."""
        subagents_dir = session_dir / "subagents"
        if not subagents_dir.is_dir():
            return []
        return sorted(
            p
            for p in subagents_dir.iterdir()
            if p.is_file() and p.name.startswith("agent-") and p.suffix == ".jsonl"
        )

    # ── Path utilities ──────────────────────────────────────────

    @staticmethod
    def decode_project_path(encoded: str) -> str:
        """Decode encoded path: '-Users-foo-bar' -> '/Users/foo/bar'."""
        if not encoded:
            return ""
        # The encoding replaces / with - and path separators with --
        # But the simplest approach: leading - is /, rest - are /
        # Note: this is lossy if dir names contain hyphens
        return encoded.replace("-", "/")

    @staticmethod
    def encode_project_path(cwd: str) -> str:
        """Encode CWD path: '/Users/foo/bar' -> '-Users-foo-bar'."""
        return cwd.replace("/", "-")

    @staticmethod
    def extract_project_name(decoded_path: str) -> str:
        """Extract a meaningful multi-component name from a project path.

        Strips common prefixes (home, Downloads, Claude, etc.) and returns
        enough trailing components to be descriptive, similar to how Claude
        encodes project directories.

        '/Users/franco/Downloads/Claude/ddtyi/YAAHub' -> 'ddtyi/YAAHub'
        '/Users/franco/Downloads/Claude/eventsmx/front/stack' -> 'eventsmx/front/stack'
        '/Users/franco/Downloads/Claude/acuernavaca' -> 'acuernavaca'
        '/Users/franco/Downloads/data/2026/04/.../0000' -> 'data-batch-0000'
        """
        if not decoded_path or decoded_path == "/":
            return decoded_path or "unknown"

        parts = decoded_path.rstrip("/").split("/")
        # Remove empty parts from leading slash or double slashes
        parts = [p for p in parts if p]

        # Find where the meaningful suffix starts by stripping known prefixes
        # Common roots: Users/<name>, Volumes/<name>, home/<name>
        cut = 0
        skip_names = {
            "Users", "Volumes", "home", "root", "tmp", "private",
        }
        # Skip /Users/<username> or /Volumes/<name>
        for i, p in enumerate(parts):
            if p in skip_names:
                cut = i + 2  # skip the prefix + username/volume name
                break

        if cut > 0 and cut < len(parts):
            parts = parts[cut:]

        # Now strip common intermediate dirs
        strip_dirs = {
            "Downloads", "Documents", "Projects", "repos", "src", "code",
            "Desktop", "workspace",
        }
        while parts and parts[0] in strip_dirs:
            parts = parts[1:]

        # Collapse date-stamped directory segments (2026/04/04/04/54/43 → skip)
        # A date sequence is 3+ consecutive all-numeric segments starting with YYYY
        import re
        collapsed: list[str] = []
        i = 0
        while i < len(parts):
            if re.fullmatch(r"\d{4}", parts[i]):
                # Peek ahead: if 3+ consecutive numeric segments, skip them
                j = i
                while j < len(parts) and re.fullmatch(r"\d{1,4}", parts[j]):
                    j += 1
                if j - i >= 3:
                    # Skip the date segments, but keep any trailing numeric
                    # that's NOT part of the date (e.g., "0000" batch id)
                    i = j
                    continue
                else:
                    collapsed.append(parts[i])
            else:
                collapsed.append(parts[i])
            i += 1
        if collapsed:
            parts = collapsed

        # Strip "Claude" as a container (it's not a project, it's where projects live)
        if parts and parts[0] == "Claude":
            parts = parts[1:]
        # Also strip "Temporal" prefix since it's just a temp container
        if parts and parts[0] == "Temporal":
            parts = parts[1:]

        if not parts:
            # Fallback: last 2 components of original path
            orig = decoded_path.rstrip("/").split("/")
            parts = [p for p in orig[-2:] if p]

        # For very deep paths (>3 components), keep last 3
        if len(parts) > 3:
            parts = parts[-3:]

        return "/".join(parts) if parts else decoded_path.rstrip("/").split("/")[-1]

    @staticmethod
    def short_cwd(cwd: str, depth: int = 3) -> str:
        """Shorten a CWD to last N meaningful path components.

        '/Users/franco/Downloads/Claude/tools/live-monitor' -> 'tools/live-monitor'
        """
        if not cwd:
            return ""
        parts = cwd.rstrip("/").split("/")
        # Skip common prefixes like /Users/xxx/Downloads/Claude
        # Find the most meaningful suffix
        meaningful = parts
        for i, p in enumerate(parts):
            if p in ("Downloads", "Documents", "Projects", "repos", "src", "code"):
                meaningful = parts[i + 1 :]
                break
            if p == "Claude" and i > 0:
                meaningful = parts[i + 1 :]
                break
        if not meaningful:
            meaningful = parts[-depth:]
        if len(meaningful) > depth:
            meaningful = meaningful[-depth:]
        return "/".join(meaningful)
