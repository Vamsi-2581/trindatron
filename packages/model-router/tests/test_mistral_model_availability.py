"""
Live integration guard for the Mistral catalog entry, mirroring
test_groq_model_availability.py / test_xai_model_availability.py's
rationale: confirm the configured model_id is actually available to this
account via Mistral's real GET /v1/models, and that a real inference +
real tool-calling round trip succeeds through the exact same
litellm.completion(model=...) shape router.py uses for every provider.

Skipped when MISTRAL_API_KEY is not set (e.g. CI without live secrets).
"""
import os

import httpx
import pytest

from trinity_router.providers import PROVIDER_CATALOG

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")


@pytest.mark.skipif(not MISTRAL_API_KEY, reason="MISTRAL_API_KEY not set — skipping live Mistral model check")
def test_catalog_mistral_models_are_available_to_this_account():
    resp = httpx.get(
        "https://api.mistral.ai/v1/models",
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
        timeout=20.0,
    )
    assert resp.status_code == 200, (
        f"Mistral /v1/models returned {resp.status_code} — cannot validate "
        f"catalog model availability (check MISTRAL_API_KEY is valid)"
    )
    available_ids = {m["id"] for m in resp.json().get("data", [])}

    for key, model in PROVIDER_CATALOG.items():
        if model.provider != "mistral":
            continue
        raw_model_id = model.model_id.removeprefix("mistral/")
        assert raw_model_id in available_ids, (
            f"Catalog entry '{key}' points at model_id={model.model_id!r} "
            f"(raw id {raw_model_id!r}), which is NOT in the models list "
            f"Mistral's API returns for this account's MISTRAL_API_KEY."
        )


@pytest.mark.skipif(not MISTRAL_API_KEY, reason="MISTRAL_API_KEY not set — skipping live Mistral inference check")
def test_mistral_small_real_inference_via_litellm():
    """Real, minimal, no-fallback inference call through the exact same
    litellm.completion(model=...) shape router.py uses for every provider."""
    import litellm

    os.environ.setdefault("MISTRAL_API_KEY", MISTRAL_API_KEY)
    model_id = PROVIDER_CATALOG["mistral-small"].model_id
    resp = litellm.completion(
        model=model_id,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5,
        timeout=25,
    )
    assert resp.choices[0].message is not None


@pytest.mark.skipif(not MISTRAL_API_KEY, reason="MISTRAL_API_KEY not set — skipping live Mistral tool-call check")
def test_mistral_small_real_tool_calling_via_litellm():
    """Real tool-calling round trip — proves supports_tools=True is not
    just copied from Mistral's advertised capability, but actually works
    through this exact router's call shape."""
    import litellm

    os.environ.setdefault("MISTRAL_API_KEY", MISTRAL_API_KEY)
    model_id = PROVIDER_CATALOG["mistral-small"].model_id
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }]
    resp = litellm.completion(
        model=model_id,
        messages=[{"role": "user", "content": "What is the weather in Paris? Use the tool."}],
        tools=tools,
        tool_choice="auto",
        max_tokens=100,
        timeout=25,
    )
    tool_calls = resp.choices[0].message.tool_calls
    assert tool_calls, "expected mistral-small to emit a real tool_call"
    assert tool_calls[0].function.name == "get_weather"
