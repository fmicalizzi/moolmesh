"""Dashboard HTTP server with SSE support."""

from __future__ import annotations

import collections
import http.server
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from hub.cache.event_store import EventStore


class SessionTracker:
    """Tracks per-project, per-session live analytics from events."""

    def __init__(self):
        self._lock = threading.Lock()
        # project_key -> session stats
        self.projects: dict[str, dict] = {}
        # tool usage histogram
        self.tool_counts: dict[str, int] = {}
        # per-provider token totals
        self.provider_tokens: dict[str, dict[str, int]] = {}
        # timeline: list of (timestamp_epoch, provider, event_type)
        self.timeline: list[tuple[float, str, str]] = []

    def track(self, event_dict: dict) -> None:
        with self._lock:
            provider = event_dict.get("provider", "")
            project = event_dict.get("project", "unknown")
            session_id = event_dict.get("session_id", "")
            cwd = event_dict.get("cwd", "")
            event_type = event_dict.get("event_type", "")
            ts = event_dict.get("timestamp", "")
            model = event_dict.get("model")
            tokens = event_dict.get("tokens")

            key = f"{provider}:{project}"
            if key not in self.projects:
                self.projects[key] = {
                    "provider": provider,
                    "project": project,
                    "cwd": cwd,
                    "sessions": set(),
                    "events": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tool_calls": 0,
                    "models": set(),
                    "last_event": "",
                    "last_event_type": "",
                    "first_seen": ts,
                }

            p = self.projects[key]
            p["events"] += 1
            p["last_event"] = ts
            p["last_event_type"] = event_type
            if cwd:
                p["cwd"] = cwd
            if session_id:
                p["sessions"].add(session_id)
            if model:
                p["models"].add(model)
            if tokens:
                p["input_tokens"] += tokens.get("input", 0)
                p["output_tokens"] += tokens.get("output", 0)

            # Tools
            tool = event_dict.get("tool_name")
            if tool:
                p["tool_calls"] += 1
                self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1

            # Provider tokens
            if provider not in self.provider_tokens:
                self.provider_tokens[provider] = {"input": 0, "output": 0}
            if tokens:
                self.provider_tokens[provider]["input"] += tokens.get("input", 0)
                self.provider_tokens[provider]["output"] += tokens.get("output", 0)

            # Timeline (keep last 200)
            try:
                from datetime import datetime
                epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError, AttributeError):
                epoch = time.time()
            self.timeline.append((epoch, provider, event_type))
            if len(self.timeline) > 200:
                self.timeline = self.timeline[-200:]

    def get_projects(self) -> list[dict]:
        with self._lock:
            result = []
            for key, p in self.projects.items():
                result.append({
                    "provider": p["provider"],
                    "project": p["project"],
                    "cwd": p["cwd"],
                    "sessions": len(p["sessions"]),
                    "events": p["events"],
                    "input_tokens": p["input_tokens"],
                    "output_tokens": p["output_tokens"],
                    "tool_calls": p["tool_calls"],
                    "models": sorted(p["models"]),
                    "last_event": p["last_event"],
                    "last_event_type": p["last_event_type"],
                })
            # Sort by last_event descending (most recent first)
            result.sort(key=lambda x: x["last_event"], reverse=True)
            return result

    def get_tool_stats(self) -> list[dict]:
        with self._lock:
            items = sorted(self.tool_counts.items(), key=lambda x: x[1], reverse=True)
            return [{"name": k, "count": v} for k, v in items[:20]]

    def get_provider_tokens(self) -> dict:
        with self._lock:
            return dict(self.provider_tokens)

    def get_timeline(self) -> list[dict]:
        with self._lock:
            return [
                {"t": t, "provider": p, "type": et}
                for t, p, et in self.timeline[-100:]
            ]


class DashboardServer:
    """Orchestrates harvesters and HTTP server."""

    MAX_RECENT = 500

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5200,
        project_filter: str | None = None,
        providers: list[str] | None = None,
    ):
        self.host = host
        self.port = port
        self._start_time = time.monotonic()

        # Shared SSE buffer — harvesters push here, SSE handler reads
        # deque is thread-safe in CPython (GIL), O(1) append, auto-discards oldest
        self.sse_buffer: collections.deque = collections.deque(maxlen=1000)
        self._sse_lock = threading.Lock()
        self.sse_clients: list[collections.deque] = []
        self.project_filter = project_filter

        # Stats
        self.stats = {
            "total_events": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tool_calls": 0,
            "active_providers": set(),
        }
        self.tracker = SessionTracker()
        self.event_store = EventStore()

        # Determine which providers to harvest
        active = set(providers) if providers else {"claude", "codex", "qwen", "opencode"}

        # Harvesters — write directly to SQLite, push to sse_buffer
        self.watchers: list = []
        if "claude" in active:
            from hub.watchers.claude_watcher import ClaudeWatcher
            self.claude_watcher = ClaudeWatcher(
                self.event_store, self.sse_buffer, project_filter
            )
            self.watchers.append(("Claude Code", self.claude_watcher))
        if "codex" in active:
            from hub.watchers.codex_watcher import CodexWatcher
            self.codex_watcher = CodexWatcher(self.event_store, self.sse_buffer)
            self.watchers.append(("Codex (GPT-5.x)", self.codex_watcher))
        if "qwen" in active:
            from hub.watchers.qwen_watcher import QwenWatcher
            self.qwen_watcher = QwenWatcher(
                self.event_store, self.sse_buffer, project_filter
            )
            self.watchers.append(("Qwen CLI", self.qwen_watcher))
        if "opencode" in active:
            from hub.watchers.opencode_watcher import OpenCodeWatcher
            self.opencode_watcher = OpenCodeWatcher(self.event_store, self.sse_buffer)
            self.watchers.append(("OpenCode", self.opencode_watcher))

        # GitHarvester — runs in background for registered repos
        self.git_store: GitStore | None = None
        self.git_harvester: Any | None = None
        self.github_harvester: Any | None = None
        self.github_sse_buffer: collections.deque = collections.deque(maxlen=500)
        self.github_client: Any | None = None
        self.digest_engine: Any | None = None
        self.ollama_client: Any | None = None
        from hub.config import load_config
        config = load_config()
        if config.repos:
            from hub.cache.git_store import GitStore
            self.git_store = GitStore()
            from hub.harvesters.git_harvester import GitHarvester
            self.git_harvester = GitHarvester(self.git_store, self.sse_buffer)
            # GitHub API harvester (requires token)
            from hub.config import get_github_token
            token = get_github_token(config)
            if token:
                from hub.integrations.github_client import GitHubClient
                from hub.harvesters.github_harvester import GitHubHarvester
                self.github_client = GitHubClient(token)
                self.github_harvester = GitHubHarvester(
                    self.git_store, self.github_client, self.github_sse_buffer
                )

            # Digest engine + LLM client (optional)
            from hub.integrations import create_llm_client
            llm_api_key = self._resolve_llm_key(config)
            llm_client = create_llm_client(
                provider=config.llm_provider,
                api_url=config.llm_api_url,
                model=config.llm_model,
                api_key=llm_api_key,
            )
            self.ollama_client = llm_client

            from hub.digests.engine import DigestEngine
            self.digest_engine = DigestEngine(self.git_store, llm_client)

        # Load persisted events into tracker for stats
        stored = self.event_store.load_recent(500)
        if stored:
            for ev in stored:
                self.tracker.track(ev)
                self.stats["total_events"] += 1
                self.stats["active_providers"].add(ev.get("provider", ""))
                t = ev.get("tokens")
                if t:
                    self.stats["total_input_tokens"] += t.get("input", 0)
                    self.stats["total_output_tokens"] += t.get("output", 0)
                if ev.get("tool_name"):
                    self.stats["tool_calls"] += 1

    @staticmethod
    def _resolve_llm_key(config) -> str:
        """Resuelve API key: config > LLM_API_KEY > provider-specific env."""
        if config.llm_api_key:
            return config.llm_api_key
        if key := os.getenv("LLM_API_KEY"):
            return key
        provider_env = {
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "together": "TOGETHER_API_KEY",
            "groq": "GROQ_API_KEY",
            "ollama": "OLLAMA_API_KEY",
        }
        env_name = provider_env.get(config.llm_provider, "")
        if env_name:
            return os.getenv(env_name, "")
        return ""

    def start(self) -> None:
        """Start harvesters and HTTP server.

        No gap_fill, no backfill, no second pass. The harvester's first cycle
        reads from the last SQLite offset (or 0 for new files), which IS the
        backfill. No race conditions possible — each file has one reader.
        """
        # Ensure logging is initialized (idempotent if already called from CLI)
        from hub.log import setup
        setup()

        from hub import __version__
        print()
        print("               ██")
        print("             ██▓▓██")
        print("           ██▓▓▓▓▓▓██")
        print("         ██▓▓▓▓▓▓▓▓▓▓██")
        print("       ██▓▓▓▓▓▓▓▓▓▓▓▓▓▓██")
        print("       ██████████████████")
        print("         M O O L M E S H")
        print()
        print("    congregating scattered signals")
        print("    into shared understanding")
        print(f"    v{__version__}")
        print()

        for name, watcher in self.watchers:
            watcher.start()
            # Give harvester a moment to do initial scan
            time.sleep(0.1)
            print(f"  {name}: watching {watcher.watched_count} files")

        # Start GitHarvester if configured
        if self.git_harvester:
            self.git_harvester.start()
            print(f"  GitHarvester: monitoring {len(self.git_store.list_repos())} repos")
        # Start GitHubHarvester if configured
        if self.github_harvester:
            self.github_harvester.start()
            print("  GitHubHarvester: polling GitHub API (token ✓)")

        # Start SSE broadcaster (reads from sse_buffer, pushes to clients)
        threading.Thread(target=self._broadcast_sse, daemon=True).start()

        # Start HTTP server (blocking) — detect existing instance or auto-increment
        handler = self._make_handler()
        server = None
        for attempt in range(10):
            try:
                server = http.server.ThreadingHTTPServer((self.host, self.port), handler)
                break
            except OSError:
                if attempt == 0:
                    # Check if the port is already running MoolMesh
                    try:
                        import json as _json
                        from urllib.request import urlopen
                        with urlopen(f"http://{self.host}:{self.port}/health", timeout=2) as resp:
                            health = _json.loads(resp.read())
                        if health.get("status") == "healthy":
                            print(f"\n  MoolMesh is already running on port {self.port}")
                            print(f"  Dashboard → http://{self.host}:{self.port}")
                            print("  Use 'mool daemon stop' to stop it, or --port to use a different port.\n")
                            return
                    except Exception:
                        pass
                    print(f"\n  Port {self.port} in use, trying next...")
                self.port += 1
        if server is None:
            print(f"\n  Could not find an available port. Exiting.")
            return
        print(f"\n  Dashboard → http://{self.host}:{self.port}")
        print("  Press Ctrl+C to stop\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            for _, watcher in self.watchers:
                watcher.stop()
            if self.git_harvester:
                self.git_harvester.stop()
            if self.github_harvester:
                self.github_harvester.stop()
            if self.git_store:
                self.git_store.close()
            self.event_store.close()
            server.shutdown()

    def _broadcast_sse(self) -> None:
        """Read events from sse_buffer and broadcast to connected SSE clients.

        Also updates in-memory stats and tracker. Consumes events from the
        deque via popleft() — same pattern as SSE client handlers.
        """
        while True:
            drained = False
            while self.sse_buffer:
                try:
                    event_dict = self.sse_buffer.popleft()
                except IndexError:
                    break
                drained = True

                # Update stats
                self.stats["total_events"] += 1
                self.stats["active_providers"].add(event_dict.get("provider", ""))
                t = event_dict.get("tokens")
                if t:
                    self.stats["total_input_tokens"] += t.get("input", 0)
                    self.stats["total_output_tokens"] += t.get("output", 0)
                if event_dict.get("tool_name"):
                    self.stats["tool_calls"] += 1

                # Track session analytics
                self.tracker.track(event_dict)

                # Broadcast to SSE clients
                with self._sse_lock:
                    for client_buf in self.sse_clients:
                        client_buf.append(event_dict)

            if not drained:
                time.sleep(0.5)

    def register_sse_client(self) -> collections.deque:
        client_buf: collections.deque = collections.deque(maxlen=200)
        with self._sse_lock:
            self.sse_clients.append(client_buf)
        return client_buf

    def unregister_sse_client(self, client_buf: collections.deque) -> None:
        with self._sse_lock:
            if client_buf in self.sse_clients:
                self.sse_clients.remove(client_buf)

    def get_stats_dict(self) -> dict:
        total_watched = sum(w.watched_count for _, w in self.watchers)
        return {
            "total_events": self.stats["total_events"],
            "total_input_tokens": self.stats["total_input_tokens"],
            "total_output_tokens": self.stats["total_output_tokens"],
            "tool_calls": self.stats["tool_calls"],
            "active_providers": list(self.stats["active_providers"]),
            "sse_clients": len(self.sse_clients),
            "watched_files": total_watched,
        }

    def _make_handler(self) -> type:
        server_ref = self
        static_dir = Path(__file__).parent / "static"

        class Handler(http.server.BaseHTTPRequestHandler):
            @staticmethod
            def _parse_int(params, key, default=0):
                try:
                    return int(params.get(key, [default])[0])
                except (ValueError, TypeError):
                    return default

            def do_GET(self):
                match self.path:
                    case "/":
                        self._serve_file(static_dir / "dashboard.html", "text/html")
                    case "/analytics":
                        self._serve_file(static_dir / "analytics.html", "text/html")
                    case _ if self.path.startswith("/api/events"):
                        self._serve_sse()
                    case "/api/recent":
                        # Read directly from SQLite — always consistent
                        # Returns {events, max_id} for snapshot+stream pattern
                        recent = server_ref.event_store.load_recent(500)
                        max_id = server_ref.event_store.get_max_id()
                        self._serve_json({"events": recent, "max_id": max_id})
                    case "/api/stats":
                        self._serve_json(server_ref.get_stats_dict())
                    case "/api/sessions":
                        self._serve_json(server_ref.event_store.get_project_summary())
                    case "/api/tools":
                        self._serve_json(server_ref.tracker.get_tool_stats())
                    case "/api/provider-tokens":
                        self._serve_json(server_ref.tracker.get_provider_tokens())
                    case "/api/timeline":
                        self._serve_json(server_ref.tracker.get_timeline())
                    case "/api/db-stats":
                        self._serve_json(server_ref.event_store.stats_summary())
                    case _ if self.path.startswith("/api/history"):
                        self._serve_history()
                    case _ if self.path.startswith("/api/analytics"):
                        self._serve_analytics()
                    case "/api/generate-reports":
                        self._serve_generate_reports()
                    case "/api/report-status":
                        self._serve_json(getattr(server_ref, '_last_report', {"status": "idle"}))
                    # --- Pantallas nuevas ---
                    case "/projects":
                        self._serve_file(static_dir / "projects.html", "text/html")
                    case "/timeline":
                        self._serve_file(static_dir / "timeline.html", "text/html")
                    # --- API: Repos ---
                    case "/api/repos":
                        if server_ref.git_store:
                            self._serve_json(server_ref.git_store.list_repos())
                        else:
                            self._serve_json([])
                    # --- API: GitHub ---
                    case _ if self.path.startswith("/api/github/issues"):
                        self._serve_github_issues()
                    case _ if self.path.startswith("/api/github/prs"):
                        self._serve_github_prs()
                    case _ if self.path.startswith("/api/github/milestones"):
                        self._serve_github_milestones()
                    case _ if self.path.startswith("/api/github/project-board"):
                        self._serve_github_project_board()
                    # --- API: Timeline ---
                    case _ if self.path.startswith("/api/timeline/commits"):
                        self._serve_timeline_commits()
                    case _ if self.path.startswith("/api/timeline/authors"):
                        self._serve_timeline_authors()
                    case _ if self.path.startswith("/api/timeline/hot-files"):
                        self._serve_timeline_hot_files()
                    case _ if self.path.startswith("/api/timeline/digest-history"):
                        self._serve_timeline_digest_history()
                    case _ if self.path.startswith("/api/timeline/digest"):
                        self._serve_timeline_digest()
                    case _ if self.path.startswith("/api/timeline/pending"):
                        self._serve_timeline_pending()
                    case _ if self.path.startswith("/api/timeline/commit-days"):
                        self._serve_timeline_commit_days()
                    case "/health":
                        from hub import __version__
                        uptime = int(time.monotonic() - server_ref._start_time)
                        self._serve_json({
                            "status": "healthy",
                            "version": __version__,
                            "uptime_seconds": uptime,
                            "events_count": server_ref.stats["total_events"],
                        })
                    case _:
                        self.send_error(404)

            def _serve_file(self, path: Path, content_type: str):
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_json(self, data):
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                # Determine replay point: Last-Event-ID header (browser auto-sends
                # on reconnect) or ?last_id query param (initial connect)
                last_id = 0
                header_id = self.headers.get("Last-Event-ID")
                if header_id:
                    try:
                        last_id = int(header_id)
                    except (ValueError, TypeError):
                        pass
                if not last_id:
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    try:
                        last_id = int(qs.get("last_id", [0])[0])
                    except (ValueError, TypeError):
                        pass

                # Send retry interval (ms) so browser reconnects quickly
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()

                # Replay missed events from SQLite
                if last_id > 0:
                    missed = server_ref.event_store.load_since_id(last_id)
                    for ev in missed:
                        data = json.dumps(ev)
                        self.wfile.write(f"id: {ev['id']}\ndata: {data}\n\n".encode())
                    if missed:
                        self.wfile.flush()

                # Register for live events (after replay to avoid dups)
                client_buf = server_ref.register_sse_client()
                last_keepalive = time.monotonic()
                try:
                    while True:
                        events_sent = False
                        while client_buf:
                            try:
                                event = client_buf.popleft()
                                # Skip events already replayed
                                eid = event.get("id")
                                if eid and last_id and eid <= last_id:
                                    continue
                                data = json.dumps(event)
                                if eid:
                                    self.wfile.write(f"id: {eid}\ndata: {data}\n\n".encode())
                                else:
                                    self.wfile.write(f"data: {data}\n\n".encode())
                                events_sent = True
                            except IndexError:
                                break
                        if events_sent:
                            self.wfile.flush()
                            last_keepalive = time.monotonic()
                        else:
                            now = time.monotonic()
                            if now - last_keepalive >= 20.0:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                last_keepalive = now
                            time.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server_ref.unregister_sse_client(client_buf)

            def _serve_analytics(self):
                """Handle /api/analytics?period=day|week|full queries."""
                from urllib.parse import urlparse, parse_qs
                from datetime import datetime, timedelta
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                period = params.get("period", ["full"])[0]

                since = None
                # Use naive local time — event timestamps are stored without
                # timezone info, so comparisons must use the same format.
                now = datetime.now()
                if period == "day":
                    since = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
                elif period == "week":
                    since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

                result = server_ref.event_store.analytics(since=since)
                result["period"] = period
                self._serve_json(result)

            def _serve_generate_reports(self):
                """Trigger batch report generation in a background thread."""
                import threading
                from pathlib import Path
                from hub.batch_reporter import generate_report

                output_dir = Path.home() / ".moolmesh" / "reports"

                def run():
                    try:
                        results = generate_report(output_dir=output_dir)
                        server_ref._last_report = {
                            "status": "done",
                            "output_dir": str(output_dir),
                            "projects": len([r for r in results if not r.get("skipped")]),
                            "total_files": sum(r.get("files", 0) for r in results),
                            "results": results,
                        }
                    except Exception as e:
                        server_ref._last_report = {"status": "error", "error": str(e)}

                if getattr(server_ref, '_report_running', False):
                    self._serve_json({"status": "already_running"})
                    return

                server_ref._report_running = True
                server_ref._last_report = {"status": "running"}

                def wrapper():
                    try:
                        run()
                    finally:
                        server_ref._report_running = False

                threading.Thread(target=wrapper, daemon=True).start()
                self._serve_json({"status": "started", "output_dir": str(output_dir)})

            def _serve_history(self):
                """Handle /api/history?provider=X&project=Y&limit=N queries."""
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                result = server_ref.event_store.query(
                    provider=params.get("provider", [None])[0],
                    project=params.get("project", [None])[0],
                    session_id=params.get("session_id", [None])[0],
                    event_type=params.get("event_type", [None])[0],
                    since=params.get("since", [None])[0],
                    limit=self._parse_int(params, "limit", 500),
                )
                self._serve_json(result)

            # --- GitHub API handlers ---
            def _serve_github_issues(self):
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                state = params.get("state", [None])[0]
                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.get_issues(repo_id, state))
                else:
                    self._serve_json([])

            def _serve_github_prs(self):
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.get_pr_pipeline(repo_id))
                else:
                    self._serve_json({})

            def _serve_github_milestones(self):
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.get_milestones(repo_id))
                else:
                    self._serve_json([])

            def _serve_github_project_board(self):
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.get_project_board(repo_id))
                else:
                    self._serve_json({})

            # --- Timeline API handlers ---
            def _serve_timeline_commits(self):
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                since = params.get("since", [None])[0]
                until = params.get("until", [None])[0]
                limit = self._parse_int(params, "limit", 50)
                if server_ref.git_store and repo_id:
                    self._serve_json(
                        server_ref.git_store.get_commits(repo_id, since=since, until=until, limit=limit)
                    )
                else:
                    self._serve_json([])

            def _serve_timeline_authors(self):
                from urllib.parse import urlparse, parse_qs
                from datetime import datetime, timedelta
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                # Si llegan since/until explícitos, usarlos; si no, fallback a days=7
                since = params.get("since", [None])[0]
                until = params.get("until", [None])[0]
                if not since:
                    days = self._parse_int(params, "days", 7)
                    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.get_author_stats(repo_id, since=since, until=until))
                else:
                    self._serve_json([])

            def _serve_timeline_hot_files(self):
                from urllib.parse import urlparse, parse_qs
                from datetime import datetime, timedelta
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                # Si llegan since/until explícitos, usarlos; si no, fallback a days=7
                since = params.get("since", [None])[0]
                until = params.get("until", [None])[0]
                limit = self._parse_int(params, "limit", 20)
                if not since:
                    days = self._parse_int(params, "days", 7)
                    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
                if server_ref.git_store and repo_id:
                    self._serve_json(
                        server_ref.git_store.get_hot_files(repo_id, since=since, until=until, limit=limit)
                    )
                else:
                    self._serve_json([])

            def _serve_timeline_digest(self):
                from urllib.parse import urlparse, parse_qs
                from datetime import datetime, timedelta
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                _yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                date = params.get("date", [_yesterday])[0]
                period = params.get("period", ["daily"])[0]
                force = params.get("force", ["0"])[0] == "1"

                if not server_ref.git_store or not repo_id:
                    self._serve_json({"level": 0, "stats": {}, "text": ""})
                    return

                # Determinar si la fecha es de la semana actual
                today = datetime.now().date()
                current_week_start = today - timedelta(days=today.weekday())  # lunes de esta semana
                try:
                    req_date = datetime.strptime(date, "%Y-%m-%d").date()
                    is_historical = req_date < current_week_start
                except ValueError:
                    is_historical = False

                # force=True siempre habilita LLM; histórico sin force → no LLM
                allow_llm = force or not is_historical

                # Usar DigestEngine si disponible
                if server_ref.digest_engine:
                    # Obtener repo_name para display
                    repos = server_ref.git_store.list_repos()
                    repo_name = ""
                    for r in repos:
                        if r["id"] == repo_id:
                            repo_name = f"{r['owner']}/{r['repo_name']}"
                            break

                    if period == "weekly":
                        d = datetime.strptime(date, "%Y-%m-%d")
                        week_start = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
                        digest = server_ref.digest_engine.get_weekly_digest(
                            repo_id, week_start, repo_name,
                            force_refresh=force,
                            allow_llm=allow_llm
                        )
                    else:
                        digest = server_ref.digest_engine.get_daily_digest(
                            repo_id, date, repo_name,
                            force_refresh=force,
                            allow_llm=allow_llm
                        )

                    self._serve_json(digest)
                else:
                    # Fallback: L1 stats only
                    if period == "daily":
                        since = f"{date}T00:00:00"
                        until = f"{date}T23:59:59"
                    else:  # weekly
                        d = datetime.strptime(date, "%Y-%m-%d")
                        start = d - timedelta(days=d.weekday())
                        since = start.strftime("%Y-%m-%dT00:00:00")
                        until = (start + timedelta(days=6)).strftime("%Y-%m-%dT23:59:59")

                    commits = server_ref.git_store.count_commits(repo_id, since, until)
                    authors = server_ref.git_store.get_author_stats(repo_id, since, until)
                    hot_files = server_ref.git_store.get_hot_files(repo_id, since, until=until, limit=10)
                    loc_added = sum(a.get("insertions", 0) for a in authors)
                    loc_removed = sum(a.get("deletions", 0) for a in authors)

                    self._serve_json({
                        "level": 1,
                        "period": period,
                        "date": date,
                        "stats": {
                            "commits": commits,
                            "authors": authors,
                            "hot_files": hot_files,
                            "loc_added": loc_added,
                            "loc_removed": loc_removed,
                        },
                        "text": "",
                        "narrative": "",
                    })

            def _serve_timeline_pending(self):
                """Tus Pendientes — items asignados al github_handle."""
                from urllib.parse import urlparse, parse_qs
                from hub.config import load_config

                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")

                config = load_config()
                handle = config.github_handle

                if not handle or not server_ref.git_store or not repo_id:
                    self._serve_json({"handle": "", "items": []})
                    return

                all_issues = server_ref.git_store.get_issues(repo_id, state="open")
                pending = []
                for issue in all_issues:
                    assignees = issue.get("assignees", [])
                    if handle in assignees:
                        pending.append({
                            "number": issue["number"],
                            "title": issue["title"],
                            "is_pr": bool(issue.get("is_pull_request")),
                            "pr_state": issue.get("pr_state"),
                            "updated_at": issue.get("updated_at"),
                        })

                self._serve_json({"handle": handle, "items": pending})

            def _serve_timeline_digest_history(self):
                """Handle /api/timeline/digest-history?repo_id=&period=daily&limit=30"""
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                period = params.get("period", ["daily"])[0]
                limit = self._parse_int(params, "limit", 30)

                if server_ref.git_store and repo_id:
                    self._serve_json(server_ref.git_store.list_cached_digests(repo_id, period, limit))
                else:
                    self._serve_json([])

            def _serve_timeline_commit_days(self):
                """Días con commits + nivel de digest cacheado (si existe)."""
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                repo_id = self._parse_int(params, "repo_id")
                limit = self._parse_int(params, "limit", 30)

                if not server_ref.git_store or not repo_id:
                    self._serve_json([])
                    return

                # Días con commits
                commit_days = server_ref.git_store.get_commit_days(repo_id, limit=limit)
                # Digests cacheados existentes
                cached = {d["date"]: d["level"]
                          for d in server_ref.git_store.list_cached_digests(repo_id, "daily", limit)}

                result = [
                    {"date": day, "has_commits": True, "level": cached.get(day, 0)}
                    for day in commit_days
                ]
                self._serve_json(result)

            def log_message(self, format, *args):
                # Suppress default access log noise
                pass

            def handle(self):
                """Suppress noisy ConnectionResetError from browser disconnects."""
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass

        return Handler
