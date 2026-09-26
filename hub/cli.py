"""CLI entry point for MoolMesh."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hub.colors import green, yellow, red, dim, bold
from hub.discovery import ProjectDiscovery


def _configure_stdio_encoding() -> None:
    """Fuerza stdout/stderr a UTF-8 en Windows.

    La consola por defecto de Windows usa cp1252, que no puede codificar
    caracteres no-ASCII (p.ej. el '→' del texto de ayuda) y hace crashear el
    CLI con UnicodeEncodeError. errors="replace" garantiza que nunca crashee
    aunque la consola no soporte el glifo. Ver issue #31.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


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


def _parse_since(value: str | None) -> float | None:
    """``--since YYYY-MM-DD`` → epoch seconds at local midnight (None = all)."""
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(value, "%Y-%m-%d").timestamp()
    except ValueError:
        print(red(f"  --since inválido: {value!r} (formato YYYY-MM-DD)"))
        sys.exit(2)


def _print_backfill_report(report, verbose: bool) -> None:
    from hub.config import load_config, masked_label
    hide = load_config().hide_project_names
    title = "Backfill (simulación, no escribe)" if report.dry_run else "Backfill"
    print(f"\n  {bold(title)} — {report.elapsed:.1f}s")
    print(f"  {'─' * 66}")
    tot_ev = tot_new = 0
    for r in report.providers:
        skipped = r.skipped_cloud + r.skipped_error + r.skipped_empty
        print(f"  {bold(r.provider.upper())}")
        print(f"    archivos vistos:        {r.seen:>7,}")
        verb = "a procesar" if report.dry_run else "procesados"
        print(f"    {verb + ':':<24}{r.processed:>7,}   "
              f"({r.pending_bytes / 1_048_576:,.1f} MB nuevos)")
        print(f"    ya al día:              {r.up_to_date:>7,}")
        print(f"    en ventana del daemon:  {r.in_window:>7,}   (los lee el daemon en vivo)")
        print(f"    salteados:              {skipped:>7,}   "
              f"(nube {r.skipped_cloud}, error {r.skipped_error}, vacíos {r.skipped_empty})")
        if r.cloud_dirs:
            print(yellow(f"    directorios en la nube sin listar: {r.cloud_dirs}"))
        if r.fingerprint_collisions:
            print(yellow(f"    archivos con huella repetida (1er KB igual): "
                         f"{r.fingerprint_collisions}"))
        if not report.dry_run:
            print(f"    eventos insertados:     {r.events_inserted:>7,}   "
                  f"(de {r.events_parsed:,} leídos; el resto ya existía)")
            print(f"    sesiones nuevas:        {r.new_sessions:>7,}   "
                  f"({r.sessions_before:,} → {r.sessions_after:,})")
            print(dim(f"    transacción más larga:  {r.max_txn_seconds * 1000:.0f} ms"))
            tot_ev += r.events_inserted
            tot_new += r.new_sessions
        if r.limit_reached:
            print(yellow("    --limit alcanzado: volvé a correr para continuar"))
        if verbose:
            for path in r.processed_paths:
                shown = Path(path).name if hide else path
                print(dim(f"      + {shown}"))
            for reason, path in r.skipped_paths:
                shown = masked_label(path, True) if hide else path
                print(dim(f"      - [{reason}] {shown}"))
    print(f"  {'─' * 66}")
    if not report.dry_run:
        print(f"  Total: {tot_ev:,} eventos insertados, {tot_new:,} sesiones nuevas")
    print(dim(f"  Archivos modificados en las últimas {report.window_hours} h se dejan "
              "al daemon (sin choque con el proceso en vivo)."))
    if report.interrupted:
        print(yellow("  Interrumpido: lo ya guardado queda; volvé a correr para continuar."))
    print()


def cmd_backfill_reparse(args: argparse.Namespace) -> None:
    """``mool backfill --reparse codex``: re-ingest stored Codex sessions (#45)."""
    from hub.backfill import ReparseReport, run_reparse_codex
    from hub.cache.event_store import DEFAULT_DB_PATH, EventStore

    real = args.yes and not args.dry_run
    store = EventStore() if real else None
    report = ReparseReport()
    try:
        run_reparse_codex(store, dry_run=not real, yes=args.yes,
                          db_path=DEFAULT_DB_PATH, report=report)
    finally:
        if store is not None:
            store.close()

    title = "Re-parseo de Codex" + ("" if real else " (simulación, no escribe)")
    print(f"\n  {bold(title)} — {report.elapsed:.1f}s")
    print(f"  {'─' * 66}")
    print(f"    sesiones de Codex guardadas:  {report.stored_sessions:>7,}")
    print(f"    a re-parsear:                 {report.sessions:>7,}   "
          f"({report.groups:,} grupos de rollouts)")
    print(f"    eventos a borrar:             {report.events_to_delete:>7,}")
    print(f"    eventos a insertar:           {report.events_to_insert:>7,}")
    if real:
        print(f"    eventos insertados:           {report.events_inserted:>7,}")
    print(f"    ya al día:                    {report.up_to_date:>7,}")
    print(f"    sin rollout en disco:         {report.no_rollout:>7,}   (no se tocan)")
    print(f"    en ventana del daemon:        {report.in_window:>7,}   (no se tocan)")
    if report.skipped_cloud:
        print(yellow(f"    rollouts en la nube:          {report.skipped_cloud:>7,}   (no se tocan)"))
    if report.failed:
        print(red(f"    grupos revertidos por error:  {report.failed:>7,}   (ver log)"))
    if report.backup_path:
        print(green(f"  Backup previo de events.db: {report.backup_path}"))
    if report.needs_yes:
        print(yellow("  Para ejecutarlo: mool backfill --reparse codex --yes "
                     "(se hace un backup de events.db antes de borrar)."))
    if report.interrupted:
        print(yellow("  Interrumpido: el grupo en curso se revirtió; volvé a correr."))
    print()
    if report.interrupted:
        sys.exit(130)


def cmd_backfill(args: argparse.Namespace) -> None:
    from hub.backfill import FILE_PROVIDERS, run_backfill
    from hub.cache.event_store import DEFAULT_DB_PATH, EventStore

    if getattr(args, "reparse", None):
        cmd_backfill_reparse(args)
        return

    providers = FILE_PROVIDERS if args.provider in (None, "all") else (args.provider,)
    since = _parse_since(args.since)
    store = None if args.dry_run else EventStore()
    print(f"  EventStore: {DEFAULT_DB_PATH}")

    def progress(rep) -> None:
        print(dim(f"  [{rep.provider}] {rep.processed:,} archivos, "
                  f"{rep.events_inserted:,} eventos…"), flush=True)

    from hub.backfill import BackfillReport
    report = BackfillReport()
    try:
        run_backfill(
            store, providers=providers, since=since, dry_run=args.dry_run,
            limit=args.limit, db_path=DEFAULT_DB_PATH,
            progress=None if args.dry_run else progress, report=report,
        )
    finally:
        if store is not None:
            store.close()
    _print_backfill_report(report, args.verbose)
    if report.interrupted:
        sys.exit(130)


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


def cmd_mcp_setup(args: argparse.Namespace) -> None:
    import json
    import platform
    import shutil
    import subprocess
    import sys

    target = getattr(args, "target", "claude-code")

    # ── Detect install method ──────────────────────────────────────
    # sys.executable is already the interpreter of the pipx / uv-tool / venv
    # environment that has `hub` installed (and is Windows-correct).
    is_pipx = "pipx" in sys.prefix
    in_venv = sys.prefix != sys.base_prefix

    if is_pipx:
        method = "pipx"
    elif in_venv:
        method = "venv"
    else:
        method = "pip"
    python = sys.executable

    # ── Build the MCP config ───────────────────────────────────────
    # Module form, never `uv run <script>`: uv runs a loose script in an
    # ephemeral env without the `hub` package → ImportError (#38).
    server_cmd = [python, "-m", "hub.mcp_server"]

    print(bold("MoolMesh MCP Setup\n"))
    print(f"  Python:    {python}")
    print(f"  Server:    {' '.join(server_cmd)}")
    print(f"  Install:   {method}")
    print()

    # ── Check mcp dependency ───────────────────────────────────────
    try:
        subprocess.run(
            [str(python), "-c", "import mcp"],
            capture_output=True, check=True, timeout=10,
        )
        mcp_available = True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        mcp_available = False

    if not mcp_available:
        print(yellow("  ⚠ The 'mcp' package is not installed in this environment."))
        if method == "pipx":
            inject_cmd = ["pipx", "inject", "moolmesh", "mcp"]
        else:
            inject_cmd = [str(python), "-m", "pip", "install", "mcp"]

        print(f"  Run:  {bold(' '.join(inject_cmd))}")
        print()

        if getattr(args, "install_mcp", False) and not getattr(args, "dry_run", False):
            print(dim(f"  Running: {' '.join(inject_cmd)}"))
            try:
                subprocess.run(inject_cmd, check=True, timeout=120)
                mcp_available = True
                print(green("  ✓ mcp installed successfully.\n"))
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
                print(red(f"  ✗ Failed: {exc}"))
                return
        else:
            print(dim("  Use --install-deps to install it automatically."))
            print()

    if not mcp_available:
        return

    config_block = {
        "command": server_cmd[0],
        "args": server_cmd[1:],
    }

    # ── Target: claude-code ────────────────────────────────────────
    if target == "claude-code":
        claude_bin = shutil.which("claude")
        if claude_bin:
            already = _claude_code_has_mcp_user(claude_bin, "moolmesh")
            if already:
                print(dim("  Replacing existing user-scope registration."))
                subprocess.run(
                    [claude_bin, "mcp", "remove", "moolmesh", "-s", "user"],
                    capture_output=True, timeout=15,
                )

            print(dim("  Registering via: claude mcp add --scope user"))
            cmd = [
                claude_bin, "mcp", "add",
                "--scope", "user",
                "--transport", "stdio",
                "moolmesh",
            ] + server_cmd
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=30)
                if result.returncode == 0:
                    verb = "updated" if already else "registered"
                    print(green(f"  ✓ MCP server 'moolmesh' {verb} globally in Claude Code."))
                else:
                    err = result.stderr.strip() or result.stdout.strip()
                    print(yellow(f"  ⚠ claude mcp add returned: {err}"))
                    print(dim("  You can add it manually — config shown below."))
                    _print_mcp_json(config_block)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                print(yellow("  ⚠ Could not run 'claude mcp add'."))
                _print_mcp_json(config_block)
        else:
            print(yellow("  'claude' CLI not found in PATH."))
            print(dim("  Add this to ~/.claude.json under mcpServers:"))
            _print_mcp_json(config_block)

    # ── Target: JSON-config clients ───────────────────────────────
    elif target in ("claude-desktop", "cursor", "qwen"):
        config_path = _mcp_config_path(target, platform.system())
        if not config_path:
            print(red(f"  Unsupported platform: {platform.system()}"))
            _print_mcp_json(config_block)
            return
        client_names = {"claude-desktop": "Claude Desktop", "cursor": "Cursor", "qwen": "Qwen CLI"}
        _write_mcp_json_config(config_path, config_block, client_names[target], args)

    # ── Target: opencode (different JSON schema) ───────────────────
    elif target == "opencode":
        config_path = _mcp_config_path(target, platform.system())
        if not config_path:
            print(red(f"  Unsupported platform: {platform.system()}"))
            return
        oc_block = {
            "type": "local",
            "command": [config_block["command"]] + config_block["args"],
            "enabled": True,
        }
        _write_opencode_mcp(config_path, oc_block, args)

    # ── Target: codex (TOML format) ────────────────────────────────
    elif target == "codex":
        config_path = _mcp_config_path(target, platform.system())
        if not config_path:
            print(red(f"  Unsupported platform: {platform.system()}"))
            return
        _write_codex_mcp(config_path, server_cmd, args)

    # ── Target: json (just print) ──────────────────────────────────
    elif target == "json":
        _print_mcp_json(config_block)

    print()


def _mcp_config_path(target: str, system: str):
    from pathlib import Path
    import os
    if target == "claude-desktop":
        if system == "Darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if system == "Linux":
            return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        if system == "Windows":
            appdata = os.environ.get("APPDATA", "")
            return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    elif target == "cursor":
        return Path.home() / ".cursor" / "mcp.json"
    elif target == "qwen":
        # TODO: Windows path unverified — may need %APPDATA%\Qwen\ instead of ~/.qwen/
        return Path.home() / ".qwen" / "settings.json"
    elif target == "opencode":
        # TODO: Windows path unverified — may need %APPDATA%\opencode\ instead of ~/.config/opencode/
        return Path.home() / ".config" / "opencode" / "opencode.json"
    elif target == "codex":
        return Path.home() / ".codex" / "config.toml"
    return None


def _write_mcp_json_config(config_path, config_block: dict, client_name: str, args) -> None:
    import json
    from hub.colors import green, yellow, red, dim

    if getattr(args, "dry_run", False):
        print(f"  Config file: {config_path}")
        print(dim("  Would add:"))
        _print_mcp_json(config_block)
        return

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(red(f"  ✗ Cannot parse {config_path}: {exc}"))
            print(dim("  Add manually:"))
            _print_mcp_json(config_block)
            return

    mcp_servers = existing.setdefault("mcpServers", {})
    already = "moolmesh" in mcp_servers
    if already:
        print(yellow(f"  ⚠ 'moolmesh' already exists in {client_name} config."))
        print(dim("  Overwriting with updated config."))
        print()
    mcp_servers["moolmesh"] = config_block
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    verb = "Updated" if already else "Written to"
    print(green(f"  ✓ {verb} {config_path}"))
    print(dim(f"  Restart {client_name} to load the new server."))


def _write_opencode_mcp(config_path, oc_block: dict, args) -> None:
    import json
    from hub.colors import green, yellow, red, dim

    if getattr(args, "dry_run", False):
        print(f"  Config file: {config_path}")
        print(dim("  Would add:"))
        print(json.dumps({"mcp": {"moolmesh": oc_block}}, indent=2))
        return

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(red(f"  ✗ Cannot parse {config_path}: {exc}"))
            return

    mcp = existing.setdefault("mcp", {})
    already = "moolmesh" in mcp
    if already:
        print(yellow("  ⚠ 'moolmesh' already exists in OpenCode config."))
        print(dim("  Overwriting with updated config."))
        print()
    mcp["moolmesh"] = oc_block
    config_path.write_text(json.dumps(existing, indent=2) + "\n")
    verb = "Updated" if already else "Written to"
    print(green(f"  ✓ {verb} {config_path}"))
    print(dim("  Restart OpenCode to load the new server."))


def _toml_str(s: str) -> str:
    """Encode s as a TOML basic string (#38).

    A JSON string literal is a valid TOML basic string here: backslashes and
    quotes get escaped, so a Windows path is not read as invalid escapes.
    ASCII-only output, so it's safe whatever encoding write_text uses.
    """
    import json
    return json.dumps(s)


def _codex_mcp_entry(server_cmd: list) -> str:
    entry = f"[mcp_servers.moolmesh]\ncommand = {_toml_str(server_cmd[0])}\n"
    if len(server_cmd) > 1:
        args_str = ", ".join(_toml_str(a) for a in server_cmd[1:])
        entry += f"args = [{args_str}]\n"
    return entry


def _write_codex_mcp(config_path, server_cmd: list, args) -> None:
    from hub.colors import green, yellow, dim

    entry = _codex_mcp_entry(server_cmd)
    shown = "".join(f"  {line}\n" for line in entry.splitlines())

    if getattr(args, "dry_run", False):
        print(f"  Config file: {config_path}")
        print(dim("  Would add:"))
        print(f"\n{shown}")
        return

    content = config_path.read_text() if config_path.exists() else ""
    already = "mcp_servers.moolmesh" in content

    if already:
        print(yellow("  ⚠ 'moolmesh' already exists in Codex config."))
        print(dim("  Check ~/.codex/config.toml and update manually:"))
        print(f"\n{shown}")
        return

    config_path.write_text(content + "\n" + entry)
    print(green(f"  ✓ Written to {config_path}"))
    print(dim("  Restart Codex to load the new server."))


def _claude_code_has_mcp_user(claude_bin: str, name: str) -> bool:
    import json
    from pathlib import Path
    claude_json = Path.home() / ".claude.json"
    try:
        data = json.loads(claude_json.read_text())
        return name in data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _print_mcp_json(config_block: dict) -> None:
    import json
    wrapper = {"mcpServers": {"moolmesh": config_block}}
    print()
    print(json.dumps(wrapper, indent=2))
    print()


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
    # UTF-8 en Windows antes de imprimir nada (issue #31).
    _configure_stdio_encoding()

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
    rep.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode", "cursor"], help="Filter by provider")
    rep.add_argument("--output", help="Output directory (default: reports/)")
    rep.add_argument("--daily", action="store_true", help="Only generate day-level reports (for auto mode)")
    rep.add_argument("--complete", action="store_true",
                     help="Full-content mode: no truncation, all messages, all operations")

    # discover
    disc = subparsers.add_parser("discover", help="List discovered projects")
    disc.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode", "cursor"], help="Filter by provider")
    disc.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    # backfill
    bf = subparsers.add_parser(
        "backfill",
        help="Import historical session files (claude, codex, qwen) into EventStore",
    )
    bf.add_argument("--provider", choices=["claude", "codex", "qwen", "all"], default="all",
                    help="Provider to ingest (default: all file-based providers)")
    bf.add_argument("--since", metavar="YYYY-MM-DD",
                    help="Only files modified on/after this local date")
    bf.add_argument("--dry-run", action="store_true",
                    help="Count what would be processed (files, bytes); write nothing")
    bf.add_argument("--limit", type=int, metavar="N",
                    help="Process at most N files with new data, then stop (re-run continues)")
    bf.add_argument("--verbose", action="store_true",
                    help="List processed/skipped files (masked under hide_project_names)")
    bf.add_argument("--reparse", choices=["codex"],
                    help="Re-ingest stored sessions with the current parser (backs up events.db)")
    bf.add_argument("--yes", action="store_true",
                    help="Confirm --reparse (without it, --reparse only simulates)")
    bf.add_argument("--full", action="store_true", help=argparse.SUPPRESS)  # legacy no-op

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
    sess.add_argument("--provider", choices=["claude", "codex", "qwen", "opencode", "cursor"], help="Filter by provider")
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

    # workspace (path→workspace attribution, issue #20)
    ws_parser = subparsers.add_parser(
        "workspace", help="Path→workspace attribution (recovers session↔project M:N)"
    )
    ws_sub = ws_parser.add_subparsers(dest="workspace_command")

    ws_sub.add_parser(
        "backfill",
        help="Populate workspace.db from existing events (read-only pass over events.db)",
    )

    ws_list = ws_sub.add_parser("list", help="List known workspaces")
    ws_list.add_argument("--json", action="store_true", dest="json_output")

    ws_session = ws_sub.add_parser("session", help="Workspaces a session touched")
    ws_session.add_argument("session_id", help="Session ID to look up")
    ws_session.add_argument("--provider", help="Filter by provider")
    ws_session.add_argument("--json", action="store_true", dest="json_output")

    ws_sessions = ws_sub.add_parser("sessions", help="Sessions that touched a workspace")
    ws_sessions.add_argument("workspace_key", help="Workspace key (see 'workspace list')")
    ws_sessions.add_argument("--json", action="store_true", dest="json_output")

    ws_touches = ws_sub.add_parser(
        "touches", help="Filesystem path-touches attributed to a workspace (#21)"
    )
    ws_touches.add_argument("workspace_key", help="Workspace key (see 'workspace list')")
    ws_touches.add_argument("--json", action="store_true", dest="json_output")

    ws_sub.add_parser(
        "rollup",
        help="Rebuild the machine-wide portfolio rollup (session+filesystem+git) (#22)",
    )

    ws_sub.add_parser(
        "classify",
        help="Classify workspaces into the #24 taxonomy (collapse harness, group)",
    )

    ws_portfolio = ws_sub.add_parser(
        "portfolio", help="Show the hot workspaces from the portfolio rollup (#22)"
    )
    ws_portfolio.add_argument("--since", help="ISO date lower bound (compares by day)")
    ws_portfolio.add_argument("--json", action="store_true", dest="json_output")
    ws_portfolio.add_argument(
        "--grouped", action="store_true",
        help="Hierarchical view: real projects with harness collapsed (#24)",
    )

    ws_delivery = ws_sub.add_parser(
        "delivery", help="Show delivery candidates (quiescence + a 2nd signal) (#22)"
    )
    ws_delivery.add_argument("--json", action="store_true", dest="json_output")

    # workspace root — manage marked filesystem roots (issue #21, opt-in)
    ws_root = ws_sub.add_parser(
        "root", help="Manage marked filesystem roots for the workspace watcher"
    )
    ws_root_sub = ws_root.add_subparsers(dest="root_command")

    ws_root_add = ws_root_sub.add_parser("add", help="Mark a folder as a workspace root")
    ws_root_add.add_argument("path", help="Absolute path to observe (opt-in; '/' allowed)")
    ws_root_add.add_argument(
        "--max-depth", type=int, default=None, dest="max_depth",
        help="Bound the recursive scan depth (default 6)",
    )
    ws_root_add.add_argument(
        "--exclude", action="append", default=None, dest="excludes",
        help="Extra directory name to prune (repeatable)",
    )

    ws_root_rm = ws_root_sub.add_parser("remove", help="Unmark a workspace root")
    ws_root_rm.add_argument("path", help="Path to stop observing")

    ws_root_sub.add_parser("list", help="List marked workspace roots")

    # mcp
    mcp_parser = subparsers.add_parser("mcp", help="MCP server management")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")

    mcp_setup = mcp_sub.add_parser("setup", help="Configure MCP server for an AI client")
    mcp_setup.add_argument(
        "target", nargs="?", default="claude-code",
        choices=["claude-code", "claude-desktop", "cursor", "codex", "qwen", "opencode", "json"],
        help="Target client (default: claude-code)",
    )
    mcp_setup.add_argument("--install-deps", action="store_true", dest="install_mcp",
                           help="Auto-install the 'mcp' package if missing")
    mcp_setup.add_argument("--dry-run", action="store_true",
                           help="Show what would be written without modifying files")

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
        case "workspace":
            cmd_workspace(args)
        case "sessions":
            cmd_sessions(args)
        case "link":
            cmd_link(args)
        case "chain":
            cmd_chain(args)
        case "detect-links":
            cmd_detect_links(args)
        case "mcp":
            if getattr(args, "mcp_command", None) == "setup":
                cmd_mcp_setup(args)
            else:
                mcp_parser.print_help()
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


def cmd_workspace(args: argparse.Namespace) -> None:
    match getattr(args, "workspace_command", None):
        case "backfill":
            cmd_workspace_backfill(args)
        case "list":
            cmd_workspace_list(args)
        case "session":
            cmd_workspace_session(args)
        case "sessions":
            cmd_workspace_sessions(args)
        case "touches":
            cmd_workspace_touches(args)
        case "rollup":
            cmd_workspace_rollup(args)
        case "classify":
            cmd_workspace_classify(args)
        case "portfolio":
            cmd_workspace_portfolio(args)
        case "delivery":
            cmd_workspace_delivery(args)
        case "root":
            cmd_workspace_root(args)
        case _:
            print(
                "Usage: mool workspace {backfill|list|session|sessions|touches|"
                "rollup|classify|portfolio|delivery|root}"
            )


def cmd_workspace_backfill(args: argparse.Namespace) -> None:
    from hub.cache.event_store import DEFAULT_DB_PATH as EVENTS_DB_PATH
    from hub.cache.workspace_store import WorkspaceStore

    if not EVENTS_DB_PATH.exists():
        print(yellow(f"No events.db found at {EVENTS_DB_PATH} — nothing to attribute."))
        return

    store = WorkspaceStore()
    print(dim(f"Reading events (read-only): {EVENTS_DB_PATH}"))
    print(dim(f"Writing attributions:      {store.db_path}"))
    result = store.backfill_from_events(EVENTS_DB_PATH)
    # Fold the freshly-attributed edges into the portfolio rollup so the
    # dashboard/MCP portfolio view is populated straight after a backfill.
    rollup = store.build_rollup()
    store.detect_delivery_candidates()
    # Classify the (possibly new) workspaces for the grouped portfolio view (#24).
    cls = store.classify_workspaces(EVENTS_DB_PATH)
    store.close()

    print(green(
        f"Attributed {result['attributed']} path-touches "
        f"across {result['workspaces']} workspaces "
        f"({result['directories']} directories resolved)."
    ))
    if result["cwd_attributed"]:
        print(dim(
            f"  {result['cwd_attributed']} cwd-fallback edges for sessions "
            f"with no absolute file path (attributed by working directory)."
        ))
    if result["skipped_non_absolute"]:
        print(dim(
            f"  Skipped {result['skipped_non_absolute']} non-absolute file_path "
            f"rows (Bash command strings, not paths)."
        ))
    print(dim(
        f"  Attribution cursor set to event id {result['cursor']} "
        f"(the daemon continues incrementally from here)."
    ))
    print(dim(
        f"  Rollup: {rollup['rows']} day-rows across {rollup['workspaces']} "
        f"workspaces ({rollup['multi_source_nodes']} multi-signal)."
    ))
    print(dim(
        f"  Classified into {cls['projects']} projects; "
        f"{cls['collapsed_harness']} harness collapsed, "
        f"{cls['unclassified']} unclassified."
    ))


def cmd_workspace_rollup(args: argparse.Namespace) -> None:
    from hub.cache.event_store import DEFAULT_DB_PATH as EVENTS_DB_PATH
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    print(dim(f"Building portfolio rollup: {store.db_path}"))
    r = store.build_rollup()
    # Delivery detection reads the same real clocks — refresh it in the same pass.
    d = store.detect_delivery_candidates()
    # Refresh the #24 classification so the grouped portfolio stays in sync.
    cls = store.classify_workspaces(EVENTS_DB_PATH)
    store.close()
    print(green(
        f"Rollup built: {r['rows']} day-rows across {r['workspaces']} workspaces."
    ))
    print(dim(
        f"  {r['multi_source_nodes']} workspaces lit by ≥2 signals; "
        f"git repos: {r['git_repos_matched']} matched, {r['git_repos_new']} new."
    ))
    print(dim(
        f"  Delivery candidates: {d['candidates']} "
        f"({d['quiescent_workspaces']} quiescent) — {d['by_signal']}."
    ))
    print(dim(
        f"  Classified into {cls['projects']} projects; "
        f"{cls['collapsed_harness']} harness collapsed, "
        f"{cls['unclassified']} unclassified."
    ))


def cmd_workspace_classify(args: argparse.Namespace) -> None:
    from hub.cache.event_store import DEFAULT_DB_PATH as EVENTS_DB_PATH
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    print(dim(f"Classifying workspaces (events.db read-only): {store.db_path}"))
    c = store.classify_workspaces(EVENTS_DB_PATH)
    store.close()
    print(green(
        f"Classified {c['classified']} workspaces into {c['projects']} projects."
    ))
    print(dim(
        f"  {c['collapsed_harness']} harness folders collapsed; "
        f"{c['unclassified']} unclassified. Categories: {c['by_category']}."
    ))
    print(dim(f"  Resolved via: {c['by_resolved_via']}."))


def cmd_workspace_delivery(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    rows = store.get_delivery_candidates()
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow(
            "No delivery candidates. Run: mool workspace rollup (runs the detector)."
        ))
        return

    from hub.config import load_config, masked_label
    hide = load_config().hide_project_names

    print(f"\n  {bold('Delivery candidates')} ({len(rows)}):")
    print(dim("  candidate with confidence — never a fact; a 2nd signal is recorded"))
    print(f"  {'─' * 70}")
    for r in rows:
        label = r["remote_url"] or r["root_path"] or r["dir_path"] or r["workspace_key"]
        label = masked_label(label, hide)
        detail = r["signal_detail"] or ""
        if hide and r["signal"] == "root_artifact":
            detail = masked_label(detail, True)
        print(f"  {label}")
        print(dim(
            f"    conf {r['confidence']}  signal={r['signal']} ({detail})  "
            f"quiet since {r['quiescent_since']}"
        ))


def cmd_workspace_portfolio(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    if getattr(args, "grouped", False):
        grouped = store.get_portfolio_grouped(getattr(args, "since", None))
        store.close()
        # This CLI view keeps the flat project list (no client tier); still drop
        # the internal join-only fields (_day_set/_remote_url/_anchor_path) the
        # store now attaches for the dashboard's #29 hierarchy — they must never
        # reach output (and the two path fields would bypass masking).
        from hub.cache.portfolio_clients import strip_internal
        strip_internal(grouped)
        _print_portfolio_grouped(grouped, getattr(args, "json_output", False))
        return
    rows = store.get_portfolio(getattr(args, "since", None))
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow("Portfolio empty. Run: mool workspace rollup (after backfill)."))
        return

    from hub.config import load_config, masked_label
    hide = load_config().hide_project_names

    print(f"\n  {bold('Portfolio')} ({len(rows)} workspaces):")
    print(f"  {'─' * 70}")
    for r in rows:
        label = r["remote_url"] or r["root_path"] or r["dir_path"] or r["workspace_key"]
        label = masked_label(label, hide)
        srcs = "+".join(r["sources"]) or "—"
        meta = (
            f"[{srcs}] {r['session_touches']}s/{r['fs_touches']}f/"
            f"{r['git_touches']}g, {r['active_days']}d"
        )
        print(f"  {label}")
        print(dim(f"    {meta}  last: {r['last_activity'] or '—'}"))


def _print_portfolio_grouped(grouped: dict, json_output: bool) -> None:
    from hub.config import load_config, masked_label
    hide = load_config().hide_project_names

    if json_output:
        import json as _json
        # Reuse the MCP masker so --json hides EVERY name field (project labels,
        # child/orphan remote_url/root_path/dir_path) exactly like the dashboard
        # and MCP surfaces — a hand-rolled pass leaked the raw paths (#24 DoD:
        # masking en toda salida nueva).
        from hub.mcp_server import _mask_grouped
        print(_json.dumps(_mask_grouped(grouped, hide), default=str))
        return

    projects = grouped.get("projects", [])
    if not projects and not grouped.get("unclassified"):
        print(yellow("Portfolio empty. Run: mool workspace classify (after rollup)."))
        return

    s = grouped.get("summary", {})
    print(f"\n  {bold('Portfolio')} — {s.get('projects', 0)} proyectos, "
          f"{s.get('collapsed_harness', 0)} carpetas de harness colapsadas, "
          f"{s.get('unclassified', 0)} sin clasificar:")
    print(f"  {'─' * 70}")
    for p in projects:
        label = masked_label(p.get("project_label") or p["project_key"], hide)
        srcs = "+".join(p["sources"]) or "—"
        extra = f"  (+{p['collapsed_harness']} harness)" if p["collapsed_harness"] else ""
        print(f"  {bold(label)}{extra}")
        print(dim(
            f"    [{srcs}] {p['session_touches']}s/{p['fs_touches']}f/"
            f"{p['git_touches']}g, {p['active_days']}d  last: {p['last_activity'] or '—'}"
        ))
        for c in p["children"]:
            clabel = masked_label(c.get("dir_path") or c.get("root_path") or "", hide)
            print(dim(f"      └ [{c['category']}/{c['subtype']}] {clabel}"))

    orphans = grouped.get("unclassified", [])
    if orphans:
        print(f"\n  {bold('Sin clasificar / herramientas')} ({len(orphans)}):")
        for o in orphans:
            olabel = masked_label(o.get("dir_path") or o.get("root_path") or "", hide)
            print(dim(f"    · [{o['subtype']}] {olabel}"))


def cmd_workspace_list(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    rows = store.list_workspaces()
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow("No workspaces yet. Run: mool workspace backfill"))
        return

    from hub.config import load_config, masked_label
    hide = load_config().hide_project_names

    print(f"\n  {bold('Workspaces')} ({len(rows)}):")
    print(f"  {'─' * 70}")
    for r in rows:
        label = r["remote_url"] or r["root_path"] or r["dir_path"] or r["workspace_key"]
        label = masked_label(label, hide)
        meta = (
            f"{r['sessions']} sessions, {r['attributions']} files, "
            f"{r.get('touches', 0)} fs-touches  ·  {r['workspace_key']}"
        )
        print(f"    [{r['kind']:<10}] {label}")
        print(f"      {dim(meta)}")
    print()


def cmd_workspace_session(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    rows = store.get_session_workspaces(args.session_id, getattr(args, "provider", None))
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow(f"No workspaces attributed to session {args.session_id[:20]}..."))
        print(dim("  (run 'mool workspace backfill' first)"))
        return

    print(f"\n  Workspaces touched by {args.session_id[:30]}...")
    print(f"  {'─' * 60}")
    for r in rows:
        label = r["remote_url"] or r["root_path"] or r["dir_path"] or r["workspace_key"]
        print(f"    [{r['kind']:<10}] {label}  {dim(str(r['files']) + ' files')}")
    print()


def cmd_workspace_sessions(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore

    store = WorkspaceStore()
    rows = store.get_workspace_sessions(args.workspace_key)
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow(f"No sessions attributed to workspace {args.workspace_key}"))
        return

    print(f"\n  Sessions that touched {args.workspace_key}")
    print(f"  {'─' * 60}")
    for r in rows:
        print(f"    [{r['provider']:<8}] {r['session_id'][:40]}  {dim(str(r['files']) + ' files')}")
    print()


def cmd_workspace_touches(args: argparse.Namespace) -> None:
    from hub.cache.workspace_store import WorkspaceStore
    from hub.config import load_config, masked_label

    store = WorkspaceStore()
    rows = store.get_workspace_touches(args.workspace_key)
    store.close()

    if getattr(args, "json_output", False):
        import json as _json
        print(_json.dumps(rows, default=str))
        return

    if not rows:
        print(yellow(f"No filesystem touches for workspace {args.workspace_key}"))
        print(dim("  (mark a root: 'mool workspace root add <path>')"))
        return

    hide = load_config().hide_project_names
    print(f"\n  Filesystem touches in {args.workspace_key} ({len(rows)}):")
    print(f"  {'─' * 60}")
    for r in rows:
        path = masked_label(r["path"], hide)
        print(f"    {path}  {dim('· ' + r['source'])}")
    print()


def cmd_workspace_root(args: argparse.Namespace) -> None:
    from hub.config import (
        add_workspace_root,
        list_workspace_roots,
        remove_workspace_root,
        WORKSPACE_ROOT_DEFAULT_MAX_DEPTH,
    )

    match getattr(args, "root_command", None):
        case "add":
            depth = args.max_depth if args.max_depth is not None else WORKSPACE_ROOT_DEFAULT_MAX_DEPTH
            root = add_workspace_root(args.path, depth, args.excludes)
            print(green(f"Marked workspace root: {root.path}"))
            print(dim(f"  max_depth={root.max_depth}, extra excludes={root.excludes or '[]'}"))
            print(dim("  The watcher observes it on the next daemon start."))
        case "remove":
            if remove_workspace_root(args.path):
                print(green(f"Unmarked workspace root: {args.path}"))
            else:
                print(yellow(f"Not a marked root: {args.path}"))
        case "list":
            roots = list_workspace_roots()
            if not roots:
                print(yellow("No workspace roots marked (opt-in — nothing observed)."))
                print(dim("  Mark one: 'mool workspace root add <path>'"))
                return
            print(f"\n  {bold('Workspace roots')} ({len(roots)}):")
            print(f"  {'─' * 60}")
            for r in roots:
                print(f"    {r.path}")
                print(dim(f"      max_depth={r.max_depth}, excludes={r.excludes or '[]'}"))
            print()
        case _:
            print("Usage: mool workspace root {add|remove|list}")


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
    # count < 0 (GIT_READ_FAILED): el repo quedó registrado (el add tuvo éxito)
    # pero el backfill del historial falló. Avisamos sin exit no-cero — el add
    # sí funcionó — y no lo reportamos como "0 commits" (#34). El retry se hace
    # con `mool repo sync`, que sí devuelve exit no-cero ante el fallo.
    if count < 0:
        print(yellow(f"  Could not read git history in {path} (see logs) — "
                     f"run 'mool repo sync {path}' to retry"))
    else:
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

    # count < 0 (GIT_READ_FAILED): git falló leyendo el historial — no es "0
    # commits". Reportamos error real + exit no-cero en vez de mentir (#34).
    if count < 0:
        print(red(f"Error reading git in {path} (see logs)"))
        sys.exit(1)

    days_desc = "full history" if days is None else f"last {days} days"
    print(green(f"Synced: {count} new commits ingested ({days_desc})"))


if __name__ == "__main__":
    main()
