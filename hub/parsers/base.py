"""Abstract base parser for all providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


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
