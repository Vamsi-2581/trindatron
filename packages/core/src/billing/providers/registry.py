"""
ProviderRegistry — config-driven, dependency-injectable payment provider registry.

Maps provider_name (str) → PaymentProvider adapter instance.

Design
------
* Zero concrete adapters in TRINITY Core — only the registry infrastructure.
* Providers are registered once at startup (main.py lifespan).
* Billing-2 ships with an empty registry; adapters are out of scope.
* The registry is injectable via FastAPI Depends(get_provider_registry).
* Tests override via app.dependency_overrides[get_provider_registry].

Thread safety
-------------
Registration happens once at startup before request handling begins.
After startup, _providers is read-only — no locking needed.
"""
from __future__ import annotations

from src.billing.providers.base import PaymentProvider


class ProviderNotFound(Exception):
    """Raised when no adapter is registered for the requested provider name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"No payment provider registered for: {name!r}")


class ProviderRegistry:
    """Thread-safe, dependency-injectable registry of PaymentProvider adapters.

    Usage — startup (main.py lifespan):
        registry = ProviderRegistry()
        registry.register(MyAdapter())
        set_provider_registry(registry)

    Usage — route handler:
        registry: ProviderRegistry = Depends(get_provider_registry)
        adapter = registry.get(provider_name)

    Usage — tests:
        mock_registry = ProviderRegistry()
        mock_registry.register(FakeAdapter())
        app.dependency_overrides[get_provider_registry] = lambda: mock_registry
    """

    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, adapter: PaymentProvider) -> None:
        """Register a provider adapter.

        Idempotent for the same adapter.provider_name — re-registering replaces
        the existing entry silently. Allows tests to swap adapters.
        """
        self._providers[adapter.provider_name] = adapter

    def get(self, name: str) -> PaymentProvider:
        """Return the adapter for name, or raise ProviderNotFound.

        Never returns None — callers must handle ProviderNotFound explicitly.
        """
        try:
            return self._providers[name]
        except KeyError:
            raise ProviderNotFound(name)

    def is_registered(self, name: str) -> bool:
        """Return True if an adapter is registered for name."""
        return name in self._providers

    def names(self) -> list[str]:
        """Return a sorted list of registered provider names."""
        return sorted(self._providers.keys())
