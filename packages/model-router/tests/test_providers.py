"""
Tests for the ProviderModel catalog.
Validates the modality field default and ensures existing entries are unaffected.
"""
import pytest

from trinity_router.providers import PROVIDER_CATALOG, ProviderModel, get_eligible_models


def test_all_existing_providers_have_text_modality():
    for key, model in PROVIDER_CATALOG.items():
        assert model.modality == "text", (
            f"Provider '{key}' has modality={model.modality!r}, expected 'text'"
        )


def test_modality_defaults_to_text():
    m = ProviderModel(
        provider="test",
        model_id="test/model",
        context_window=4096,
        cost_per_1m_input_usd=1.0,
        cost_per_1m_output_usd=2.0,
    )
    assert m.modality == "text"


def test_modality_can_be_overridden():
    m = ProviderModel(
        provider="test",
        model_id="test/image-model",
        context_window=0,
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        supports_tools=False,
        modality="image",
    )
    assert m.modality == "image"


def test_provider_catalog_has_expected_entries():
    expected_keys = {
        "groq-llama4-scout",
        "claude-opus", "claude-sonnet", "claude-haiku",
        "gpt-4o", "gpt-4o-mini",
        # gemini-25-flash removed 2026-09-13 - unreachable by cost rank.
        "gemini-25-flash-lite",
        "xai-grok", "mistral-small",
    }
    assert expected_keys == set(PROVIDER_CATALOG.keys())


def test_provider_model_is_immutable():
    m = PROVIDER_CATALOG["claude-sonnet"]
    with pytest.raises((AttributeError, TypeError)):
        m.modality = "image"  # type: ignore[misc]


def test_mistral_small_only_eligible_when_capability_bar_is_met():
    """Regression guard: mistral-small ("low" quality_class) must be
    included when a request's bar is at or below "low", and excluded the
    moment a caller requires "standard" or higher — being cheap must
    never let it slip past a capability gate it doesn't actually clear.
    Uses the real get_eligible_models() gate, not a reimplementation."""
    mistral = PROVIDER_CATALOG["mistral-small"]

    eligible_low = get_eligible_models([mistral], min_quality_class="low")
    assert mistral in eligible_low, "mistral-small must be eligible for a 'low' bar"

    eligible_standard = get_eligible_models([mistral], min_quality_class="standard")
    assert mistral not in eligible_standard, (
        "mistral-small is 'low' quality_class and must NOT be eligible for "
        "a 'standard'-or-higher bar, regardless of how cheap it is"
    )

    # Tool-requiring requests: mistral-small genuinely supports tools
    # (live-verified via test_mistral_model_availability.py), so it must
    # not be excluded by that gate specifically.
    eligible_tools = get_eligible_models([mistral], requires_tools=True, min_quality_class="low")
    assert mistral in eligible_tools
