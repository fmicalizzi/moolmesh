"""Parser for Antigravity CLI (``agy``) conversations.

The CLI keeps its data under ``~/.gemini/antigravity-cli`` (``$ANTIGRAVITY_APP_DATA_DIR``
when set). Two files are read, both strictly read-only:

* ``brain/<conversation-uuid>/.system_generated/logs/transcript.jsonl`` — one
  JSON step per line (``step_index``, ``type``, ``source``, ``status``,
  ``created_at``, ``content``, ``thinking``, ``tool_calls``, ...). The file is
  append-only: a step is never rewritten, a ``RUNNING`` step stays ``RUNNING``,
  and a resumed conversation keeps appending to the same file.
* ``conversation_summaries.db`` — a SQLite index (``PRAGMA user_version = 3``)
  shared by every Antigravity surface. Only session metadata is read from it
  (title, workspace, parent conversation), filtered to the CLI's own rows.

The per-conversation ``conversations/<uuid>.db`` files (SQLite WAL + protobuf)
are never opened.

Format hazards handled here (observed on real transcripts):

* ``step_index`` is not unique or ordered: some transcripts repeat their steps
  (a copy restarting at 0), some write steps out of order, indices have gaps.
  Every step is ingested at most once per conversation (first occurrence wins).
* Every ``args`` value of a tool call is a JSON-encoded string; it is decoded
  once here so the adapter sees plain values.
* ``type`` / ``status`` values outside the known set are carried verbatim and
  never break the parse; lines without a numeric ``step_index``, a ``type`` or a
  valid ``created_at`` are skipped (logged once per file and reason).
"""

from __future__ import annotations

import collections
import json
import ntpath
import os
import posixpath
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from hub.log import get as get_logger
from hub.models.antigravity import AntigravityEntry, AntigravitySummary, AntigravityToolCall
from hub.parsers.base import BaseParser
from hub.sqlite_ro import ro_uri

_log = get_logger("AntigravityParser")

TRANSCRIPT_NAME = "transcript.jsonl"
SUMMARIES_DB = "conversation_summaries.db"
# ``PRAGMA user_version`` of the ``conversation_summaries.db`` this parser knows.
SUMMARIES_USER_VERSION = 3

# Title / initial prompt bounds (session metadata is a summary, not a copy) —
# the same bounds the Pi parser uses.
_TITLE_MAX = 120
_PROMPT_MAX = 2000

# Upper bound of the transcript scan that looks for a ``run_command`` Cwd when
# the summaries index has no workspace for a conversation (discovery fallback).
CWD_SCAN_MAX_BYTES = 512 * 1024

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_DRIVE_URI_PATH_RE = re.compile(r"^/[A-Za-z]:")


def default_antigravity_base() -> Path:
    """The CLI's app data dir: ``$ANTIGRAVITY_APP_DATA_DIR`` or ``~/.gemini/antigravity-cli``.

    ``ANTIGRAVITY_APP_DATA_DIR`` is not documented by the vendor (it was seen
    in the binary); the default is the documented ``<app_data_dir>`` of the CLI
    surface. Windows has no documented data dir: ``%USERPROFILE%`` plus the
    same relative path is used. Resolved on every call so a test (or a user)
    can change the environment.
    """
    env = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".gemini" / "antigravity-cli"


def conversation_id_from_path(path: Path) -> str:
    """The conversation uuid of ``brain/<uuid>/.system_generated/logs/transcript.jsonl``.

    '' when the path does not follow that layout: every transcript has the same
    file name, so the id can only come from the directory two levels up.
    """
    parts = path.parts
    if (
        len(parts) >= 4
        and parts[-1] == TRANSCRIPT_NAME
        and parts[-2] == "logs"
        and parts[-3] == ".system_generated"
    ):
        return parts[-4]
    return ""


def _is_abs(p: str) -> bool:
    return p.startswith("/") or bool(_WIN_ABS_RE.match(p))


def decode_arg(value: Any) -> Any:
    """Decode one ``args`` value: the transcript stores them as JSON-encoded strings.

    A string that is not valid JSON (long content cut by the writer, free
    text) is returned as is — never a guess at a half-decoded value.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _valid_step_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_timestamp(ts: Any) -> datetime | None:
    """``created_at`` as an aware datetime (a naive one is taken as UTC); None if unusable."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_command_cwd(tool_calls: list[AntigravityToolCall]) -> str:
    """The Cwd of the first ``run_command`` call that carries an absolute one."""
    for call in tool_calls:
        if call.name != "run_command":
            continue
        cwd = call.args.get("Cwd")
        if isinstance(cwd, str) and _is_abs(cwd.strip()):
            return cwd.strip()
    return ""


def workspace_uri_to_path(uri: Any) -> str:
    """A ``file://`` workspace URI as a filesystem path ('' for anything else).

    Percent-decoded; ``file:///C:/x`` becomes ``C:\\x`` (the leading slash
    before a drive letter is dropped); ``file://host/share/x`` becomes a UNC
    path; a trailing separator is stripped.
    """
    if not isinstance(uri, str):
        return ""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return ""
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return ntpath.normpath(f"//{parsed.netloc}{path}")
    if _DRIVE_URI_PATH_RE.match(path):
        return ntpath.normpath(path[1:])
    if not path.startswith("/"):
        return ""
    return posixpath.normpath(path)


def _same_app_dir(value: Any, base: Path) -> bool:
    """Is a summaries ``app_data_dir`` this CLI's own data dir?

    The index is shared by every Antigravity surface and stores the app dir as
    a *name* relative to ``~/.gemini`` (``antigravity-cli`` for the CLI,
    ``antigravity`` for the desktop app). Compared by last path component, so
    an absolute value also matches; conversation ids are UUIDs, so two data
    dirs with the same name cannot mix up sessions.
    """
    if not isinstance(value, str):
        return False
    name = re.split(r"[\\/]", value.strip().rstrip("/\\"))[-1]
    return bool(name) and name == base.name


def _summaries_uri(db: Path) -> str:
    """Read-only URI of the summaries index (#54).

    With the CLI running its ``-wal`` sits next to the file: open ``mode=ro``
    without ``immutable`` so the reader sees the WAL frames. With the CLI shut
    down there is no ``-wal``, and a plain read-only open of a WAL database
    leaves empty ``-wal``/``-shm`` files behind: ``immutable=1`` avoids that
    (safe, nobody is writing). Built here so ``hub.sqlite_ro.connect_ro`` stays
    unchanged for the providers that do not need it.
    """
    uri = ro_uri(db)
    if not Path(str(db) + "-wal").exists():
        uri += "&immutable=1"
    return uri


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _i(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


class AntigravityParser(BaseParser):

    def __init__(self) -> None:
        # Per-file context keyed by path: the dedupe set of ``step_index``, the
        # first user prompt, the first run_command Cwd and ``offset`` — the
        # file position this context describes. A chunk read from offset > 0
        # carries no header of its own, so the context is the state.
        # ``watchers/base.py`` clears the entry after a history walk.
        self._session_ctx: dict[str, dict[str, Any]] = {}
        # Skipped lines by reason (tests / validation) and the (path, reason)
        # pairs already logged: an incremental parse polls every few seconds,
        # a malformed file must not flood the log.
        self.skipped: collections.Counter[str] = collections.Counter()
        self._warned: set[tuple[str, str]] = set()
        self._summaries_failing = False
        self._summaries_version_warned: set[tuple[str, Any]] = set()

    # ── public API ───────────────────────────────────────────────

    def parse_file(self, path: Path) -> list[AntigravityEntry]:
        session_id = conversation_id_from_path(path)
        if not session_id:
            self._skip_file(path, "layout")
            return []
        ctx = self._new_ctx(session_id)
        entries, _ = self._consume(path, 0, ctx)
        self._stamp(entries, ctx)
        return entries

    def parse_incremental(self, path: Path, offset: int) -> tuple[list[AntigravityEntry], int]:
        session_id = conversation_id_from_path(path)
        if not session_id:
            self._skip_file(path, "layout")
            return [], offset
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
        if offset > size:
            # Truncated, rewound or replaced: start over. The context below is
            # rebuilt, so what is re-read is deduped only against itself; the
            # event store's fingerprint drops what it already holds.
            offset = 0
        ctx = self._session_ctx.get(str(path))
        if ctx is None or ctx["offset"] != offset:
            # First read of this file in this process (daemon restart, history
            # walk, catch-up), or the caller re-reads from a different offset
            # (a failed store write retries the same bytes). The set of seen
            # steps must describe exactly what the offset says was ingested:
            # rebuild it from the bytes before the offset. The transcript is
            # append-only, so that set is a pure function of the prefix, and
            # nothing has to be persisted next to the offset (no schema change).
            ctx = self._new_ctx(session_id)
            if offset > 0:
                self._consume(path, 0, ctx, limit=offset, collect=False)
            ctx["offset"] = offset
            self._session_ctx[str(path)] = ctx
        entries, new_offset = self._consume(path, offset, ctx)
        ctx["offset"] = new_offset
        self._stamp(entries, ctx)
        return entries, new_offset

    @staticmethod
    def can_parse(path: Path) -> bool:
        if not conversation_id_from_path(path):
            return False
        try:
            with open(path, "rb") as f:
                first = f.readline(1 << 20)
            data = json.loads(first)
        except (OSError, ValueError):
            return False
        return (
            isinstance(data, dict)
            and _valid_step_index(data.get("step_index"))
            and isinstance(data.get("type"), str)
        )

    # ── session metadata (conversation_summaries.db) ─────────────

    def read_summaries(self, base: Path) -> dict[str, AntigravitySummary] | None:
        """The CLI's rows of ``conversation_summaries.db`` keyed by conversation id.

        None when the index is absent, unreadable (locked, corrupt, another
        schema) or of an unknown ``user_version``: the caller keeps its last
        good answer and the watcher goes on with the transcripts alone — a
        missing index never stops ingestion (#52). Logged once when it starts
        failing and once when it reads again.
        """
        db = base / SUMMARIES_DB
        if not db.is_file():
            return None
        try:
            conn = sqlite3.connect(_summaries_uri(db), uri=True, timeout=5.0)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != SUMMARIES_USER_VERSION:
                    key = (str(db), version)
                    if key not in self._summaries_version_warned:
                        self._summaries_version_warned.add(key)
                        _log.warning(
                            "%s tiene user_version=%s (se conoce %s); se ignora el índice "
                            "y se usan solo los transcripts",
                            db, version, SUMMARIES_USER_VERSION,
                        )
                    return None
                rows = conn.execute(
                    "SELECT conversation_id, title, preview, workspace_uris, "
                    "parent_conversation_id, nesting_depth, last_modified_time, "
                    "app_data_dir FROM conversation_summaries"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            if not self._summaries_failing:
                self._summaries_failing = True
                _log.warning(
                    "No se pudo leer %s en solo lectura (bloqueada, ilegible o con otro "
                    "esquema); se usan solo los transcripts hasta que vuelva a abrir",
                    db, exc_info=True,
                )
            return None
        if self._summaries_failing:
            self._summaries_failing = False
            _log.info("%s vuelve a leerse en solo lectura", db)

        out: dict[str, AntigravitySummary] = {}
        for cid, title, preview, ws, parent, depth, modified, app_dir in rows:
            if not isinstance(cid, str) or not cid:
                continue
            if not _same_app_dir(app_dir, base):
                continue  # another Antigravity surface (desktop app, IDE)
            out[cid] = AntigravitySummary(
                conversation_id=cid,
                title=_s(title).strip(),
                preview=_s(preview).strip(),
                cwd=self._first_workspace(ws),
                parent_conversation_id=_s(parent).strip(),
                nesting_depth=_i(depth),
                last_modified=_s(modified),
            )
        return out

    @staticmethod
    def _first_workspace(raw: Any) -> str:
        """The first ``workspace_uris`` entry (a JSON array of ``file://`` URIs), decoded."""
        if not isinstance(raw, str) or not raw.strip():
            return ""
        try:
            uris = json.loads(raw)
        except ValueError:
            return ""
        if not isinstance(uris, list):
            return ""
        for uri in uris:
            path = workspace_uri_to_path(uri)
            if path:
                return path
        return ""

    def scan_cwd(self, path: Path, max_bytes: int = CWD_SCAN_MAX_BYTES) -> str:
        """First ``run_command`` Cwd of a transcript ('' when none within ``max_bytes``).

        The fallback when the summaries index has no workspace for a
        conversation. Stops at the first Cwd found, in file order.
        """
        read = 0
        try:
            with open(path, "rb") as f:
                for raw in f:
                    read += len(raw)
                    if read > max_bytes:
                        break
                    if not raw.endswith(b"\n"):
                        break
                    entry = self._parse_line(raw, path, count=False)
                    if entry is not None:
                        cwd = _run_command_cwd(entry.tool_calls)
                        if cwd:
                            return cwd
        except OSError:
            return ""
        return ""

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _new_ctx(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "offset": 0,
            "seen": set(),
            "user_idx": None,
            "initial_prompt": "",
            "session_title": "",
            "cwd_idx": None,
            "cwd": "",
            "first_at": None,  # (datetime, created_at) of the earliest step seen
            "last_at": None,  # ... of the latest one
        }

    def _consume(
        self,
        path: Path,
        offset: int,
        ctx: dict[str, Any],
        limit: int | None = None,
        collect: bool = True,
    ) -> tuple[list[AntigravityEntry], int]:
        """Stream complete lines from ``offset`` (up to ``limit``), folding them into ``ctx``.

        Line by line, never the whole file in memory (#52). A trailing line
        without its newline is a write in progress: it is left for the next
        cycle, so a half-written step is never parsed. A step whose
        ``step_index`` was already seen is skipped (first occurrence wins).
        """
        entries: list[AntigravityEntry] = []
        consumed = offset
        seen: set[int] = ctx["seen"]
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                if limit is not None and consumed >= limit:
                    break
                consumed += len(raw)
                entry = self._parse_line(raw, path, count=collect)
                if entry is None or entry.step_index in seen:
                    continue
                seen.add(entry.step_index)
                self._fold(entry, ctx)
                if collect:
                    entries.append(entry)
        return entries, consumed

    def _fold(self, entry: AntigravityEntry, ctx: dict[str, Any]) -> None:
        """Update the conversation context from one new step.

        Lines can arrive out of order, so "first" means the lowest
        ``step_index`` seen so far, not the first line, and the session's time
        range is the min / max ``created_at`` over every step seen.
        """
        idx = entry.step_index
        when = _parse_timestamp(entry.timestamp)
        if when is not None:
            if ctx["first_at"] is None or when < ctx["first_at"][0]:
                ctx["first_at"] = (when, entry.timestamp)
            if ctx["last_at"] is None or when > ctx["last_at"][0]:
                ctx["last_at"] = (when, entry.timestamp)
        if entry.step_type == "USER_INPUT" and entry.content.strip():
            if ctx["user_idx"] is None or idx < ctx["user_idx"]:
                ctx["user_idx"] = idx
                ctx["initial_prompt"] = entry.content[:_PROMPT_MAX]
                first_line = entry.content.strip().splitlines()[0].strip()
                ctx["session_title"] = first_line[:_TITLE_MAX]
        if entry.tool_calls and (ctx["cwd_idx"] is None or idx < ctx["cwd_idx"]):
            cwd = _run_command_cwd(entry.tool_calls)
            if cwd:
                ctx["cwd_idx"] = idx
                ctx["cwd"] = cwd

    @staticmethod
    def _stamp(entries: list[AntigravityEntry], ctx: dict[str, Any]) -> None:
        """Stamp the final context of this chunk on every entry of it."""
        for entry in entries:
            entry.session_id = ctx["session_id"]
            entry.cwd = ctx["cwd"]
            entry.session_title = ctx["session_title"]
            entry.initial_prompt = ctx["initial_prompt"]
            if ctx["first_at"] is not None:
                entry.first_event_at = ctx["first_at"][1]
                entry.last_event_at = ctx["last_at"][1]

    def _skip_file(self, path: Path, reason: str) -> None:
        self.skipped[reason] += 1
        if (str(path), reason) not in self._warned:
            self._warned.add((str(path), reason))
            _log.warning(
                "%s no sigue el formato conocido de Antigravity (%s); se saltea", path, reason,
            )

    def _skip_line(self, path: Path, reason: str, count: bool) -> None:
        if not count:
            return
        self.skipped[reason] += 1
        if (str(path), reason) not in self._warned:
            self._warned.add((str(path), reason))
            _log.warning(
                "%s: línea de transcript saltada (%s); se avisa una sola vez por archivo",
                path, reason,
            )

    def _parse_line(self, raw: bytes, path: Path, count: bool = True) -> AntigravityEntry | None:
        """One transcript line as an entry, or None when it is not a usable step."""
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except ValueError:
            self._skip_line(path, "json inválido", count)
            return None
        if not isinstance(data, dict):
            self._skip_line(path, "no es un objeto", count)
            return None
        step_index = data.get("step_index")
        if not _valid_step_index(step_index):
            self._skip_line(path, "step_index no numérico", count)
            return None
        step_type = data.get("type")
        if not isinstance(step_type, str) or not step_type:
            self._skip_line(path, "sin type", count)
            return None
        created_at = data.get("created_at")
        if _parse_timestamp(created_at) is None:
            self._skip_line(path, "sin created_at válido", count)
            return None

        calls = data.get("tool_calls")
        tool_calls: list[AntigravityToolCall] = []
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                args = call.get("args")
                tool_calls.append(
                    AntigravityToolCall(
                        name=_text(call.get("name")),
                        args=(
                            {k: decode_arg(v) for k, v in args.items()}
                            if isinstance(args, dict) else {}
                        ),
                    )
                )
        truncated = data.get("truncated_fields")
        return AntigravityEntry(
            step_index=step_index,
            step_type=step_type,
            source=_text(data.get("source")),
            status=_text(data.get("status")),
            timestamp=created_at,
            content=_text(data.get("content")),
            thinking=_text(data.get("thinking")),
            error=_text(data.get("error")),
            tool_calls=tool_calls,
            truncated_fields=[t for t in truncated if isinstance(t, str)]
            if isinstance(truncated, list) else [],
            raw=data,
        )
