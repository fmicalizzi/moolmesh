"""Abstract base parser for all providers."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def iter_complete_lines(path: Path, offset: int) -> Iterator[tuple[str, int]]:
    """Yield ``(line, end_offset)`` for each complete line after ``offset``.

    Streams the file line by line (issue #52): memory is bounded by the
    longest line, never by the file — the old read-everything-then-split
    peaked at several times the size of a 130 MB session file. Only lines
    terminated by ``\\n`` are yielded (chunk-and-tail): a partial last line
    is left for the next call, and ``end_offset`` is the byte offset just
    past the yielded line, so the caller's new offset is the last one seen
    (or ``offset`` when nothing was yielded). An ``offset`` beyond the end
    of the file means the file was rewritten: reading restarts at 0, and a
    blank ``("", 0)`` is yielded first so the caller's offset resets even
    when no complete line follows yet.
    Lines are decoded UTF-8 with replacement and stripped (a blank line is
    yielded as ``""`` so its bytes still advance the offset).
    """
    with open(path, "rb") as f:
        if offset > os.fstat(f.fileno()).st_size:
            offset = 0
            yield "", 0
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # incomplete tail: re-read once the writer finishes it
            pos += len(raw)
            yield raw.decode("utf-8", errors="replace").strip(), pos


class BaseParser(ABC):

    @abstractmethod
    def parse_file(self, path: Path) -> list[Any]:
        """Full-file parse. Returns provider-specific entry objects."""
        ...

    @abstractmethod
    def parse_incremental(self, path: Path, offset: int) -> tuple[list[Any], int]:
        """Parse new content from byte offset.

        Returns (new_entries, new_byte_offset).
        """
        ...

    @staticmethod
    @abstractmethod
    def can_parse(path: Path) -> bool:
        """Check if this parser can handle the given file."""
        ...
