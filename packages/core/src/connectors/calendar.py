"""CalendarConnector — contract for a real, external calendar service
(Google Calendar/Outlook-style). No concrete implementation yet —
infrastructure only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CalendarConnectorError(Exception):
    """Base exception for CalendarConnector failures."""


class CalendarConnector(ABC):
    """Provider that creates events in an external calendar service.

    Side-effect classification note: creating a calendar event is an
    EXTERNAL_SIDE_EFFECT (Tool Contract, trinity_agent.tools.base.SideEffect)
    at the TOOL layer that wraps a call to this connector — this ABC itself
    carries no approval/permission metadata; that belongs on the Tool, not
    the Connector (see the Tool/Connector Foundation report's Capability
    <-> Tool relationship section for why these stay separate concepts).
    """

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "google_calendar"."""

    @abstractmethod
    async def create_event(self, *, title: str, start: str, end: str, **kwargs) -> str:
        """Create a calendar event and return its reference/event ID.

        Raises:
            CalendarConnectorError: if the underlying provider call fails.
        """

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        OAuth token is configured). Defaults to True."""
        return True
