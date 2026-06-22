"""Abstract base analyzer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hub.models.base import UnifiedMessage


class BaseAnalyzer(ABC):
    """Analyzes a list of UnifiedMessages and produces insights."""

    def __init__(self, *, complete: bool = False) -> None:
        self.complete = complete

    @property
    @abstractmethod
    def name(self) -> str:
        """Short name for this analyzer (used in filenames)."""
        ...

    @property
    @abstractmethod
    def title(self) -> str:
        """Human-readable title for the report."""
        ...

    @abstractmethod
    def analyze(self, messages: list[UnifiedMessage]) -> dict[str, Any]:
        """Run analysis and return structured results."""
        ...

    @abstractmethod
    def render_markdown(self, results: dict[str, Any]) -> str:
        """Render results as Markdown text."""
        ...
