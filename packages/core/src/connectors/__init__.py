"""Connector Foundation — infrastructure only, zero concrete adapters.

See registry.py's own module docstring for the full design rationale.
Image/video generation connectors are NOT re-exported here: they remain
src.capability.multimodal.ImageProvider / VideoProvider (extended with
connector_name/is_available() during this same etapa) — not duplicated
under a new name, per "não simplesmente remover a implementação
existente."
"""
from src.connectors.calendar import CalendarConnector, CalendarConnectorError
from src.connectors.email import EmailConnector, EmailConnectorError
from src.connectors.registry import ConnectorNotFound, ConnectorRegistry
from src.connectors.search import SearchConnector, SearchConnectorError, SearchResult
from src.connectors.storage import StorageConnector, StorageConnectorError

__all__ = [
    "ConnectorNotFound",
    "ConnectorRegistry",
    "SearchConnector",
    "SearchConnectorError",
    "SearchResult",
    "StorageConnector",
    "StorageConnectorError",
    "CalendarConnector",
    "CalendarConnectorError",
    "EmailConnector",
    "EmailConnectorError",
]
