"""SQLite store para datos de Git y GitHub."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hub.log import get as get_logger

_log = get_logger("GitStore")


def _to_local_naive(ts: str) -> str:
    """Convierte timestamp ISO 8601 con TZ a hora local naive.

    Necesario para que los digests agrupen por fecha de calendario local, no UTC.
    Ej: '2026-04-16T23:30:00+00:00' → '2026-04-16T20:30:00' (en UTC-3)
    """
    if not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        return ts
    except (ValueError, TypeError):
        return ts


# --- Migrations System ---
# Each migration is a tuple: (version, name, function)
# Function signature: fn(conn: sqlite3.Connection) -> int (rows modified)
_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], int]]] = []


def _mig_1_normalize_timestamps(conn: sqlite3.Connection) -> int:
    """Migration 1: normaliza timestamps con timezone local a UTC."""
    updated = 0
    # Query corregida: excluye explícitamente los ya-normalizados (UTC)
    cursor = conn.execute(
        """SELECT id, timestamp FROM git_commits
           WHERE timestamp NOT LIKE '%+00:00'
             AND timestamp NOT LIKE '%Z'
             AND timestamp IS NOT NULL
             AND timestamp != ''"""
    )
    for row_id, ts in cursor.fetchall():
        try:
            if re.match(r'.+[-+]\d{2}:\d{2}$', ts):
                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
                new_ts = dt.astimezone(timezone.utc).isoformat()
                if new_ts != ts:  # Solo actualizar si realmente cambió
                    conn.execute(
                        "UPDATE git_commits SET timestamp = ? WHERE id = ?",
                        (new_ts, row_id)
                    )
                    updated += 1
        except (ValueError, TypeError):
            continue
    conn.commit()
    return updated


def _mig_2_extract_branches(conn: sqlite3.Connection) -> int:
    """Migration 2: extrae branch de merge commits sin branch asignado.

    Solo procesa commits con is_merge=1 para evitar falsos positivos en
    commits regulares que contengan la palabra "from" o "into".
    """
    rows = conn.execute(
        "SELECT id, message, is_merge FROM git_commits WHERE (branch = '' OR branch IS NULL)"
    ).fetchall()

    updated = 0
    for row_id, message, is_merge in rows:
        if not is_merge:
            continue  # Solo extraer de merge commits

        branch = None

        # Patrón 1: "Merge pull request #N from owner/branch-name"
        m = re.search(r"Merge pull request #\d+ from\s+\S+/(\S+)", message)
        if m:
            branch = m.group(1)

        # Patrón 2: "Merge branch 'name'" o 'Merge branch "name"'
        if not branch:
            m = re.search(r"Merge branch ['\"]([^'\"]+)['\"]", message)
            if m:
                branch = m.group(1)

        # Patrón 3: "Merge ... into branch-name" (último recurso, solo merge commits)
        if not branch:
            m = re.search(r"into (\S+)$", message)
            if m:
                branch = m.group(1)

        if branch:
            conn.execute(
                "UPDATE git_commits SET branch = ? WHERE id = ?",
                (branch, row_id)
            )
            updated += 1

    conn.commit()
    return updated


def _mig_3_utc_to_local(conn: sqlite3.Connection) -> int:
    """Migration 3: re-convierte timestamps UTC a hora local naive.

    Migration 1 normalizó a UTC, pero los digests agrupan por fecha de calendario
    local. Un commit a las 23:30 hora local quedaba almacenado como el día siguiente.
    """
    rows = conn.execute(
        "SELECT id, timestamp FROM git_commits WHERE timestamp LIKE '%+00:00' OR timestamp LIKE '%Z'"
    ).fetchall()

    if not rows:
        return 0

    updated = 0
    for row_id, ts in rows:
        try:
            local_naive = _to_local_naive(ts)
            if local_naive != ts:
                conn.execute(
                    "UPDATE git_commits SET timestamp = ? WHERE id = ?",
                    (local_naive, row_id)
                )
                updated += 1
        except (ValueError, TypeError):
            continue
    conn.commit()
    return updated


# Register migrations
_MIGRATIONS = [
    (1, "normalize_timestamps", _mig_1_normalize_timestamps),
    (2, "extract_branches", _mig_2_extract_branches),
    (3, "utc_to_local", _mig_3_utc_to_local),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Aplica migrations pendientes. Cada migration corre exactamente una vez."""
    # Crear tabla de control si no existe
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Obtener versiones ya aplicadas
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}

    for version, name, fn in _MIGRATIONS:
        if version not in applied:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            _log.info("Migration %d aplicada: %s", version, name)


_SCHEMA = """
-- Repos registrados
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    remote_url TEXT NOT NULL,
    owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    added_at TEXT NOT NULL,
    last_fetch_at TEXT
);

-- Refs snapshot (para detectar nuevos commits)
CREATE TABLE IF NOT EXISTS git_refs (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    ref_name TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, ref_name)
);

-- Commits
CREATE TABLE IF NOT EXISTS git_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_email TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    message TEXT NOT NULL,
    is_merge INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    files_changed INTEGER DEFAULT 0,
    insertions INTEGER DEFAULT 0,
    deletions INTEGER DEFAULT 0,
    issue_refs TEXT,
    co_authors TEXT,
    ai_assisted INTEGER DEFAULT 0,
    session_id TEXT,
    UNIQUE(repo_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_repo_ts ON git_commits(repo_id, timestamp);

-- Archivos por commit
CREATE TABLE IF NOT EXISTS commit_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL REFERENCES git_commits(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    insertions INTEGER DEFAULT 0,
    deletions INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(file_path);

-- GitHub Issues + PRs
CREATE TABLE IF NOT EXISTS github_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    author TEXT,
    assignees TEXT,
    labels TEXT,
    milestone_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    body TEXT,
    is_pull_request INTEGER DEFAULT 0,
    pr_state TEXT,
    pr_base_branch TEXT,
    pr_head_branch TEXT,
    pr_merged_at TEXT,
    pr_review_decision TEXT,
    etag TEXT,
    UNIQUE(repo_id, number)
);

-- Milestones
CREATE TABLE IF NOT EXISTS github_milestones (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    due_on TEXT,
    open_issues INTEGER DEFAULT 0,
    closed_issues INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, number)
);

-- Project v2 items
CREATE TABLE IF NOT EXISTS github_project_items (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    project_title TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_number INTEGER,
    title TEXT,
    status TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, item_id)
);

-- Digests cacheados
CREATE TABLE IF NOT EXISTS daily_digests (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT 'daily',
    digest_level INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, date, period, digest_level)
);

-- Cache de ETags para API
CREATE TABLE IF NOT EXISTS api_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    updated_at TEXT NOT NULL
);
"""


class GitStore:
    """Thread-safe SQLite persistence for Git and GitHub data."""

    DEFAULT_DB_PATH = Path.home() / ".moolmesh" / "github.db"

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or self.DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")        # Enable cascade delete
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA mmap_size=268435456")   # 256MB — zero-copy reads
        self._conn.execute("PRAGMA temp_store=MEMORY")       # sorts in RAM
        self._conn.execute("PRAGMA cache_size=-65536")        # 64MB page cache
        self._conn.execute("PRAGMA busy_timeout=5000")        # 5s wait on lock contention
        self._lock = threading.Lock()

        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        # Run migrations versionadas (solo las pendientes)
        try:
            _apply_migrations(self._conn)
        except Exception:
            _log.error("Error aplicando migrations — DB sigue funcionando pero puede estar desactualizada", exc_info=True)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared connection. Callers MUST hold self._lock."""
        return self._conn

    # --- Repos ---

    def register_repo(self, config) -> int:
        """INSERT en repos. Retorna repo_id."""
        
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """INSERT OR REPLACE INTO repos
                   (path, remote_url, owner, repo_name, added_at, last_fetch_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (config.path, config.remote_url, config.owner, config.repo,
                 config.added_at, None)
            )
            conn.commit()
            return cursor.lastrowid

    def get_repo_id(self, path: str) -> int | None:
        """SELECT id FROM repos WHERE path = ?"""
        with self._lock:
            row = self._get_conn().execute(
                "SELECT id FROM repos WHERE path = ?",
                (path,)
            ).fetchone()
        return row[0] if row else None

    def list_repos(self) -> list[dict]:
        """SELECT * FROM repos. Retorna list de dicts."""
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT id, path, remote_url, owner, repo_name, added_at, last_fetch_at FROM repos"
            ).fetchall()
        
        return [
            {
                "id": r[0],
                "path": r[1],
                "remote_url": r[2],
                "owner": r[3],
                "repo_name": r[4],
                "added_at": r[5],
                "last_fetch_at": r[6],
            }
            for r in rows
        ]

    def remove_repo(self, path: str) -> bool:
        """DELETE FROM repos WHERE path = ?. Cascade a tablas dependientes."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute("DELETE FROM repos WHERE path = ?", (path,))
            conn.commit()
            return cursor.rowcount > 0

    def update_last_fetch(self, repo_id: int) -> None:
        """UPDATE repos SET last_fetch_at = ? WHERE id = ?"""
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._get_conn().execute(
                "UPDATE repos SET last_fetch_at = ? WHERE id = ?",
                (now, repo_id)
            )
            self._get_conn().commit()

    # --- Git Refs ---

    def get_refs(self, repo_id: int) -> dict[str, str]:
        """SELECT ref_name, commit_sha FROM git_refs WHERE repo_id = ?
        Retorna {ref_name: sha}."""
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT ref_name, commit_sha FROM git_refs WHERE repo_id = ?",
                (repo_id,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def update_refs(self, repo_id: int, refs: dict[str, str]) -> None:
        """INSERT OR REPLACE en git_refs para cada ref."""
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            for ref_name, commit_sha in refs.items():
                conn.execute(
                    """INSERT OR REPLACE INTO git_refs
                       (repo_id, ref_name, commit_sha, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (repo_id, ref_name, commit_sha, now)
                )
            conn.commit()

    # --- Commits ---

    def store_commits(self, repo_id: int, commits: list[dict]) -> int:
        """INSERT OR IGNORE (dedup por UNIQUE repo_id+sha).
        Incluye insert en commit_files para cada archivo.
        Retorna número de commits nuevos insertados."""
        
        if not commits:
            return 0
        
        inserted_count = 0
        
        with self._lock:
            conn = self._get_conn()
            
            for commit in commits:
                # Serialize list fields
                issue_refs = json.dumps(commit.get("issue_refs", [])) if commit.get("issue_refs") else None
                co_authors = json.dumps(commit.get("co_authors", [])) if commit.get("co_authors") else None
                
                # Calculate file stats from files list if provided
                files = commit.get("files", [])
                if files:
                    files_changed = len(files)
                    insertions = sum(f.get("insertions", 0) for f in files)
                    deletions = sum(f.get("deletions", 0) for f in files)
                else:
                    files_changed = commit.get("files_changed", 0)
                    insertions = commit.get("insertions", 0)
                    deletions = commit.get("deletions", 0)
                
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO git_commits
                       (repo_id, sha, author_name, author_email, timestamp, message,
                        is_merge, branch, files_changed, insertions, deletions,
                        issue_refs, co_authors, ai_assisted, session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        repo_id,
                        commit["sha"],
                        commit["author_name"],
                        commit["author_email"],
                        commit["timestamp"],
                        commit["message"],
                        1 if commit.get("is_merge") else 0,
                        commit.get("branch", ""),
                        files_changed,
                        insertions,
                        deletions,
                        issue_refs,
                        co_authors,
                        1 if commit.get("ai_assisted") else 0,
                        commit.get("session_id"),
                    )
                )
                
                if cursor.rowcount > 0:
                    inserted_count += 1
                    commit_id = cursor.lastrowid
                    
                    # Insert files
                    for file_info in commit.get("files", []):
                        conn.execute(
                            """INSERT INTO commit_files
                               (commit_id, file_path, insertions, deletions)
                               VALUES (?, ?, ?, ?)""",
                            (
                                commit_id,
                                file_info["file_path"],
                                file_info.get("insertions", 0),
                                file_info.get("deletions", 0),
                            )
                        )
            
            conn.commit()
        
        return inserted_count

    def get_commits(self, repo_id: int, since: str | None = None,
                    until: str | None = None, limit: int = 100) -> list[dict]:
        """SELECT con filtros opcionales. ORDER BY timestamp DESC."""
        where_parts = ["repo_id = ?"]
        params: list[Any] = [repo_id]
        
        if since:
            where_parts.append("timestamp >= ?")
            params.append(since)
        if until:
            where_parts.append("timestamp <= ?")
            params.append(until)
        
        where = " AND ".join(where_parts)
        params.append(limit)
        
        with self._lock:
            rows = self._get_conn().execute(
                f"""SELECT sha, author_name, author_email, timestamp, message,
                           is_merge, branch, files_changed, insertions, deletions,
                           issue_refs, co_authors, ai_assisted, session_id
                    FROM git_commits WHERE {where}
                    ORDER BY timestamp DESC LIMIT ?""",
                params
            ).fetchall()
        
        commits = []
        for r in rows:
            commit = {
                "sha": r[0],
                "author_name": r[1],
                "author_email": r[2],
                "timestamp": r[3],
                "message": r[4],
                "is_merge": bool(r[5]),
                "branch": r[6],
                "files_changed": r[7],
                "insertions": r[8],
                "deletions": r[9],
                "issue_refs": json.loads(r[10]) if r[10] else [],
                "co_authors": json.loads(r[11]) if r[11] else [],
                "ai_assisted": bool(r[12]),
                "session_id": r[13],
            }
            commits.append(commit)
        
        return commits

    def update_commit_correlations(self, repo_id: int, sha: str, ai_assisted: bool, session_id: str | None = None) -> bool:
        """UPDATE ai_assisted y session_id para un commit específico.
        Retorna True si se actualizó alguna fila.
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """UPDATE git_commits
                   SET ai_assisted = ?, session_id = ?
                   WHERE repo_id = ? AND sha = ?""",
                (1 if ai_assisted else 0, session_id, repo_id, sha)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_author_stats(self, repo_id: int, since: str | None = None,
                         until: str | None = None) -> list[dict]:
        """GROUP BY author_name con COUNT, SUM(insertions), SUM(deletions).
        ORDER BY commits DESC."""
        where = "repo_id = ?"
        params: list[Any] = [repo_id]

        if since:
            where += " AND timestamp >= ?"
            params.append(since)
        if until:
            where += " AND timestamp <= ?"
            params.append(until)

        with self._lock:
            rows = self._get_conn().execute(
                f"""SELECT author_name, COUNT(*) as commits,
                           SUM(insertions) as total_insertions,
                           SUM(deletions) as total_deletions
                    FROM git_commits WHERE {where}
                    GROUP BY author_name
                    ORDER BY commits DESC""",
                params
            ).fetchall()

        return [
            {
                "author_name": r[0],
                "commits": r[1],
                "insertions": r[2] or 0,
                "deletions": r[3] or 0,
            }
            for r in rows
        ]

    def get_hot_files(self, repo_id: int, since: str | None = None,
                      until: str | None = None, limit: int = 20) -> list[dict]:
        """JOIN git_commits + commit_files. GROUP BY file_path.
        COUNT(*) as changes, SUM(insertions), SUM(deletions).
        ORDER BY changes DESC."""
        where = "c.repo_id = ?"
        params: list[Any] = [repo_id]

        if since:
            where += " AND c.timestamp >= ?"
            params.append(since)
        if until:
            where += " AND c.timestamp <= ?"
            params.append(until)

        params.append(limit)

        with self._lock:
            rows = self._get_conn().execute(
                f"""SELECT f.file_path, COUNT(*) as changes,
                           SUM(f.insertions) as total_insertions,
                           SUM(f.deletions) as total_deletions
                    FROM commit_files f
                    JOIN git_commits c ON f.commit_id = c.id
                    WHERE {where}
                    GROUP BY f.file_path
                    ORDER BY changes DESC
                    LIMIT ?""",
                params
            ).fetchall()

        return [
            {
                "file_path": r[0],
                "changes": r[1],
                "insertions": r[2] or 0,
                "deletions": r[3] or 0,
            }
            for r in rows
        ]

    def count_commits(self, repo_id: int, since: str | None = None,
                      until: str | None = None) -> int:
        """SELECT COUNT(*) con filtros."""
        where_parts = ["repo_id = ?"]
        params: list[Any] = [repo_id]

        if since:
            where_parts.append("timestamp >= ?")
            params.append(since)
        if until:
            where_parts.append("timestamp <= ?")
            params.append(until)

        where = " AND ".join(where_parts)

        with self._lock:
            row = self._get_conn().execute(
                f"SELECT COUNT(*) FROM git_commits WHERE {where}",
                params
            ).fetchone()

        return row[0] if row else 0

    def get_branch_stats(self, repo_id: int, since: str | None = None,
                         until: str | None = None) -> list[dict]:
        """GROUP BY branch con COUNT de commits.
        Solo branches no vacías. ORDER BY commits DESC."""
        where = "repo_id = ? AND branch IS NOT NULL AND branch != ''"
        params: list[Any] = [repo_id]

        if since:
            where += " AND timestamp >= ?"
            params.append(since)
        if until:
            where += " AND timestamp <= ?"
            params.append(until)

        with self._lock:
            rows = self._get_conn().execute(
                f"""SELECT branch, COUNT(*) as commits
                    FROM git_commits WHERE {where}
                    GROUP BY branch
                    ORDER BY commits DESC
                    LIMIT 10""",
                params
            ).fetchall()

        return [
            {"name": r[0], "commits": r[1]}
            for r in rows
        ]

    # --- GitHub ---

    def upsert_issues(self, repo_id: int, issues: list[dict]) -> None:
        """INSERT OR REPLACE en github_issues."""
        with self._lock:
            conn = self._get_conn()
            for issue in issues:
                conn.execute(
                    """INSERT OR REPLACE INTO github_issues
                       (repo_id, number, title, state, author, assignees, labels,
                        milestone_number, created_at, updated_at, closed_at, body,
                        is_pull_request, pr_state, pr_base_branch, pr_head_branch,
                        pr_merged_at, pr_review_decision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (repo_id, issue["number"], issue["title"], issue["state"],
                     issue.get("author"), issue.get("assignees"), issue.get("labels"),
                     issue.get("milestone_number"),
                     _to_local_naive(issue["created_at"]),
                     _to_local_naive(issue["updated_at"]),
                     _to_local_naive(issue.get("closed_at") or "") or None,
                     issue.get("body"),
                     issue.get("is_pull_request", 0), issue.get("pr_state"),
                     issue.get("pr_base_branch"), issue.get("pr_head_branch"),
                     _to_local_naive(issue.get("pr_merged_at") or "") or None,
                     issue.get("pr_review_decision"))
                )
            conn.commit()

    def upsert_milestones(self, repo_id: int, milestones: list[dict]) -> None:
        """INSERT OR REPLACE en github_milestones."""
        with self._lock:
            conn = self._get_conn()
            for m in milestones:
                conn.execute(
                    """INSERT OR REPLACE INTO github_milestones
                       (repo_id, number, title, state, due_on, open_issues, closed_issues, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (repo_id, m["number"], m["title"], m["state"],
                     m.get("due_on"), m.get("open_issues", 0), m.get("closed_issues", 0),
                     _to_local_naive(m.get("updated_at") or ""))
                )
            conn.commit()

    def upsert_project_items(self, repo_id: int, items: list[dict]) -> None:
        """INSERT OR REPLACE en github_project_items."""
        with self._lock:
            conn = self._get_conn()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                conn.execute(
                    """INSERT OR REPLACE INTO github_project_items
                       (repo_id, item_id, project_title, content_type, content_number, title, status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (repo_id, item["item_id"], item.get("project_title", ""),
                     item.get("content_type", ""), item.get("content_number"),
                     item.get("title", ""), item.get("status"), now)
                )
            conn.commit()

    def get_issues(self, repo_id: int, state: str | None = None) -> list[dict]:
        """SELECT con filtro opcional de state. Parsear assignees/labels de JSON."""
        with self._lock:
            where = "repo_id = ?"
            params: list[Any] = [repo_id]
            if state:
                where += " AND state = ?"
                params.append(state)
            
            rows = self._get_conn().execute(
                f"""SELECT number, title, state, author, assignees, labels,
                           milestone_number, created_at, updated_at, closed_at, body,
                           is_pull_request, pr_state, pr_merged_at
                    FROM github_issues WHERE {where} ORDER BY updated_at DESC""",
                params
            ).fetchall()
        
        return [
            {
                "number": r[0], "title": r[1], "state": r[2], "author": r[3],
                "assignees": json.loads(r[4]) if r[4] else [], "labels": json.loads(r[5]) if r[5] else [], "milestone_number": r[6],
                "created_at": r[7], "updated_at": r[8], "closed_at": r[9],
                "body": r[10], "is_pull_request": r[11], "pr_state": r[12],
                "pr_merged_at": r[13],
            }
            for r in rows
        ]

    def get_pr_pipeline(self, repo_id: int) -> dict[str, list]:
        """Agrupa PRs por pr_state para la vista kanban.
        Returns: {"draft": [...], "review": [...], "approved": [...], "merged": [...], "closed": [...]}
        """
        with self._lock:
            rows = self._get_conn().execute(
                """SELECT number, title, author, pr_state, pr_merged_at, updated_at, assignees, labels
                   FROM github_issues
                   WHERE repo_id = ? AND is_pull_request = 1
                   ORDER BY updated_at DESC""",
                (repo_id,)
            ).fetchall()
        
        pipeline = {"draft": [], "review": [], "approved": [], "merged": [], "closed": []}
        for r in rows:
            pr = {
                "number": r[0], "title": r[1], "author": r[2],
                "pr_state": r[3] or "review", "merged_at": r[4],
                "updated_at": r[5], "assignees": json.loads(r[6]) if r[6] else [], "labels": json.loads(r[7]) if r[7] else [],
            }
            state = pr["pr_state"]
            if state in pipeline:
                pipeline[state].append(pr)
        return pipeline

    def get_milestones(self, repo_id: int) -> list[dict]:
        """SELECT * con estado, open/closed counts, due_on."""
        with self._lock:
            rows = self._get_conn().execute(
                """SELECT number, title, state, due_on, open_issues, closed_issues, updated_at
                   FROM github_milestones
                   WHERE repo_id = ?
                   ORDER BY due_on ASC NULLS LAST""",
                (repo_id,)
            ).fetchall()
        
        return [
            {
                "number": r[0], "title": r[1], "state": r[2], "due_on": r[3],
                "open_issues": r[4], "closed_issues": r[5], "updated_at": r[6],
            }
            for r in rows
        ]

    def get_project_board(self, repo_id: int) -> dict[str, list]:
        """Agrupa project items por status (columna del board).
        Returns: {"Todo": [...], "In Progress": [...], "Done": [...], ...}
        """
        with self._lock:
            rows = self._get_conn().execute(
                """SELECT item_id, project_title, content_type, content_number, title, status
                   FROM github_project_items
                   WHERE repo_id = ?
                   ORDER BY updated_at DESC""",
                (repo_id,)
            ).fetchall()
        
        board: dict[str, list] = {}
        for r in rows:
            item = {
                "item_id": r[0], "project_title": r[1], "content_type": r[2],
                "content_number": r[3], "title": r[4], "status": r[5],
            }
            status = r[5] or "Todo"
            if status not in board:
                board[status] = []
            board[status].append(item)
        return board

    # --- Digests ---

    def get_digest(self, repo_id: int, date: str, period: str, level: int) -> dict | None:
        """Lee digest cacheado de SQLite."""
        with self._lock:
            row = self._get_conn().execute(
                """SELECT content_json FROM daily_digests
                   WHERE repo_id = ? AND date = ? AND period = ? AND digest_level = ?""",
                (repo_id, date, period, level)
            ).fetchone()

        if row and row[0]:
            import json
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        return None

    def store_digest(self, repo_id: int, date: str, period: str,
                     level: int, content: dict) -> None:
        """Guarda digest en cache."""
        import json
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        content_json = json.dumps(content, ensure_ascii=False)

        with self._lock:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO daily_digests
                   (repo_id, date, period, digest_level, content_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (repo_id, date, period, level, content_json, now)
            )
            self._get_conn().commit()

    def delete_cached(self, repo_id: int, date: str, period: str) -> None:
        """Borra todas las filas de cache para (repo_id, date, period) — todos los niveles."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM daily_digests WHERE repo_id=? AND date=? AND period=?",
                (repo_id, date, period),
            )
            self._conn.commit()

    def list_cached_digests(self, repo_id: int, period: str = "daily",
                            limit: int = 30) -> list[dict]:
        """Lista fechas con digests cacheados, retorna nivel máximo por fecha."""
        with self._lock:
            rows = self._get_conn().execute(
                """SELECT date, MAX(digest_level) AS digest_level, MAX(created_at) AS created_at
                   FROM daily_digests
                   WHERE repo_id = ? AND period = ?
                   GROUP BY date
                   ORDER BY date DESC LIMIT ?""",
                (repo_id, period, limit)
            ).fetchall()

        return [{"date": r[0], "level": r[1], "created_at": r[2]} for r in rows]

    def get_commit_days(self, repo_id: int, limit: int = 30) -> list[str]:
        """Lista de fechas distintas con commits. ORDER BY date DESC."""
        with self._lock:
            rows = self._get_conn().execute(
                """SELECT DISTINCT DATE(timestamp) as day
                   FROM git_commits
                   WHERE repo_id = ?
                   ORDER BY day DESC LIMIT ?""",
                (repo_id, limit)
            ).fetchall()
        return [r[0] for r in rows]
