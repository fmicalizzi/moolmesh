"""Batch report generator — discover, parse, adapt, analyze, render.

Two modes:
  1. _all/ — reporte general con todos los mensajes (full/week/day)
  2. Per-project — agrupado por NOMBRE de proyecto (unifica providers)
     YAAHub/ contiene sesiones de Claude + Qwen juntas
"""

from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hub.analyzers import ALL_ANALYZERS
from hub.discovery import DiscoveredProject, ProjectDiscovery
from hub.models.base import Provider, UnifiedMessage
from hub.parsers.claude_parser import ClaudeParser
from hub.parsers.codex_parser import CodexParser
from hub.parsers.qwen_parser import QwenParser
from hub.parsers.opencode_parser import OpenCodeParser
from hub.adapters.claude_adapter import ClaudeAdapter
from hub.adapters.codex_adapter import CodexAdapter
from hub.adapters.qwen_adapter import QwenAdapter
from hub.adapters.opencode_adapter import OpenCodeAdapter
from hub.renderers.markdown import MarkdownRenderer


_PARSERS = {
    Provider.CLAUDE: ClaudeParser(),
    Provider.CODEX: CodexParser(),
    Provider.QWEN: QwenParser(),
    Provider.OPENCODE: OpenCodeParser(),
}

_ADAPTERS = {
    Provider.CLAUDE: ClaudeAdapter(),
    Provider.CODEX: CodexAdapter(),
    Provider.QWEN: QwenAdapter(),
    Provider.OPENCODE: OpenCodeAdapter(),
}

_opencode_cache: dict[Path, list] = {}


def _safe_dirname(name: str) -> str:
    """Convert project name to a flat safe directory name using '-' as separator."""
    # Join path components with '-' to keep flat structure like Claude encoding
    flat = name.replace("/", "-")
    return re.sub(r'[^\w\-.]', '_', flat) or "_unknown"


def load_project_messages(project: DiscoveredProject) -> list[UnifiedMessage]:
    """Load all messages from a discovered project."""
    parser = _PARSERS.get(project.provider)
    adapter = _ADAPTERS.get(project.provider)
    if not parser or not adapter:
        return []

    messages: list[UnifiedMessage] = []

    if project.provider == Provider.OPENCODE:
        db_path = project.session_files[0] if project.session_files else None
        if not db_path:
            return []
        if db_path not in _opencode_cache:
            _opencode_cache[db_path] = parser.parse_file(db_path)
        all_entries = _opencode_cache[db_path]
        entries = [e for e in all_entries if e.project_dir == project.path]
        for entry in entries:
            msg = adapter.to_unified(entry, project.name)
            if msg is not None:
                messages.append(msg)
    else:
        for fpath in project.session_files:
            try:
                entries = parser.parse_file(fpath)
            except Exception as e:
                import sys
                print(f"Warning: failed to parse {fpath.name}: {e}", file=sys.stderr)
                continue
            for entry in entries:
                msg = adapter.to_unified(entry, project.name)
                if msg is not None:
                    messages.append(msg)

    messages.sort(key=lambda m: str(m.timestamp or ""))
    return messages


def _filter_by_time(messages: list[UnifiedMessage], since: datetime) -> list[UnifiedMessage]:
    return [m for m in messages if m.timestamp and m.timestamp >= since]


def _run_report(
    messages: list[UnifiedMessage],
    output_dir: Path,
    label: str,
    *,
    complete: bool = False,
) -> list[Path]:
    if not messages:
        return []
    analyzers = [cls(complete=complete) for cls in ALL_ANALYZERS]
    renderer = MarkdownRenderer(analyzers)
    return renderer.render_all(messages, output_dir, label)


def _run_periods(
    msgs: list[UnifiedMessage],
    base_dir: Path,
    label: str,
    week_ago: datetime,
    today_start: datetime,
    *,
    complete: bool = False,
) -> tuple[list[Path], bool, bool]:
    """Run full/week/day reports. Returns (files, has_week, has_day)."""
    created: list[Path] = []
    created.extend(_run_report(msgs, base_dir / "full", f"{label} — Full", complete=complete))

    week_msgs = _filter_by_time(msgs, week_ago)
    has_week = bool(week_msgs)
    if has_week:
        created.extend(_run_report(week_msgs, base_dir / "week", f"{label} — Week", complete=complete))

    day_msgs = _filter_by_time(msgs, today_start)
    has_day = bool(day_msgs)
    if has_day:
        created.extend(_run_report(day_msgs, base_dir / "day", f"{label} — Day", complete=complete))

    return created, has_week, has_day


def _generate_grouped_project(
    project_name: str,
    projects: list[DiscoveredProject],
    base_dir: Path,
    week_ago: datetime,
    today_start: datetime,
    *,
    complete: bool = False,
) -> dict:
    """Generate report for a project name, merging all providers."""
    all_msgs: list[UnifiedMessage] = []
    providers_seen: list[str] = []

    for proj in projects:
        msgs = load_project_messages(proj)
        all_msgs.extend(msgs)
        if msgs:
            providers_seen.append(proj.provider.value)

    if not all_msgs:
        return {"project": project_name, "providers": [], "messages": 0,
                "files": 0, "skipped": True}

    all_msgs.sort(key=lambda m: str(m.timestamp or ""))

    dirname = _safe_dirname(project_name)
    proj_dir = base_dir / dirname
    providers_str = "+".join(sorted(set(providers_seen)))
    label = f"{project_name} ({providers_str})"

    created, has_week, has_day = _run_periods(
        all_msgs, proj_dir, label, week_ago, today_start, complete=complete
    )

    return {
        "project": project_name,
        "providers": sorted(set(providers_seen)),
        "messages": len(all_msgs),
        "files": len(created),
        "skipped": False,
        "has_week": has_week,
        "has_day": has_day,
    }


def generate_report(
    project_filter: str | None = None,
    provider_filter: str | None = None,
    output_dir: Path | None = None,
    *,
    complete: bool = False,
) -> list[dict]:
    """Generate batch reports: general + per-project, in parallel.

    Structure:
      reports/
        _all/                        <- reporte general
          full/  week/  day/
        YAAHub/                      <- por proyecto (claude+qwen unificados)
          full/  week/  day/
        services/
          full/  week/
        codex-sessions/
          full/  week/  day/
        ...
    """
    discovery = ProjectDiscovery()

    if provider_filter:
        provider_map = {
            "claude": discovery.discover_claude,
            "codex": discovery.discover_codex,
            "qwen": discovery.discover_qwen,
            "opencode": discovery.discover_opencode,
            "cursor": discovery.discover_cursor,
        }
        func = provider_map.get(provider_filter)
        all_projects = func() if func else []
    else:
        all_projects = discovery.discover_all()

    if project_filter:
        all_projects = [p for p in all_projects if project_filter.lower() in p.name.lower()]

    if not all_projects:
        print("No projects found matching filters.")
        return []

    if output_dir is None:
        output_dir = Path.cwd() / "reports"

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Group discovered projects by name (merge providers)
    by_name: dict[str, list[DiscoveredProject]] = defaultdict(list)
    for proj in all_projects:
        by_name[proj.name].append(proj)

    print(f"Generando reportes: {len(by_name)} proyectos + reporte general")
    print(f"Output: {output_dir}/\n")

    results: list[dict] = []

    # Phase 1: per-project reports (parallel, max 4 workers to limit memory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures: dict = {}
        for proj_name, proj_list in by_name.items():
            f = pool.submit(
                _generate_grouped_project,
                proj_name, proj_list, output_dir, week_ago, today_start,
                complete=complete,
            )
            futures[f] = proj_name

        for future in as_completed(futures):
            name = futures[future]
            try:
                r = future.result()
                results.append(r)
                if r["skipped"]:
                    print(f"  {name}: sin mensajes, skip")
                else:
                    provs = ",".join(r.get("providers", []))
                    periods = "full"
                    if r.get("has_week"):
                        periods += "+week"
                    if r.get("has_day"):
                        periods += "+day"
                    print(f"  {name} [{provs}]: {r['messages']:,} msgs -> {r['files']} archivos ({periods})")
            except Exception as e:
                print(f"  {name}: ERROR {e}")
                results.append({"project": name, "providers": [], "messages": 0,
                                "files": 0, "skipped": True, "error": str(e)})

    _opencode_cache.clear()

    # Phase 2: general report (_all/) — sequential to avoid doubling peak memory
    print("  _all: generando reporte general...")
    all_msgs: list[UnifiedMessage] = []
    for proj in all_projects:
        all_msgs.extend(load_project_messages(proj))
    all_msgs.sort(key=lambda m: str(m.timestamp or ""))
    _opencode_cache.clear()

    if not all_msgs:
        results.append({"project": "_all", "providers": [], "messages": 0,
                        "files": 0, "skipped": True})
    else:
        created, has_week, has_day = _run_periods(
            all_msgs, output_dir / "_all", "all-projects", week_ago, today_start,
            complete=complete,
        )
        r = {
            "project": "_all",
            "providers": sorted(set(p.provider.value for p in all_projects)),
            "messages": len(all_msgs),
            "files": len(created),
            "skipped": False,
            "has_week": has_week,
            "has_day": has_day,
        }
        results.append(r)
        provs = ",".join(r["providers"])
        periods = "full"
        if has_week:
            periods += "+week"
        if has_day:
            periods += "+day"
        print(f"  _all [{provs}]: {r['messages']:,} msgs -> {r['files']} archivos ({periods})")
    del all_msgs

    total_files = sum(r["files"] for r in results)
    total_msgs = sum(r.get("messages", 0) for r in results if r["project"] != "_all")
    ok = [r for r in results if not r["skipped"]]
    print(f"\n{'='*50}")
    print(f"{len(ok)}/{len(results)} reportes, {total_msgs:,} mensajes, {total_files} archivos")
    print(f"{'='*50}")

    return results
