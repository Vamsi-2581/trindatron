"""
Unit tests for CapabilityRouter.

Verifies that each Capability maps to the correct ExecutorKind and that
implemented/not-implemented flags are set correctly.

These tests make no LLM calls, no database calls, and no network requests.
They are pure routing logic tests.
"""
import pytest

from src.capability.classifier import Capability
from src.capability.capability_router import (
    CapabilityRoute,
    ExecutorKind,
    route,
)


# ── TEXT capabilities ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("capability", [
    Capability.CHAT,
    Capability.RESEARCH,
    Capability.CODE,
    Capability.WORK,
    Capability.FILES,
    Capability.AUTOMATION,
    Capability.AGENT,
])
def test_text_capabilities_route_to_text_executor(capability):
    result = route(capability)
    assert result.executor == ExecutorKind.TEXT


@pytest.mark.parametrize("capability", [
    Capability.CHAT,
    Capability.RESEARCH,
    Capability.CODE,
    Capability.WORK,
    Capability.FILES,
    Capability.AUTOMATION,
    Capability.AGENT,
])
def test_text_capabilities_are_implemented(capability):
    result = route(capability)
    assert result.implemented is True


# ── MULTIMODAL capabilities — executor assigned but not implemented ────────────

def test_image_routes_to_image_executor():
    result = route(Capability.IMAGE)
    assert result.executor == ExecutorKind.IMAGE


def test_image_is_not_implemented():
    result = route(Capability.IMAGE)
    assert result.implemented is False


def test_video_routes_to_video_executor():
    result = route(Capability.VIDEO)
    assert result.executor == ExecutorKind.VIDEO


def test_video_is_not_implemented():
    result = route(Capability.VIDEO)
    assert result.implemented is False


def test_audio_routes_to_audio_executor():
    result = route(Capability.AUDIO)
    assert result.executor == ExecutorKind.AUDIO


def test_audio_is_not_implemented():
    result = route(Capability.AUDIO)
    assert result.implemented is False


# ── CapabilityRoute contract ──────────────────────────────────────────────────

def test_route_returns_capability_route_instance():
    result = route(Capability.CHAT)
    assert isinstance(result, CapabilityRoute)


def test_capability_route_preserves_input_capability():
    for cap in Capability:
        result = route(cap)
        assert result.capability is cap


def test_capability_route_is_immutable():
    result = route(Capability.CHAT)
    with pytest.raises((AttributeError, TypeError)):
        result.executor = ExecutorKind.IMAGE  # type: ignore[misc]


# ── All 10 capabilities are covered ──────────────────────────────────────────

def test_all_capabilities_produce_a_route():
    """Every Capability value must return a valid CapabilityRoute."""
    for cap in Capability:
        result = route(cap)
        assert isinstance(result, CapabilityRoute)
        assert isinstance(result.executor, ExecutorKind)
        assert isinstance(result.implemented, bool)


# Executors with a concrete, wired implementation. Adding to this set is a
# deliberate act: it is what flips a capability from "intercepted with a
# coming-soon reply" to "dispatched at a real executor".
WIRED = {ExecutorKind.TEXT, ExecutorKind.PRESENTATION}


def test_only_wired_executors_are_implemented():
    """TEXT and PRESENTATION are wired; IMAGE/VIDEO/AUDIO are not.

    PRESENTATION being "implemented" means the code path exists, not that
    every deployment can run it — the engine also has to be configured and
    healthy, which presentation_service.availability() answers separately.
    """
    for cap in Capability:
        result = route(cap)
        if result.executor in WIRED:
            assert result.implemented is True, f"{cap} should be implemented"
        else:
            assert result.implemented is False, f"{cap} should NOT be implemented"


# ── ExecutorKind values ───────────────────────────────────────────────────────

def test_executor_kind_values_are_pinned():
    assert set(ExecutorKind) == {
        ExecutorKind.TEXT,
        ExecutorKind.IMAGE,
        ExecutorKind.VIDEO,
        ExecutorKind.AUDIO,
        ExecutorKind.PRESENTATION,
    }
