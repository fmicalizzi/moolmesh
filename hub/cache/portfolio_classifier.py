"""Portfolio classification — collapse harness noise, group by real project.

MoolMesh #24, Stage 1. A pure **read-layer** projection that answers "which of
these 376 workspace folders are real projects, and how do the rest hang off
them?" — WITHOUT touching the resolver (#20), the watcher (#21), ``events.db``
(read-only), or the attribution edges. Classification is derived state; it lives
in its own additive table (see ``workspace_store._SCHEMA``) and is rebuildable
from scratch at any time (unlike ``workspace_rollup``, which is a durable store
that must never be blindly rebuilt).

The taxonomy (epic #24), one rule each:

  * **A. Harness that encodes a project** — Claude scratchpad
    (``/private/tmp/claude-*/<encoded>/<uuid>/…``) and session storage
    (``~/.claude/projects/<encoded>/…``). Its name/uuid encodes the *real* cwd,
    so it **collapses** onto that project (its activity folds in at read time).
  * **B. Deep subdir of a real project** — nests under the nearest real
    ancestor (the project anchor).
  * **C. Materials / reports / exports** (non-dot folders like ``yaahub-ops``,
    ``reportes/inprocess``) — kept and shown, nested under the project.
  * **D. Config dotfolders + degenerate roots** — D1: a ``.``-prefixed dir
    *inside* a project nests under it but de-prioritized as config; D2:
    home-level dotfolders (``~/.config/*``, ``~/.claude/*`` non-project) and
    degenerate/system roots (``/``, ``/tmp``, ``~``, containers) go to the
    collapsed "sin clasificar / herramientas" section.

Collapse (A) resolves the real project two ways, in order:

  1. **Session cwd (primary)** — a scratchpad path embeds the session uuid; the
     session's real ``cwd`` is already in ``events.db`` (read-only). No decode.
  2. **Encoded-name decode (fallback)** — for session storage (no uuid) or a
     scratchpad whose session left no cwd. The naive ``replace('-','/')`` decode
     is LOSSY (Claude encodes every non-alphanumeric char — ``/`` AND ``_`` AND
     ``-`` — to ``-``, so ``coep-services`` and ``_eventsmx`` are ambiguous). We
     resolve it exactly by matching the encoded segment against the *forward*
     encoding of directories we already know are real (``encode_match``), and
     only as a last resort walk the filesystem to disambiguate (``fs_decode``).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from hub.discovery import ProjectDiscovery
from hub.correlation.workspace_resolver import resolve_dir

# Container segments that are NOT projects — a path made only of these is a
# degenerate root. Mirrors the container knowledge in
# ``discovery.extract_project_name`` (single source of that judgement); kept in
# sync deliberately rather than re-derived per call.
_HOME_ANCHORS = {"Users", "Volumes", "home", "root"}
_SYS_ROOTS = {"private", "tmp", "var", "usr", "opt", "etc", "bin", "sbin",
              "System", "Library", "Applications", "dev", "cores"}
_STRIP = {"Downloads", "Documents", "Projects", "repos", "src", "code", "Desktop",
          "workspace", "Programming", "GitHub", "GitHub Projects", "Claude", "Temporal"}

# Non-dot subdir names that read as deliverable *materials* (category C) rather
# than a plain source subdir (category B). Both nest and render the same; the
# distinction is only a label for the human.
_MATERIALS = {"reportes", "reports", "snapshots", "exports", "export",
              "materials", "material", "dist", "build", "out", "assets",
              "renders", "deliverables"}

# A scratchpad path: /private/tmp/claude-<uid>/<encoded>/<session-uuid>/…
_SCRATCH = re.compile(
    r"^/private/tmp/claude-[^/]+/(?P<enc>-[^/]+)/"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)"
)


def _split(p: str) -> list[str]:
    return [x for x in p.replace("\\", "/").split("/") if x]


def path_encode(abspath: str) -> str:
    """Claude's directory-name encoding: every non-alphanumeric char → ``-``.

    Forward-only and total (unlike decoding, which is ambiguous). Verified
    against ground-truth pairs on a real machine:

        /Users/u/Downloads/Claude/_eventsmx/fiestados
            → -Users-u-Downloads-Claude--eventsmx-fiestados
        /Users/u/Downloads/Claude/PRODUCCIONES/LACNIC
            → -Users-u-Downloads-Claude-PRODUCCIONES-LACNIC
    """
    return re.sub(r"[^A-Za-z0-9]", "-", abspath)


def container_index(parts: list[str]) -> int:
    """Index of the first *meaningful* path component below the container prefix.

    Skips the home/volume anchor + its owner segment (``/Users/<user>``,
    ``/Volumes/<vol>``) and any number of leading container dirs (``Downloads``,
    ``Claude``, ``Temporal``, …). Returns ``len(parts)`` when the whole path is
    container — i.e. a degenerate root with no project component.
    """
    i, n = 0, len(parts)
    while i < n and parts[i] in _HOME_ANCHORS:
        i += 1
        if i < n:  # consume the owner/volume name after the anchor
            i += 1
    while i < n and (parts[i] in _STRIP or parts[i] in _SYS_ROOTS):
        i += 1
    return i


def anchor_path(abspath: str) -> str | None:
    """The project anchor for a real directory: the first meaningful component
    below the container prefix. ``None`` for a degenerate/system/container root.
    """
    parts = _split(abspath)
    i = container_index(parts)
    if i >= len(parts):
        return None
    return "/" + "/".join(parts[: i + 1])


def _path_hash_key(directory: str) -> str:
    """The ``path_hash`` workspace_key the resolver would mint for ``directory``.

    Matches ``workspace_resolver.resolve_dir`` exactly so a synthesized anchor
    key unifies with an already-persisted workspace row for the same dir.
    """
    digest = hashlib.sha256(directory.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"path_hash:{digest}"


@dataclass(frozen=True)
class Anchor:
    key: str
    label: str
    path: str


def anchor_of_realpath(realpath: str) -> Anchor | None:
    """Resolve a real directory to its project anchor (key + display label).

    Git-backed dirs anchor at the git root (finer, correct granularity); non-git
    trees anchor at the first meaningful component. ``None`` for degenerate roots.
    """
    ident = resolve_dir(realpath)
    if ident.kind == "git_remote":
        label = ident.remote_url or os.path.basename(ident.root_path or realpath)
        return Anchor(ident.key, label, ident.root_path or realpath)
    if ident.kind == "git_root":
        label = ProjectDiscovery.extract_project_name(ident.root_path or realpath)
        return Anchor(ident.key, label, ident.root_path or realpath)
    a = anchor_path(realpath)
    if a is None:
        return None
    return Anchor(_path_hash_key(a), ProjectDiscovery.extract_project_name(a), a)


def fs_decode(encoded: str) -> str | None:
    """Disambiguate an encoded segment against the real filesystem (last resort).

    Descends from ``/`` matching, at each level, the child directory whose
    forward encoding is a prefix of the remaining encoded string — choosing the
    LONGEST such child so a hyphenated name (``coep-services``) is never split
    into ``coep/services`` when the real dir exists. Returns the reconstructed
    absolute path, or ``None`` if the walk cannot proceed (dir gone, ambiguous).
    Touches disk read-only; never mutates.
    """
    if not encoded or encoded[0] != "-":
        return None
    cur = "/"
    rest = encoded  # always starts with '-' at a boundary
    while rest and rest != "-":
        try:
            entries = [
                e for e in os.listdir(cur)
                if os.path.isdir(os.path.join(cur, e))
            ]
        except OSError:
            return None
        best: str | None = None
        best_enc = ""
        for name in entries:
            token = "-" + path_encode(name).lstrip("-")
            # token must align on a segment boundary in `rest`.
            if rest == token or rest.startswith(token + "-"):
                if len(token) > len(best_enc):
                    best, best_enc = name, token
        if best is None:
            return None
        cur = os.path.join(cur, best)
        rest = rest[len(best_enc):]
    return cur


def index_real_dir(realdir: str, enc_index: dict[str, Anchor]) -> None:
    """Register a real directory (and every ancestor down to its anchor) in the
    encode-match index, mapping ``path_encode(dir)`` → its :class:`Anchor`.

    A harness folder's encoded name is the encoding of the session's *initial*
    cwd, which may be the project root or any dir between it and the deepest cwd
    we observed. Indexing the whole chain from the anchor down lets an exact
    forward-encoding match resolve it without a lossy decode.
    """
    if not realdir or realdir == "/":
        return
    anchor = anchor_of_realpath(realdir)
    if anchor is None:
        return
    ap = len(_split(anchor.path))
    parts = _split(realdir)
    for j in range(ap, len(parts) + 1):
        sub = "/" + "/".join(parts[:j])
        enc_index.setdefault(path_encode(sub), anchor)


@dataclass(frozen=True)
class Classification:
    """The classification of one workspace (a row of ``workspace_classification``)."""

    category: str      # root | A | B | C | D
    subtype: str       # project | harness | subdir | materials | config | home_config | degenerate
    role: str          # project | collapse | nest | orphan
    project_key: str | None
    project_label: str | None
    resolved_via: str  # self | session_cwd | encode_match | fs_decode | subdir | materials | dotchild | home_dot | degenerate | tool | unresolved


def _orphan(subtype: str, via: str) -> Classification:
    return Classification("D", subtype, "orphan", None, None, via)


def classify(
    kind: str,
    remote_url: str | None,
    root_path: str | None,
    dir_path: str | None,
    *,
    session_cwds: dict[str, str],
    enc_index: dict[str, Anchor],
    home: str,
) -> Classification:
    """Classify one workspace into the #24 taxonomy.

    ``session_cwds`` maps session-uuid → real cwd (from events.db, read-only).
    ``enc_index`` maps ``path_encode(real_dir)`` → its resolved :class:`Anchor`,
    for the decode fallback. ``home`` is the user's home directory.
    """
    # 1. Git workspaces are always real project roots.
    if kind in ("git_remote", "git_root"):
        label = remote_url or ProjectDiscovery.extract_project_name(root_path or "")
        return Classification("root", "project", "project", _git_key(kind, remote_url, root_path),
                              label, "self")

    d = (dir_path or "").rstrip("/")
    if not d:
        return _orphan("degenerate", "degenerate")

    # 2. Harness A — scratchpad (uuid → real session cwd, primary).
    m = _SCRATCH.match(d + "/")
    if m:
        return _collapse_from_encoded(
            m.group("enc"), m.group("uuid"), session_cwds, enc_index
        )

    # 3. Any other /private/tmp/claude-* path is harness *tooling* (bundled
    #    skills, temp scratch without a session) → D2, never a project.
    if d.startswith("/private/tmp/claude-"):
        return _orphan("home_config", "tool")

    # 4. Harness A — session storage ~/.claude/projects/<encoded>/… (no uuid).
    proj_root = f"{home}/.claude/projects/"
    if d.startswith(proj_root):
        seg = d[len(proj_root):].split("/", 1)[0]
        if seg.startswith("-"):
            return _collapse_from_encoded(seg, None, session_cwds, enc_index)

    # 5. D2 — home-level dotfolders and tooling (~/.config, ~/.claude/plugins…).
    if d.startswith(home + "/."):
        return _orphan("home_config", "home_dot")

    # 6. Real dir under a container → project root, or a nested child.
    anchor = anchor_of_realpath(d)
    if anchor is None:
        return _orphan("degenerate", "degenerate")

    if os.path.normpath(anchor.path) == os.path.normpath(d):
        return Classification("root", "project", "project", anchor.key,
                              anchor.label, "self")

    # Nested under the anchor. What kind of child?
    rel = _split(d)[len(_split(anchor.path)):]
    if any(seg.startswith(".") for seg in rel):
        return Classification("D", "config", "nest", anchor.key, anchor.label, "dotchild")
    if any(seg.lower() in _MATERIALS or seg.lower().endswith("-ops") for seg in rel):
        return Classification("C", "materials", "nest", anchor.key, anchor.label, "materials")
    return Classification("B", "subdir", "nest", anchor.key, anchor.label, "subdir")


def _git_key(kind: str, remote_url: str | None, root_path: str | None) -> str:
    return (f"git_remote:{remote_url}" if kind == "git_remote"
            else f"git_root:{root_path}")


def _collapse_from_encoded(
    enc: str,
    uuid: str | None,
    session_cwds: dict[str, str],
    enc_index: dict[str, Anchor],
) -> Classification:
    """Resolve a harness folder to the real project it collapses onto.

    Primary: the session uuid's real cwd (events.db). Fallbacks: exact match of
    the encoded segment against known real dirs, then a filesystem-validated
    decode. An unresolvable harness folder still collapses — into a synthetic
    project keyed by the encoded name — so its activity is never orphaned.
    """
    if uuid:
        cwd = session_cwds.get(uuid)
        if cwd:
            anchor = anchor_of_realpath(cwd)
            if anchor is not None:
                return Classification("A", "harness", "collapse",
                                      anchor.key, anchor.label, "session_cwd")

    anchor = enc_index.get(enc)
    if anchor is not None:
        return Classification("A", "harness", "collapse",
                              anchor.key, anchor.label, "encode_match")

    decoded = fs_decode(enc)
    if decoded is not None:
        anchor = anchor_of_realpath(decoded)
        if anchor is not None:
            return Classification("A", "harness", "collapse",
                                  anchor.key, anchor.label, "fs_decode")

    # Last resort: keep the harness folder collapsed onto a synthetic project
    # from its encoded name (never orphaned — its work still belongs to a
    # project we simply could not pin to a real path).
    label = ProjectDiscovery.extract_project_name(enc.replace("-", "/"))
    return Classification("A", "harness", "collapse",
                          f"encoded:{enc}", label or enc, "unresolved")
