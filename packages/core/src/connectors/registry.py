"""
ConnectorRegistry — generic, reusable registry mechanics for ANY connector
kind, shared across search/storage/calendar/email/image-generation/
video-generation/etc.

Design rationale (Post-V2 Architecture Audit, Tool/Connector Foundation
etapa): this codebase already has this exact registry shape implemented
independently three times — billing.providers.registry.ProviderRegistry,
voice.registry.VoiceProviderRegistry, and (implicitly) trinity_agent's
own ToolRegistry — each register()/get()/is_registered()/names() over a
dict[str, SomeAdapterABC] keyed by an adapter-declared name property.
Rather than hand-rolling that boilerplate a fourth, fifth, and sixth time
for Search/Storage/Calendar/Email connectors, this module generalizes ONLY
the registry mechanics into one reusable, generic class.

What this deliberately does NOT do: force different services into one
artificial shared interface. Each connector KIND (SearchConnector,
StorageConnector, CalendarConnector, EmailConnector, ImageProvider,
VideoProvider, ...) keeps its own, independently-defined ABC with
whatever methods make sense for that kind of service — this class only
needs each adapter to expose a `connector_name` property (mirroring
ProviderRegistry's own `provider_name` / VoiceProviderRegistry's
`provider_name` convention), nothing else.

Pre-existing ProviderRegistry/VoiceProviderRegistry are NOT retrofitted
to use this — they are working, tested, production classes with a real
concrete adapter each (Binance Pay/NOWPayments; ElevenLabs); refactoring
them for a purely cosmetic DRY gain would be unnecessary regression risk.
This class is the pattern for every NEW connector kind going forward.

Zero concrete adapters exist for any of the kinds below yet — this is
infrastructure only, exactly like ProviderRegistry's own "Billing-2 ships
with an empty registry" precedent.
"""
from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable


@runtime_checkable
class _NamedConnector(Protocol):
    @property
    def connector_name(self) -> str: ...


T = TypeVar("T", bound=_NamedConnector)


class ConnectorNotFound(Exception):
    """Raised when no adapter is registered for the requested connector name."""

    def __init__(self, kind: str, name: str) -> None:
        self.kind = kind
        self.name = name
        super().__init__(f"No {kind} connector registered for: {name!r}")


class ConnectorRegistry(Generic[T]):
    """Thread-safe (registration happens once at startup, read-only after),
    dependency-injectable registry of connector adapters for ONE kind.

    Usage — startup (main.py lifespan), once a real adapter exists:
        search_registry: ConnectorRegistry[SearchConnector] = ConnectorRegistry(kind="search")
        search_registry.register(MySearchAdapter())
        set_search_connector_registry(search_registry)

    Usage — route handler:
        registry: ConnectorRegistry[SearchConnector] = Depends(get_search_connector_registry)
        adapter = registry.get(connector_name)

    Usage — tests:
        registry = ConnectorRegistry[SearchConnector](kind="search")
        registry.register(FakeSearchConnector())
    """

    def __init__(self, *, kind: str) -> None:
        self._kind = kind
        self._adapters: dict[str, T] = {}

    def register(self, adapter: T) -> None:
        """Register a connector adapter. Idempotent for the same
        adapter.connector_name — re-registering replaces the existing
        entry silently, allowing tests to swap adapters."""
        self._adapters[adapter.connector_name] = adapter

    def get(self, name: str) -> T:
        """Return the adapter for name, or raise ConnectorNotFound. Never
        returns None — callers must handle ConnectorNotFound explicitly."""
        try:
            return self._adapters[name]
        except KeyError:
            raise ConnectorNotFound(self._kind, name)

    def is_registered(self, name: str) -> bool:
        return name in self._adapters

    def names(self) -> list[str]:
        return sorted(self._adapters.keys())

    @property
    def kind(self) -> str:
        return self._kind
