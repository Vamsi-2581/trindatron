"""
Live integration guard for the xAI catalog entry, mirroring
test_groq_model_availability.py's rationale: confirm the configured
model_id is actually available to this account via xAI's real
GET /v1/models, not merely assumed from docs or a prior manual check.

Skipped when XAI_API_KEY is not set (e.g. CI without live secrets).
"""
import os

import httpx
import pytest

from trinity_router.providers import PROVIDER_CATALOG

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")


@pytest.mark.skipif(not XAI_API_KEY, reason="XAI_API_KEY not set — skipping live xAI model check")
def test_catalog_xai_models_are_available_to_this_account():
    resp = httpx.get(
        "https://api.x.ai/v1/models",
        headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        timeout=20.0,
    )
    assert resp.status_code == 200, (
        f"xAI /v1/models returned {resp.status_code} — cannot validate catalog "
        f"model availability (check XAI_API_KEY is valid)"
    )
    available_ids = {m["id"] for m in resp.json().get("data", [])}

    for key, model in PROVIDER_CATALOG.items():
        if model.provider != "xai":
            continue
        raw_model_id = model.model_id.removeprefix("xai/")
        assert raw_model_id in available_ids, (
            f"Catalog entry '{key}' points at model_id={model.model_id!r} "
            f"(raw id {raw_model_id!r}), which is NOT in the models list "
            f"xAI's API returns for this account's XAI_API_KEY."
        )


@pytest.mark.skipif(not XAI_API_KEY, reason="XAI_API_KEY not set — skipping live xAI inference check")
def test_xai_grok_real_inference_via_litellm():
    """Real, minimal, no-fallback inference call through the exact same
    litellm.completion(model=...) shape router.py uses for every provider."""
    import litellm

    os.environ.setdefault("XAI_API_KEY", XAI_API_KEY)
    model_id = PROVIDER_CATALOG["xai-grok"].model_id
    resp = litellm.completion(
        model=model_id,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5,
        timeout=25,
    )
    assert resp.choices[0].message is not None
