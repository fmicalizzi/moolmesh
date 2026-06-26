"""CLI entry point for MoolMesh."""

from __future__ import annotations

import argparse
import os

from hub.colors import green, yellow, red, dim, bold
from hub.discovery import ProjectDiscovery


def cmd_dashboard(args: argparse.Namespace) -> None:
    from hub.dashboard.server import DashboardServer
    from hub.log import setup

    setup(level=getattr(args, "log_level", "INFO"))

    providers = None
    if args.providers:
        providers = [p.strip() for p in args.providers.split(",")]

    server = DashboardServer(
        host=args.host,
        port=args.port,
        project_filter=args.project,
        providers=providers,
    )
    server.start()


def cmd_daemon(args: argparse.Namespace) -> None:
    from hub.daemon import daemonize, stop_daemon, daemon_status, read_pid, LOG_FILE

    match args.daemon_command:
        case "start":
            existing = read_pid()
            if existing:
                print(yellow(f"MoolMesh daemon already running (PID {existing})"))
                return

            providers = None
            if args.providers:
                providers = [p.strip() for p in args.providers.split(",")]

            pid = daemonize(
                host=args.host,
                port=args.port,
                project_filter=getattr(args, "project", None),
                providers=providers,
            )
            print(green(f"MoolMesh daemon started (PID {pid})"))
            print(f"  Dashboard → http://{args.host}:{args.port}")
            print(dim(f"  Logs → {LOG_FILE}"))

        case "stop":
            if stop_daemon():
                print(green("MoolMesh daemon stopped"))
            else:
                print(yellow("No daemon running"))

        case "restart":
            info = daemon_status()
            if info:
                stop_daemon()
                print(dim("Stopped previous daemon"))
                import time
                time.sleep(0.5)

            providers = None
            if args.providers:
                providers = [p.strip() for p in args.providers.split(",")]

            pid = daemonize(
                host=args.host,
                port=args.port,
                project_filter=getattr(args, "project", None),
                providers=providers,
            )
            print(green(f"MoolMesh daemon restarted (PID {pid})"))
            print(f"  Dashboard → http://{args.host}:{args.port}")
            print(dim(f"  Logs → {LOG_FILE}"))

        case "status":
            _print_daemon_status()

        case _:
            print("Uso: mool daemon {start|stop|status|restart}")


def _print_daemon_status() -> None:
    import json
    from urllib.request import urlopen
    from hub.daemon import daemon_status

    info = daemon_status()
    if info is None:
        print(yellow("MoolMesh daemon is not running"))
        return

    uptime = info["uptime_seconds"]
    if uptime >= 3600:
        uptime_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m"
    elif uptime >= 60:
        uptime_str = f"{uptime // 60}m {uptime % 60}s"
    else:
        uptime_str = f"{uptime}s"

    print(green("MoolMesh daemon is running"))
    print(f"  PID:    {info['pid']}")
    print(f"  Uptime: {uptime_str}")

    # Query the running daemon for live stats
    try:
        with urlopen("http://localhost:5200/health", timeout=2) as resp:
            health = json.loads(resp.read())
        print(f"  Port:   {5200}")
        print(f"  Events: {health.get('events_count', 0):,}")
    except Exception:
        pass

    # Show monitored repos
    try:
        from hub.config import load_config
        config = load_config()
        if config.repos:
            print(f"  Repos:  {', '.join(f'{r.owner}/{r.repo}' for r in config.repos)}")
    except Exception:
        pass

    log_kb = info.get("log_size", 0) / 1024
    if log_kb > 1024:
        print(dim(f"  Log:    {log_kb / 1024:.1f} MB"))
    else:
        print(dim(f"  Log:    {log_kb:.0f} KB"))


def cmd_status(args: argparse.Namespace) -> None:
    if getattr(args, "json_output", False):
        _print_daemon_status_json()
    else:
        _print_daemon_status()


def _print_daemon_status_json() -> None:
    import json as _json
    from urllib.request import urlopen
    from hub.daemon import daemon_status

    info = daemon_status()
    if info is None:
        print(_json.dumps({"running": False}))
        return

    result = {"running": True, "pid": info["pid"], "uptime_seconds": info["uptime_seconds"]}

    try:
        with urlopen("http://localhost:5200/health", timeout=2) as resp:
            health = _json.loads(resp.read())
        result["port"] = 5200
        result["events_count"] = health.get("events_count", 0)
        result["version"] = health.get("version")
    except Exception:
        pass

    try:
        from hub.config import load_config
        config = load_config()
        if config.repos:
            result["repos"] = [f"{r.owner}/{r.repo}" for r in config.repos]
    except Exception:
        pass

    print(_json.dumps(result))


def cmd_report(args: argparse.Namespace) -> None:
    from datetime import date
    from pathlib import Path
    from hub.batch_reporter import generate_report

    if args.mode == "auto":
        if args.output:
            base = Path(args.output)
        else:
            base = Path.home() / ".moolmesh" / "reports"
        output_dir = base / date.today().isoformat()
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Auto report → {output_dir}")
    else:
        output_dir = Path(args.output) if args.output else None

    generate_report(
        project_filter=args.project,
        provider_filter=args.provider,
        output_dir=output_dir,
        complete=getattr(args, "complete", False),
    )


def cmd_discover(args: argparse.Namespace) -> None:
    discovery = ProjectDiscovery()

    if args.provider:
        provider_map = {
            "claude": discovery.discover_claude,
            "codex": discovery.discover_codex,
            "qwen": discovery.discover_qwen,
            "opencode": discovery.discover_opencode,
        }
        projects = provider_map[args.provider]()
    else:
        projects = discovery.discover_all()

    if getattr(args, "json_output", False):
        import json as _json
        result = [
            {
                "provider": p.provider.value,
                "name": p.name,
                "path": str(p.path),
                "session_files": len(p.session_files),
            }
            for p in projects
        ]
        print(_json.dumps(result))
        return

    if not projects:
        print(yellow("No projects found."))
        return

    by_provider: dict[str, list] = {}
    for p in projects:
        by_provider.setdefault(p.provider.value, []).append(p)

    for provider, projs in sorted(by_provider.items()):
        total_files = sum(len(p.session_files) for p in projs)
        print(f"\n  {bold(f'[{provider.upper()}]')} {len(projs)} projects, {total_files} session files")
        print(f"  {'─' * 50}")
        for p in projs:
            print(f"    {p.name:<30} {len(p.session_files):>4} files  {dim(str(p.path))}")

    total_projects = len(projects)
    total_files = sum(len(p.session_files) for p in projects)
    print(f"\n  Total: {total_projects} projects, {total_files} session files\n")


def cmd_backfill(args: argparse.Namespace) -> None:
    from hub.cache.event_store import EventStore

    store = EventStore()
    print(f"EventStore: {store.db_path}")
    print(f"Current events: {store.count():,}\n")
    print("Harvesters handle backfill automatically on dashboard startup.")
    print("Run 'python3 -m hub.cli dashboard' to start harvesting.")
    print(f"\nTotal in EventStore: {store.count():,}")
    store.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    import shutil
    import socket
    import sys
    from pathlib import Path

    from hub import __version__

    print(f"\n  {bold('MoolMesh Doctor')} v{__version__}\n")

    checks_ok = 0
    checks_fail = 0

    # Python version
    v = sys.version_info
    if v >= (3, 11):
        print(green(f"  ✓ Python {v.major}.{v.minor}.{v.micro}"))
        checks_ok += 1
    else:
        print(red(f"  ✗ Python {v.major}.{v.minor}.{v.micro} (requires 3.11+)"))
        checks_fail += 1

    # Events DB
    config_dir = Path.home() / ".moolmesh"
    events_db = config_dir / "events.db"
    if events_db.exists():
        size_mb = events_db.stat().st_size / (1024 * 1024)
        try:
            from hub.cache.event_store import EventStore
            store = EventStore()
            count = store.count()
            store.close()
            print(green(f"  ✓ events.db ({size_mb:.1f} MB, {count:,} events)"))
        except Exception:
            print(yellow(f"  ~ events.db ({size_mb:.1f} MB, unreadable)"))
        checks_ok += 1
    else:
        print(dim("  - events.db (not created yet)"))

    # GitHub DB
    github_db = config_dir / "github.db"
    if github_db.exists():
        size_mb = github_db.stat().st_size / (1024 * 1024)
        try:
            from hub.cache.git_store import GitStore
            store = GitStore()
            repos = store.list_repos()
            total_commits = sum(store.count_commits(r["id"]) for r in repos)
            store.close()
            print(green(f"  ✓ github.db ({size_mb:.1f} MB, {total_commits:,} commits)"))
        except Exception:
            print(yellow(f"  ~ github.db ({size_mb:.1f} MB, unreadable)"))
        checks_ok += 1
    else:
        print(dim("  - github.db (not created yet)"))

    # Repos
    from hub.config import load_config
    config = load_config()
    for r in config.repos:
        repo_path = Path(r.path)
        if repo_path.exists() and (repo_path / ".git").exists():
            print(green(f"  ✓ Repo: {r.owner}/{r.repo} (accessible)"))
            checks_ok += 1
        else:
            print(red(f"  ✗ Repo: {r.owner}/{r.repo} (not found: {r.path})"))
            checks_fail += 1

    # GitHub token
    from hub.config import get_github_token
    token = get_github_token(config)
    if token:
        source = "config" if config.github_token else "gh auth / env"
        print(green(f"  ✓ GitHub token: {source}"))
        checks_ok += 1
    else:
        print(yellow("  ~ GitHub token: not available (optional)"))

    # Port 5200
    port = 5200
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("localhost", port))
        sock.close()
        print(green(f"  ✓ Port {port}: available"))
        checks_ok += 1
    except OSError:
        from hub.daemon import read_pid
        pid = read_pid()
        if pid:
            print(green(f"  ✓ Port {port}: in use by MoolMesh daemon (PID {pid})"))
            checks_ok += 1
        else:
            print(yellow(f"  ~ Port {port}: in use by another process"))
            checks_fail += 1

    # Disk space
    usage = shutil.disk_usage(str(config_dir) if config_dir.exists() else str(Path.home()))
    free_gb = usage.free / (1024 ** 3)
    if free_gb > 1:
        print(green(f"  ✓ Disk: {free_gb:.0f} GB free"))
        checks_ok += 1
    else:
        print(red(f"  ✗ Disk: {free_gb:.1f} GB free (low)"))
        checks_fail += 1

    # Daemon status
    from hub.daemon import read_pid as _read_pid
    daemon_pid = _read_pid()
    if daemon_pid:
        print(green(f"  ✓ Daemon: running (PID {daemon_pid})"))
    else:
        print(dim("  - Daemon: not running"))

    print()
    if checks_fail == 0:
        print(green("  All checks passed.\n"))
    else:
        print(yellow(f"  {checks_ok} passed, {checks_fail} failed\n"))


def cmd_install(args: argparse.Namespace) -> None:
    import sys
    from pathlib import Path

    # Use the venv's Python, not the resolved base interpreter
    venv_python = Path(sys.prefix) / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable).resolve()

    local_bin = Path.home() / ".local" / "bin"
    target = local_bin / "mool"

    local_bin.mkdir(parents=True, exist_ok=True)

    wrapper = f"""#!/bin/sh
exec "{venv_python}" -m hub.cli "$@"
"""
    target.write_text(wrapper)
    target.chmod(0o755)

    print(green(f"Installed: {target}"))

    # Check if ~/.local/bin is in PATH
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(local_bin) not in path_dirs and str(local_bin.resolve()) not in path_dirs:
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            rc = "~/.zshrc"
        elif "bash" in shell:
            rc = "~/.bashrc"
        else:
            rc = "your shell rc file"
        print()
        print(yellow(f"  ~/.local/bin is not in your PATH."))
        print(f"  Add this line to {rc}:")
        print(f'    export PATH="$HOME/.local/bin:$PATH"')
        print(f"  Then restart your terminal.")
    else:
        print(dim("  ~/.local/bin is already in PATH — ready to use."))


def main() -> None:
    # Raise fd limit — some OS defaults (e.g. macOS 256) are too low for SQLite + many session files
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
    except (ImportError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="mool",
        description="MoolMesh — the context mesh for autonomous agents",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    subparsers = parser.add_subparsers(dest="command")

    # dashboard
    dash = subparsers.add_parser("dashboard", help="Start live dashboard")
    dash.add_argument("--port", type=int, default=5200, help="Server port (default: 5200)")
    dash.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    dash.add_argument("--project", help="Filter to project name (substring match)")
    dash.add_argument("--providers", help="Comma-separated providers: claude,codex,qwen,opencode")
    dash.add_argument("--log-level", default="INFO",
                      choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                      help="Logging level (default: INFO)")

    # daemon
    daemon = subparsers.add_parser("daemon", help="Run dashboard as background service")
    daemon_sub = daemon.add_subparsers(dest="daemon_command")

    d_start = daemon_sub.add_parser("start", help="Start daemon")
    d_start.add_argument("--port", type=int, default=5200, help="Server port (default: 5200)")
    d_start.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    d_start.add_argument("--project", help="Filter to project name")
    d_start.add_argument("--providers", help="Comma-separated providers")

    daemon_sub.add_parser("stop", help="Stop daemon")
    daemon_sub.add_parser("status", help="Show daemon status")

    d_restart = daemon_sub.add_parser("restart", help="Restart daemon")
    d_restart.add_argument("--port", type=int, default=5200, help="Server port (default: 5200)")
    d_restart.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    d_restart.add_argument("--project", help="Filter to project name")
    d_restart.add_argument("--providers", help="Comma-separated providers")

    # status (shortcut for daemon status)
    st = subparsers.add_parser("status", help="Show daemon status")
    st.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    # report
    rep = subparsers.add_parser("report", help="Generate batch analysis report")
    rep.add_argument("mode", nargs="?", default=None, choices=["auto"],
                     help="'auto': generate to ~/.moolmesh/reports/YYYY-MM-DD/")
    rep.add_argument("--project", help="Filter to project name (substring match)")
    rep.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode"], help="Filter by provider")
    rep.add_argument("--output", help="Output directory (default: reports/)")
    rep.add_argument("--daily", action="store_true", help="Only generate day-level reports (for auto mode)")
    rep.add_argument("--complete", action="store_true",
                     help="Full-content mode: no truncation, all messages, all operations")

    # discover
    disc = subparsers.add_parser("discover", help="List discovered projects")
    disc.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode"], help="Filter by provider")
    disc.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    # backfill
    bf = subparsers.add_parser("backfill", help="Import historical session data into EventStore")
    bf.add_argument("--full", action="store_true",
                    help="Full import (all data). Default: only import new events since last run.")

    # repo (con sub-subcommands)
    repo_parser = subparsers.add_parser("repo", help="Manage monitored git repositories")
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Register a repository")
    repo_add.add_argument("path", nargs="?", default=".", help="Path to git repo (default: current directory)")
    repo_add.add_argument("--days", type=int, default=14, metavar="N",
                          help="Days of history to ingest (default: 14)")
    repo_add.add_argument("--all", dest="all_history", action="store_true",
                          help="Ingest full history (ignores --days)")
    repo_add.add_argument("--no-github", action="store_true",
                           help="Don't poll GitHub API for this repo")

    repo_sub.add_parser("list", help="List registered repositories")

    repo_rm = repo_sub.add_parser("remove", help="Unregister a repository")
    repo_rm.add_argument("path", nargs="?", default=".", help="Path to repo (default: current directory)")

    repo_sync = repo_sub.add_parser("sync", help="Re-ingest commit history")
    repo_sync.add_argument("path", nargs="?", default=".", help="Path to git repo (default: current directory)")
    repo_sync.add_argument("--days", type=int, default=14, metavar="N",
                           help="Days of history to ingest (default: 14)")
    repo_sync.add_argument("--all", dest="all_history", action="store_true",
                           help="Ingest full history")

    # query (agent-friendly JSON output)
    query_parser = subparsers.add_parser("query", help="Query data as JSON (agent-friendly)")
    query_sub = query_parser.add_subparsers(dest="query_command")

    q_events = query_sub.add_parser("events", help="Recent events")
    q_events.add_argument("-n", "--limit", type=int, default=50, help="Max events (default: 50, max: 500)")

    q_sessions = query_sub.add_parser("sessions", help="Active sessions")
    q_sessions.add_argument("--hours", type=int, default=4, help="Lookback window in hours (default: 4)")

    q_tokens = query_sub.add_parser("tokens", help="Token usage by provider")
    q_tokens.add_argument("--provider", help="Filter by provider")
    q_tokens.add_argument("--since", help="ISO 8601 date (e.g. 2026-06-22)")

    q_tools = query_sub.add_parser("tools", help="Top tools used by agents")
    q_tools.add_argument("--project", help="Filter by project (substring)")
    q_tools.add_argument("--since", help="ISO 8601 date")
    q_tools.add_argument("-n", "--limit", type=int, default=20, help="Max results (default: 20)")

    q_search = query_sub.add_parser("search", help="Search events by text")
    q_search.add_argument("text", help="Text to search in event summaries")
    q_search.add_argument("--provider", help="Filter by provider")
    q_search.add_argument("--project", help="Filter by project (substring)")
    q_search.add_argument("--type", dest="event_type", help="Filter by event type")
    q_search.add_argument("--full", action="store_true", help="Search full text (slower)")
    q_search.add_argument("-n", "--limit", type=int, default=50, help="Max results (default: 50)")

    q_project = query_sub.add_parser("project", help="Project activity summary")
    q_project.add_argument("name", help="Project name (substring match)")
    q_project.add_argument("--since", help="ISO 8601 date")

    q_chain = query_sub.add_parser("chain", help="Session chain as JSON")
    q_chain.add_argument("session_id", help="Session ID to look up")

    # export
    p_export = subparsers.add_parser("export", help="Export session transcript")
    p_export.add_argument("session_id", help="Session ID to export")
    p_export.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p_export.add_argument("--output", "-o", help="Output file path")

    # sessions
    sess = subparsers.add_parser("sessions", help="List sessions with metadata")
    sess.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    sess.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode"], help="Filter by provider")
    sess.add_argument("--branch", help="Filter by git branch (exact match)")
    sess.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    # link
    p_link = subparsers.add_parser("link", help="Link two related sessions")
    p_link.add_argument("source", help="Source session ID")
    p_link.add_argument("target", help="Target session ID")
    p_link.add_argument("--type", choices=["continues", "references", "reviews"], default="continues")

    # chain
    p_chain = subparsers.add_parser("chain", help="Show sessions linked to a session")
    p_chain.add_argument("session_id", help="Session ID to look up")
    p_chain.add_argument("--json", action="store_true", dest="json_output")

    # detect-links
    p_detect = subparsers.add_parser("detect-links", help="Detect temporal links between sessions")
    p_detect.add_argument("--session", help="Specific session ID to analyze")
    p_detect.add_argument("--hours", type=float, default=4.0, help="Time window in hours (default 4)")
    p_detect.add_argument("--auto", action="store_true", help="Automatically store detected links")
    p_detect.add_argument("--json", action="store_true", dest="json_output")

    # doctor
    subparsers.add_parser("doctor", help="Run system diagnostics")

    # install
    subparsers.add_parser("install", help="Install mool command globally")

    args = parser.parse_args()

    if args.version:
        from hub import __version__
        print(f"moolmesh {__version__}")
        return

    match args.command:
        case "dashboard":
            cmd_dashboard(args)
        case "daemon":
            cmd_daemon(args)
        case "status":
            cmd_status(args)
        case "report":
            cmd_report(args)
        case "discover":
            cmd_discover(args)
        case "export":
            cmd_export(args)
        case "backfill":
            cmd_backfill(args)
        case "repo":
            cmd_repo(args)
        case "query":
            cmd_query(args)
        case "sessions":
            cmd_sessions(args)
        case "link":
            cmd_link(args)
        case "chain":
            cmd_chain(args)
        case "detect-links":
            cmd_detect_links(args)
        case "doctor":
            cmd_doctor(args)
        case "install":
            cmd_install(args)
        case _:
            parser.print_help()


def cmd_export(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.cache.event_store import EventStore

    store = EventStore()
    detail = store.get_session_detail(args.session_id)
    if not detail:
        print(red(f"Session not found: {args.session_id}"))
        store.close()
        return

    events = store.get_session_events(args.session_id, include_full_text=True)
    store.close()
    if not events:
        print(yellow(f"No events found for session: {args.session_id}"))
        return

    if args.format == "json":
        import json as _json
        print(_json.dumps({"session": detail, "events": events}, default=str, indent=2))
        return

    lines: list[str] = []
    title = detail.get("title") or detail.get("id", "Unknown")
    lines.append(f"# Session: {title}")
    lines.append("")
    provider = detail.get("provider", "")
    model = detail.get("model", "")
    branch = detail.get("git_branch", "")
    project = detail.get("project", "")
    meta_parts = []
    if provider:
        meta_parts.append(f"**Provider**: {provider}")
    if model:
        meta_parts.append(f"**Model**: {model}")
    if branch:
        meta_parts.append(f"**Branch**: {branch}")
    if project:
        meta_parts.append(f"**Project**: {project}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))
        lines.append("")
    first = detail.get("first_event_at", "")
    last = detail.get("last_event_at", "")
    if first and last:
        lines.append(f"**Started**: {first} — **Ended**: {last}")
        lines.append("")
    lines.append("---")
    lines.append("")

    for ev in events:
        ts = ev.get("timestamp", "")
        ts_short = ts[11:19] if len(ts) >= 19 else ts
        event_type = ev.get("event_type", "")
        full_text = ev.get("full_text") or ev.get("summary", "")
        tool_name = ev.get("tool_name")

        if event_type == "user":
            lines.append(f"### [{ts_short}] User")
            lines.append("")
            lines.append(full_text)
        elif event_type == "assistant":
            lines.append(f"### [{ts_short}] Assistant")
            lines.append("")
            lines.append(full_text)
        elif event_type in ("tool_use", "tool_result") and tool_name:
            lines.append(f"### [{ts_short}] Tool: {tool_name}")
            file_path = ev.get("file_path", "")
            if file_path:
                lines.append(f"File: `{file_path}`")
            lines.append("")
            if full_text and full_text != ev.get("summary"):
                lines.append("```")
                lines.append(full_text)
                lines.append("```")
        elif event_type == "thinking":
            lines.append(f"### [{ts_short}] Thinking")
            lines.append("")
            lines.append(f"*{full_text}*")
        else:
            lines.append(f"### [{ts_short}] {event_type}")
            lines.append("")
            lines.append(full_text)

        lines.append("")

    output = "\n".join(lines)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(green(f"Exported to {args.output}"))
    else:
        print(output)


def cmd_sessions(args: argparse.Namespace) -> None:
    from hub.mcp_server import EVENTS_DB, _get_sessions

    data = _get_sessions(
        EVENTS_DB,
        hours=args.hours,
        provider=args.provider,
        branch=args.branch,
    )

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(data, default=str))
        return

    if not data:
        label = f" on branch '{args.branch}'" if args.branch else ""
        print(yellow(f"No sessions found in the last {args.hours}h{label}."))
        return

    print(f"\n  {bold('Sessions')} (last {args.hours}h)")
    if args.branch:
        print(f"  Branch: {args.branch}")
    print(f"  {'─' * 70}")
    for s in data:
        provider = s.get("provider", "?")
        project = s.get("project", "unknown")
        title = s.get("title", "")
        branch = s.get("git_branch", "")
        model = s.get("model", "")
        events = s.get("event_count", 0)
        last = (s.get("last_event_at") or "")[:19]
        sid_short = (s.get("id") or "")[:12]

        line1 = f"    {bold(provider):>12}  {project:<30} {events:>5} events  {dim(last)}"
        print(line1)
        details = []
        if title:
            details.append(f"title={title[:50]}")
        if branch:
            details.append(f"branch={branch}")
        if model:
            details.append(f"model={model[:30]}")
        if details:
            print(f"              {dim(sid_short)}  {dim(' | '.join(details))}")
    print(f"\n  Total: {len(data)} sessions\n")


def cmd_link(args: argparse.Namespace) -> None:
    """Create an explicit link between two sessions."""
    from hub.cache.event_store import EventStore
    store = EventStore()

    source = store.get_session_detail(args.source)
    target = store.get_session_detail(args.target)

    if not source:
        print(red(f"Source session not found: {args.source}"))
        store.close()
        return
    if not target:
        print(red(f"Target session not found: {args.target}"))
        store.close()
        return

    link_type = args.type or "continues"
    created = store.link_sessions(
        source_session=source["id"],
        source_provider=source["provider"],
        target_session=target["id"],
        target_provider=target["provider"],
        link_type=link_type,
        confidence=1.0,
    )
    store.close()

    if created:
        print(green(f"Linked: {source['provider']}:{source['id'][:20]}... → {target['provider']}:{target['id'][:20]}... ({link_type})"))
    else:
        print(yellow("Link already exists."))


def cmd_chain(args: argparse.Namespace) -> None:
    """Show sessions linked to a given session."""
    import json as _json
    from hub.cache.event_store import EventStore
    store = EventStore()

    chain = store.get_session_chain(args.session_id)
    store.close()

    if not chain:
        print(yellow(f"No linked sessions found for: {args.session_id}"))
        return

    if getattr(args, "json_output", False):
        print(_json.dumps(chain, default=str))
        return

    print(f"\n  Session chain for {args.session_id[:30]}...")
    print(f"  {'─' * 60}")
    for link in chain:
        arrow = "←" if link["direction"] == "predecessor" else "→"
        title = link["title"][:40] if link["title"] else link["session_id"][:30]
        conf = f"{link['confidence']:.0%}" if link["confidence"] < 1.0 else ""
        conf_str = f" ({conf})" if conf else ""
        print(f"  {arrow} [{link['provider']:8}] {title}  {link['event_count']} events  {link['link_type']}{conf_str}")
    print()


def cmd_detect_links(args: argparse.Namespace) -> None:
    """Detect and optionally store temporal links between sessions."""
    import json as _json
    from hub.cache.event_store import EventStore
    store = EventStore()

    if args.session:
        candidates = store.detect_temporal_links(args.session, hours=args.hours)
    else:
        sessions = store.get_sessions(hours=int(args.hours))
        candidates = []
        seen: set[tuple[str, str]] = set()
        for s in sessions:
            for c in store.detect_temporal_links(s["id"], hours=args.hours):
                pair = tuple(sorted([s["id"], c["session_id"]]))
                if pair not in seen:
                    seen.add(pair)
                    c["source_session"] = s["id"]
                    c["source_provider"] = s["provider"]
                    candidates.append(c)

    if not candidates:
        print(yellow("No temporal links detected."))
        store.close()
        return

    if getattr(args, "json_output", False):
        print(_json.dumps(candidates, default=str))
        store.close()
        return

    print(f"\n  Detected {len(candidates)} potential link(s)")
    print(f"  {'─' * 60}")
    for c in candidates:
        title = c["title"][:40] if c["title"] else c["session_id"][:30]
        print(f"  [{c['provider']:8}] {title}  {c['shared_files']} shared files  {c['confidence']:.0%}")

    if args.auto:
        stored = 0
        for c in candidates:
            source = c.get("source_session", args.session)
            source_provider = c.get("source_provider", "")
            if not source_provider:
                detail = store.get_session_detail(source)
                source_provider = detail["provider"] if detail else ""
            if source_provider:
                created = store.link_sessions(
                    source_session=source,
                    source_provider=source_provider,
                    target_session=c["session_id"],
                    target_provider=c["provider"],
                    link_type="temporal",
                    confidence=c["confidence"],
                )
                if created:
                    stored += 1
        print(f"\n  Stored {stored} new link(s).")
    else:
        print(f"\n  Run with --auto to store these links.")
    print()
    store.close()


def cmd_query(args: argparse.Namespace) -> None:
    import json as _json
    from hub.mcp_server import (
        EVENTS_DB,
        _get_recent_events,
        _get_active_sessions,
        _get_token_usage,
        _get_tool_stats,
        _search_events,
        _search_session_content,
        _get_project_activity,
        _get_session_chain,
    )

    match args.query_command:
        case "events":
            data = _get_recent_events(EVENTS_DB, args.limit)
        case "sessions":
            data = _get_active_sessions(EVENTS_DB, args.hours)
        case "tokens":
            data = _get_token_usage(EVENTS_DB, args.provider, args.since)
        case "tools":
            data = _get_tool_stats(EVENTS_DB, args.project, args.since, args.limit)
        case "search":
            if getattr(args, "full", False):
                data = _search_session_content(
                    EVENTS_DB, args.text,
                    provider=args.provider,
                    project=args.project,
                    limit=args.limit,
                )
            else:
                data = _search_events(
                    EVENTS_DB, args.text,
                    provider=args.provider,
                    project=args.project,
                    event_type=args.event_type,
                    limit=args.limit,
                )
        case "project":
            data = _get_project_activity(EVENTS_DB, args.name, args.since)
        case "chain":
            data = _get_session_chain(EVENTS_DB, args.session_id)
        case _:
            print("Usage: mool query {events|sessions|tokens|tools|search|project|chain}")
            return

    print(_json.dumps(data, default=str))


def cmd_repo(args: argparse.Namespace) -> None:
    match args.repo_command:
        case "add":
            cmd_repo_add(args)
        case "list":
            cmd_repo_list(args)
        case "remove":
            cmd_repo_remove(args)
        case "sync":
            cmd_repo_sync(args)
        case _:
            print("Usage: mool repo {add|list|remove|sync}")


def cmd_repo_add(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import add_repo, save_config, load_config
    from hub.cache.git_store import GitStore
    from hub.harvesters.git_harvester import GitHarvester

    path = str(Path(args.path).resolve())

    try:
        repo_config = add_repo(path, no_github=args.no_github)
    except ValueError as e:
        print(red(f"Error: {e}"))
        return

    config = load_config()
    if any(r.path == path for r in config.repos):
        print(yellow(f"Already registered: {repo_config.owner}/{repo_config.repo}"))
        return

    config.repos.append(repo_config)
    save_config(config)

    store = GitStore()
    store.register_repo(repo_config)

    harvester = GitHarvester(store)
    days = None if args.all_history else args.days

    if args.all_history:
        print(dim("Ingesting full history — this may take several minutes..."))

    count = harvester.ingest_history(path, days=days)

    store.close()
    print(green(f"Registered {repo_config.owner}/{repo_config.repo}"))
    days_desc = "full history" if days is None else f"last {days} days"
    print(f"  Ingested {count} commits ({days_desc})")


def cmd_repo_list(args: argparse.Namespace) -> None:
    from hub.config import load_config
    from hub.cache.git_store import GitStore

    config = load_config()
    if not config.repos:
        print(yellow("No repositories registered."))
        print(dim("  Use: mool repo add /path/to/repo"))
        return

    store = GitStore()
    print(f"\n  {bold('Registered repositories')} ({len(config.repos)}):")
    print(f"  {'─' * 60}")
    for r in config.repos:
        repo_id = store.get_repo_id(r.path)
        commits = store.count_commits(repo_id) if repo_id else 0
        github = green("✓ GitHub") if r.github_enabled else dim("  local")
        print(f"    {r.owner}/{r.repo:<25} {commits:>5} commits  {github}")
        print(f"      {dim(r.path)}")
    store.close()
    print()


def cmd_repo_remove(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import remove_repo
    from hub.cache.git_store import GitStore

    path = str(Path(args.path).resolve())

    found = remove_repo(path)

    if not found:
        print(red(f"Not found: {path}"))
        return

    store = GitStore()
    store.remove_repo(path)
    store.close()

    print(green(f"Removed: {path}"))


def cmd_repo_sync(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import load_config
    from hub.cache.git_store import GitStore
    from hub.harvesters.git_harvester import GitHarvester

    path = str(Path(args.path).resolve())
    config = load_config()

    if not any(r.path == path for r in config.repos):
        print(red(f"Not registered: {path}"))
        print(dim("  Use: mool repo add /path/to/repo"))
        return

    store = GitStore()
    repo_id = store.get_repo_id(path)
    if repo_id is None:
        print(red(f"Not found in GitStore: {path}"))
        store.close()
        return

    harvester = GitHarvester(store)
    days = None if args.all_history else args.days

    if args.all_history:
        print(dim("Ingesting full history — this may take several minutes..."))

    count = harvester.ingest_history(path, days=days)
    store.close()

    days_desc = "full history" if days is None else f"last {days} days"
    print(green(f"Synced: {count} new commits ingested ({days_desc})"))


if __name__ == "__main__":
    main()
