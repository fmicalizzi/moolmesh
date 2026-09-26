"""Cloud-only placeholder detection (issue #45).

Sync clients (iCloud Drive, OneDrive, Dropbox "online-only", Google Drive
streaming) leave local stand-ins whose bytes live in the cloud. Opening,
reading — or, for a dataless DIRECTORY, merely listing it — makes the OS
download the content, which can stall for minutes on a slow link and breaks
the zero-cloud promise (MoolMesh must never trigger a download).

``is_cloud_placeholder`` answers from metadata alone: one ``os.lstat`` plus the
file name. It never opens the path. Callers that walk history (``mool
backfill``, the daemon's startup catch-up) skip what it flags and report it.
"""

from __future__ import annotations

import os
from pathlib import Path

# macOS / BSD ``st_flags``: the file's data (or a directory's entries) is not
# materialized locally. Defined in <sys/stat.h> since macOS 10.15.
SF_DATALESS = 0x40000000

# Windows ``st_file_attributes`` (winnt.h). Cloud Files API placeholders carry
# RECALL_ON_DATA_ACCESS / RECALL_ON_OPEN; legacy HSM/offline files carry OFFLINE.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_CLOUD_MASK = (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_OFFLINE
)


def is_cloud_placeholder(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` is a cloud-only stand-in (file or directory).

    Metadata only — the path is never opened, read, or listed:

    * legacy iCloud stubs are named ``.<name>.icloud`` (any OS);
    * macOS: ``st_flags & SF_DATALESS``;
    * Windows: ``st_file_attributes`` recall/offline bits.

    A path that cannot be stat'ed is not reported as a placeholder (the caller's
    own stat/open handles the error as it does today).
    """
    name = os.path.basename(os.fspath(path))
    if name.startswith(".") and name.endswith(".icloud") and len(name) > len("..icloud"):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    flags = getattr(st, "st_flags", 0) or 0
    if flags & SF_DATALESS:
        return True
    attrs = getattr(st, "st_file_attributes", 0) or 0
    return bool(attrs & _WIN_CLOUD_MASK)


class PlaceholderSkipper:
    """``skip_dir`` callback for ``ProjectDiscovery`` that records what it skipped.

    Discovery asks it before listing any directory; a dataless directory is
    skipped (listing it would trigger the download) and remembered so the
    caller can report it.
    """

    def __init__(self) -> None:
        self.skipped: list[Path] = []

    def __call__(self, path: Path) -> bool:
        if is_cloud_placeholder(path):
            self.skipped.append(Path(path))
            return True
        return False
