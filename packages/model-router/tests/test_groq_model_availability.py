"""
Live integration guard against the exact regression found 2026-09-06: the
Groq catalog entry pointed at meta-llama/llama-4-scout-17b-16e-instruct,
which Groq's API returned as model_not_found for this project's account —
confirmed only by calling Groq's real GET /v1/models, not by trusting Groq's
own docs or the model existing in general. This test re-runs that same
real check so a future model swap that isn't actually available to this
project's GROQ_API_KEY fails loudly instead of silently reaching production.

Skipped when GROQ_API_KEY is not set (e.g. CI without live secrets) — this
is a live-network guard, not a substitute for the offline catalog tests in
test_providers.py.
"""
import os

import httpx
import pytest

from trinity_router.providers import PROVIDER_CATALOG

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


@pytest.mark.skipif(not GROQ_API_KEY, reason="GROQ_API_KEY not set — skipping live Groq model check")
def test_catalog_groq_models_are_available_to_this_account():
    resp = httpx.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        timeout=20.0,
    )
    assert resp.status_code == 200, (
        f"Groq /v1/models returned {resp.status_code} — cannot validate catalog "
        f"model availability (check GROQ_API_KEY is valid)"
    )
    available_ids = {m["id"] for m in resp.json().get("data", [])}

    for key, model in PROVIDER_CATALOG.items():
        if model.provider != "groq":
            continue
        raw_model_id = model.model_id.removeprefix("groq/")
        assert raw_model_id in available_ids, (
            f"Catalog entry '{key}' points at model_id={model.model_id!r} "
            f"(raw id {raw_model_id!r}), which is NOT in the models list "
            f"Groq's API returns for this project's GROQ_API_KEY. "
            f"This is the same failure mode as the 2026-09-06 regression — "
            f"do not swap in a replacement model without first confirming "
            f"it appears in this same live list."
        )
