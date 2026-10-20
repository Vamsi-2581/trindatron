"""
VoiceProviderRegistry — config-driven, dependency-injectable voice provider registry.

Maps provider_name (str) -> VoiceProvider adapter instance.

Mirrors src/billing/providers/registry.py so TRINITY Core never hardcodes a
single voice vendor. ElevenLabs is the reference adapter today; OpenAI,
Google, or others can be registered later with zero changes to routes or
callers.

Thread safety
-------------
Registration happens once at startup before request handling begins.
After startup, _providers is read-only — no locking needed.
"""
from __future__ import annotations

from src.capability.multimodal import VoiceProvider


class VoiceProviderNotFound(Exception):
    """Raised when no adapter is registered for the requested provider name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"No voice provider registered for: {name!r}")


class VoiceProviderRegistry:
    """Thread-safe, dependency-injectable registry of VoiceProvider adapters.

    Usage — startup (main.py lifespan):
        registry = VoiceProviderRegistry()
        registry.register(ElevenLabsVoiceProvider(api_key=settings.elevenlabs_api_key))
        set_voice_registry(registry)

    Usage — route handler:
        registry: VoiceProviderRegistry = Depends(get_voice_registry)
        adapter = registry.get(settings.voice_provider)

    Usage — tests:
        mock_registry = VoiceProviderRegistry()
        mock_registry.register(FakeVoiceProvider())
        app.dependency_overrides[get_voice_registry] = lambda: mock_registry
    """

    def __init__(self) -> None:
        self._providers: dict[str, VoiceProvider] = {}

    def register(self, adapter: VoiceProvider) -> None:
        """Register a provider adapter.

        Idempotent for the same adapter.provider_name — re-registering
        replaces the existing entry silently. Allows tests to swap adapters.
        """
        self._providers[adapter.provider_name] = adapter

    def get(self, name: str) -> VoiceProvider:
        """Return the adapter for name, or raise VoiceProviderNotFound.

        Never returns None — callers must handle VoiceProviderNotFound explicitly.
        """
        try:
            return self._providers[name]
        except KeyError:
            raise VoiceProviderNotFound(name)

    def is_registered(self, name: str) -> bool:
        """Return True if an adapter is registered for name."""
        return name in self._providers

    def names(self) -> list[str]:
        """Return a sorted list of registered provider names."""
        return sorted(self._providers.keys())
