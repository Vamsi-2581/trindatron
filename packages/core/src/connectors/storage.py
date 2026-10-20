"""StorageConnector — contract for a real, external file storage service
(Drive/Dropbox/S3-style). No concrete implementation yet — infrastructure
only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class StorageConnectorError(Exception):
    """Base exception for StorageConnector failures."""


class StorageConnector(ABC):
    """Provider that stores and retrieves files in an external service."""

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "google_drive", "s3"."""

    @abstractmethod
    async def upload(self, path: str, content: bytes, **kwargs) -> str:
        """Upload content and return a reference (URL or provider-specific ID).

        Raises:
            StorageConnectorError: if the underlying provider call fails.
        """

    @abstractmethod
    async def download(self, reference: str, **kwargs) -> bytes:
        """Download content by the reference upload() previously returned.

        Raises:
            StorageConnectorError: if the underlying provider call fails.
        """

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        API key/OAuth token is configured). Defaults to True."""
        return True
