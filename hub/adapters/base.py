"""Abstract base adapter for converting provider entries to unified models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hub.models.base import UnifiedEvent, UnifiedMessage


class BaseAdapter(ABC):

    @abstractmethod
    def to_unified(self, entry: Any, project: str) -> UnifiedMessage | None:
        """Convert a provider-specific entry to UnifiedMessage.

        Returns None if the entry should be skipped.
        """
        ...

    @abstractmethod
    def to_event(self, entry: Any, project: str) -> UnifiedEvent | None:
        """Convert a provider-specific entry to a lightweight UnifiedEvent for SSE.

        Returns None if the entry should be skipped.
        """
        ...
