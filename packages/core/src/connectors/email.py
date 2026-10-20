"""EmailConnector — contract for a real, external email-sending service
(SendGrid/SES/SMTP-style). No concrete implementation yet — infrastructure
only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class EmailConnectorError(Exception):
    """Base exception for EmailConnector failures."""


class EmailConnector(ABC):
    """Provider that sends email through an external service.

    Side-effect classification note: sending an email is an
    EXTERNAL_SIDE_EFFECT (Tool Contract, trinity_agent.tools.base.SideEffect)
    at the TOOL layer that wraps a call to this connector, same as
    CalendarConnector above — this ABC itself carries no approval/
    permission metadata.
    """

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "sendgrid", "ses"."""

    @abstractmethod
    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> str:
        """Send an email and return a provider message ID/reference.

        Raises:
            EmailConnectorError: if the underlying provider call fails.
        """

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        API key is configured). Defaults to True."""
        return True
