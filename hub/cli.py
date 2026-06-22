"""CLI entry point for MoolMesh."""

from __future__ import annotations

import argparse

from hub.discovery import ProjectDiscovery


def cmd_dashboard(args: argparse.Namespace) -> None:
    from hub.dashboard.server import DashboardServer
    from hub.log import setup

    # Initialize logging with specified level (default: INFO)
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

    if not projects:
        print("No projects found.")
        return

    # Group by provider
    by_provider: dict[str, list] = {}
    for p in projects:
        by_provider.setdefault(p.provider.value, []).append(p)

    for provider, projs in sorted(by_provider.items()):
        total_files = sum(len(p.session_files) for p in projs)
        print(f"\n  [{provider.upper()}] {len(projs)} projects, {total_files} session files")
        print(f"  {'─' * 50}")
        for p in projs:
            print(f"    {p.name:<30} {len(p.session_files):>4} files  {p.path}")

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


def main() -> None:
    # Raise fd limit — macOS defaults to 256 which is too low for kqueue + SQLite
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
    except (ImportError, ValueError, OSError):
        pass  # Not critical — the active-only filtering is the real fix

    parser = argparse.ArgumentParser(
        prog="mool",
        description="MoolMesh — the context mesh for autonomous agents",
    )
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

    # backfill
    bf = subparsers.add_parser("backfill", help="Import historical session data into EventStore")
    bf.add_argument("--full", action="store_true",
                    help="Full import (all data). Default: only import new events since last run.")

    # repo (con sub-subcommands)
    repo_parser = subparsers.add_parser("repo", help="Gestionar repositorios git monitoreados")
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Registrar un repositorio")
    repo_add.add_argument("path", help="Ruta al repositorio git")
    repo_add.add_argument("--days", type=int, default=14, metavar="N",
                          help="Días de historial a ingestar (default: 14)")
    repo_add.add_argument("--all", dest="all_history", action="store_true",
                          help="Ingestar historial completo (ignora --days, puede tardar)")
    repo_add.add_argument("--no-github", action="store_true",
                           help="No consultar API de GitHub para este repo")

    repo_sub.add_parser("list", help="Listar repositorios registrados")

    repo_rm = repo_sub.add_parser("remove", help="Eliminar un repositorio")
    repo_rm.add_argument("path", help="Ruta del repositorio a eliminar")

    repo_sync = repo_sub.add_parser("sync", help="Re-ingestar historial de un repo ya registrado")
    repo_sync.add_argument("path", help="Ruta al repositorio git")
    repo_sync.add_argument("--days", type=int, default=14, metavar="N",
                           help="Días de historial a ingestar (default: 14)")
    repo_sync.add_argument("--all", dest="all_history", action="store_true",
                           help="Ingestar historial completo")

    args = parser.parse_args()

    match args.command:
        case "dashboard":
            cmd_dashboard(args)
        case "report":
            cmd_report(args)
        case "discover":
            cmd_discover(args)
        case "backfill":
            cmd_backfill(args)
        case "repo":
            cmd_repo(args)
        case _:
            parser.print_help()


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
            print("Uso: mool repo {add|list|remove|sync}")


def cmd_repo_add(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import add_repo, save_config, load_config
    from hub.cache.git_store import GitStore
    from hub.harvesters.git_harvester import GitHarvester

    path = str(Path(args.path).resolve())

    try:
        repo_config = add_repo(path, no_github=args.no_github)
    except ValueError as e:
        print(f"Error: {e}")
        return

    config = load_config()
    # Verificar que no esté ya registrado
    if any(r.path == path for r in config.repos):
        print(f"Ya registrado: {repo_config.owner}/{repo_config.repo}")
        return

    config.repos.append(repo_config)
    save_config(config)

    # Registrar en GitStore e ingestar historial
    store = GitStore()
    store.register_repo(repo_config)

    harvester = GitHarvester(store)
    days = None if args.all_history else args.days

    if args.all_history:
        print("Ingestando historial completo — puede tardar varios minutos...")

    count = harvester.ingest_history(path, days=days)

    store.close()
    print(f"Registrado {repo_config.owner}/{repo_config.repo}")
    days_desc = "historial completo" if days is None else f"últimos {days} días"
    print(f"Ingestados {count} commits ({days_desc})")


def cmd_repo_list(args: argparse.Namespace) -> None:
    from hub.config import load_config
    from hub.cache.git_store import GitStore

    config = load_config()
    if not config.repos:
        print("No hay repositorios registrados.")
        print("Usa: mool repo add /path/to/repo")
        return

    store = GitStore()
    print(f"\n  Repositorios registrados ({len(config.repos)}):")
    print(f"  {'─' * 60}")
    for r in config.repos:
        repo_id = store.get_repo_id(r.path)
        commits = store.count_commits(repo_id) if repo_id else 0
        github = "✓ GitHub" if r.github_enabled else "  local"
        print(f"    {r.owner}/{r.repo:<25} {commits:>5} commits  {github}")
        print(f"      {r.path}")
    store.close()
    print()


def cmd_repo_remove(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import remove_repo
    from hub.cache.git_store import GitStore

    path = str(Path(args.path).resolve())

    found = remove_repo(path)

    if not found:
        print(f"No encontrado: {path}")
        return

    # También limpiar de GitStore
    store = GitStore()
    store.remove_repo(path)
    store.close()

    print(f"Eliminado: {path}")


def cmd_repo_sync(args: argparse.Namespace) -> None:
    from pathlib import Path
    from hub.config import load_config
    from hub.cache.git_store import GitStore
    from hub.harvesters.git_harvester import GitHarvester

    path = str(Path(args.path).resolve())
    config = load_config()

    if not any(r.path == path for r in config.repos):
        print(f"No registrado: {path}")
        print("Usa: mool repo add /path/to/repo")
        return

    store = GitStore()
    repo_id = store.get_repo_id(path)
    if repo_id is None:
        print(f"No encontrado en GitStore: {path}")
        store.close()
        return

    harvester = GitHarvester(store)
    days = None if args.all_history else args.days

    if args.all_history:
        print("Ingestando historial completo — puede tardar varios minutos...")

    count = harvester.ingest_history(path, days=days)
    store.close()

    days_desc = "historial completo" if days is None else f"últimos {days} días"
    print(f"Sincronizado: {count} commits nuevos ingestados ({days_desc})")


if __name__ == "__main__":
    main()
