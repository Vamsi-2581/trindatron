"""SearchConnector — contract for a real, external search API (replacing
the agent's own hand-rolled DuckDuckGo call with a genuine, swappable
provider). No concrete implementation yet — infrastructure only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class SearchConnectorError(Exception):
    """Base exception for SearchConnector failures."""


class SearchConnector(ABC):
    """Provider that searches the web and returns structured results."""

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "duckduckgo", "bing"."""

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 5, **kwargs) -> list[SearchResult]:
        """Run a search and return up to max_results results.

        Raises:
            SearchConnectorError: if the underlying provider call fails.
        """

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        API key is configured). Defaults to True."""
        return True
