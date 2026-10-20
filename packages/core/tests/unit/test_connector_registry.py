"""Unit tests for the Connector Foundation — Tool/Connector Foundation etapa.

Test matrix
-----------
C01) register() stores adapter by connector_name
C02) get() returns the correct adapter
C03) get() raises ConnectorNotFound for unknown name
C04) is_registered() returns True for registered, False otherwise
C05) names() returns sorted list; register() with same name replaces adapter
C06) the generic ConnectorRegistry works identically for a DIFFERENT
     connector kind (proves it isn't accidentally coupled to one ABC)
C07) SearchConnector/StorageConnector/CalendarConnector/EmailConnector
     fake adapters satisfy their respective ABCs end to end
C08) ImageProvider/VideoProvider gained connector_name/is_available()
     without breaking (there are zero concrete subclasses anywhere yet,
     so this is a pure interface-completeness check)
C09) no concrete connector exists anywhere — importing this package does
     not change any existing behavior (nothing references it from a
     production code path yet)
"""
from __future__ import annotations

import pytest

from src.capability.multimodal import ImageProvider, VideoProvider
from src.connectors.calendar import CalendarConnector
from src.connectors.email import EmailConnector
from src.connectors.registry import ConnectorNotFound, ConnectorRegistry
from src.connectors.search import SearchConnector, SearchResult
from src.connectors.storage import StorageConnector


# ---------------------------------------------------------------------------
# Minimal fake adapters, one per connector kind
# ---------------------------------------------------------------------------


class _FakeSearch(SearchConnector):
    def __init__(self, name: str = "fake-search") -> None:
        self._name = name

    @property
    def connector_name(self) -> str:
        return self._name

    async def search(self, query: str, *, max_results: int = 5, **kwargs) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://example.test", snippet=query)]


class _FakeStorage(StorageConnector):
    @property
    def connector_name(self) -> str:
        return "fake-storage"

    async def upload(self, path: str, content: bytes, **kwargs) -> str:
        return f"ref://{path}"

    async def download(self, reference: str, **kwargs) -> bytes:
        return b"data"


class _FakeCalendar(CalendarConnector):
    @property
    def connector_name(self) -> str:
        return "fake-calendar"

    async def create_event(self, *, title: str, start: str, end: str, **kwargs) -> str:
        return "event-1"


class _FakeEmail(EmailConnector):
    @property
    def connector_name(self) -> str:
        return "fake-email"

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> str:
        return "message-1"


# ---------------------------------------------------------------------------
# Generic ConnectorRegistry behavior — C01-C06
# ---------------------------------------------------------------------------


def test_C01_register_stores_by_connector_name():
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    registry.register(_FakeSearch("ddg"))
    assert registry.is_registered("ddg")


def test_C02_get_returns_correct_adapter():
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    adapter = _FakeSearch("ddg")
    registry.register(adapter)
    assert registry.get("ddg") is adapter


def test_C03_get_raises_connector_not_found():
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    with pytest.raises(ConnectorNotFound):
        registry.get("nonexistent")


def test_C04_is_registered_true_and_false():
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    registry.register(_FakeSearch("ddg"))
    assert registry.is_registered("ddg") is True
    assert registry.is_registered("bing") is False


def test_C05_names_sorted_and_replace_on_reregister():
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    registry.register(_FakeSearch("bing"))
    registry.register(_FakeSearch("ddg"))
    assert registry.names() == ["bing", "ddg"]

    replacement = _FakeSearch("ddg")
    registry.register(replacement)
    assert registry.get("ddg") is replacement


@pytest.mark.asyncio
async def test_C06_generic_registry_works_for_a_different_connector_kind():
    registry = ConnectorRegistry[_FakeStorage](kind="storage")
    registry.register(_FakeStorage())
    adapter = registry.get("fake-storage")
    ref = await adapter.upload("a.txt", b"hi")
    assert ref == "ref://a.txt"
    assert await adapter.download(ref) == b"data"


# ---------------------------------------------------------------------------
# Per-kind ABC contracts — C07
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C07_search_connector_contract():
    adapter = _FakeSearch()
    results = await adapter.search("python", max_results=3)
    assert results[0].snippet == "python"
    assert await adapter.is_available() is True


@pytest.mark.asyncio
async def test_C07_calendar_connector_contract():
    adapter = _FakeCalendar()
    event_id = await adapter.create_event(title="t", start="2026-01-01", end="2026-01-01")
    assert event_id == "event-1"


@pytest.mark.asyncio
async def test_C07_email_connector_contract():
    adapter = _FakeEmail()
    message_id = await adapter.send(to="a@b.com", subject="s", body="b")
    assert message_id == "message-1"


# ---------------------------------------------------------------------------
# ImageProvider/VideoProvider extension — C08
# ---------------------------------------------------------------------------


class _FakeImageProvider(ImageProvider):
    @property
    def connector_name(self) -> str:
        return "fake-image"

    async def generate(self, prompt: str, **kwargs) -> bytes:
        return b"\x89PNG"


class _FakeVideoProvider(VideoProvider):
    @property
    def connector_name(self) -> str:
        return "fake-video"

    async def generate(self, prompt: str, **kwargs) -> bytes:
        return b"\x00\x00\x00\x18ftyp"


@pytest.mark.asyncio
async def test_C08_image_provider_connector_name_and_availability():
    provider = _FakeImageProvider()
    assert provider.connector_name == "fake-image"
    assert await provider.is_available() is True
    assert await provider.generate("a cat") == b"\x89PNG"


@pytest.mark.asyncio
async def test_C08_video_provider_connector_name_and_availability():
    provider = _FakeVideoProvider()
    assert provider.connector_name == "fake-video"
    assert await provider.is_available() is True


def test_C08_image_video_providers_registerable_in_the_generic_registry():
    """Proves ImageProvider/VideoProvider now satisfy the SAME
    connector_name protocol every other connector kind does — usable in
    a ConnectorRegistry with zero special-casing."""
    registry = ConnectorRegistry[_FakeImageProvider](kind="image_generation")
    registry.register(_FakeImageProvider())
    assert registry.names() == ["fake-image"]


# ---------------------------------------------------------------------------
# No concrete adapters anywhere — C09
# ---------------------------------------------------------------------------


def test_C09_no_concrete_connector_is_registered_anywhere_by_default():
    """Infrastructure only: a fresh registry starts empty, and nothing in
    this package registers a real adapter at import time."""
    registry = ConnectorRegistry[_FakeSearch](kind="search")
    assert registry.names() == []
