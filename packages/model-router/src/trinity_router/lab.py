"""Candidate models under evaluation — deliberately NOT in PROVIDER_CATALOG.

Why a separate registry instead of a flag on the catalog
--------------------------------------------------------
A `benchmark_only=True` field on a catalog entry would be one forgotten
`if` away from a model reaching a live user. Routing selects from
PROVIDER_CATALOG via selector.py's curated chains; a model that is not in
that dict cannot be selected by any code path, present or future, without
someone deliberately moving it. The isolation is structural rather than
conditional, which is the only kind worth relying on for something that
spends money.

Nothing in this module is imported by router.py, selector.py, or any
service. It exists to give the benchmark a single, reviewed source of
truth for model ids, prices and capabilities, so those facts are written
down once and tested rather than retyped into a script.

Pricing provenance
------------------
Every figure here is from the PROVIDER'S OWN documentation, checked on
2026-09-12, not from an aggregator. Where litellm's cost map disagrees,
the provider's number wins and the disagreement is recorded on the entry —
litellm is a convenience, not an authority, and our TCU accounting derives
from these numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LabModel:
    """A candidate being evaluated, with the facts needed to judge it."""

    key: str
    provider: str
    #: The litellm model string. Verified to exist in litellm.model_cost, or
    #: flagged below when it does not.
    model_id: str
    #: Base URL for a direct, non-litellm call (the benchmark harness uses
    #: this so a missing litellm route never blocks measurement).
    api_base: str
    #: Environment variable holding the credential. Never the credential.
    api_key_env: str

    context_window: int
    cost_per_1m_input_usd: float
    cost_per_1m_output_usd: float
    #: Cached-input price. The System Policy is a constant ~1,070-token
    #: prefix — exactly the shape that caches — so this column, not the
    #: headline input price, is where the policy's cost actually lands.
    cost_per_1m_cached_input_usd: float | None = None

    #: Off-peak multiplier, where the provider runs time-of-day pricing.
    #: 1.0 = no such scheme. Relevant to a future scheduler, not used yet.
    offpeak_multiplier: float = 1.0
    offpeak_note: str = ""

    supports_tools: bool = True
    supports_structured_output: bool = True
    supports_vision: bool = False
    supports_video: bool = False
    supports_reasoning_effort: bool = False

    #: Weights licence and whether commercial use is permitted. Checked
    #: before anything could be self-hosted — a permissive API does not
    #: imply permissive weights.
    weights_license: str = "unknown"
    weights_public: bool = False
    commercial_use: bool = False

    notes: tuple[str, ...] = field(default_factory=tuple)


#: Candidates. BENCHMARK ONLY — see the module docstring.
LAB_CATALOG: dict[str, LabModel] = {
    # ── Z.ai GLM-5.3-Flash ───────────────────────────────────────────────
    # Official docs (docs.z.ai) on 2026-09-12: model id "glm-5.3-flash",
    # OpenAI-compatible at https://api.z.ai/api/paas/v4/chat/completions with
    # Bearer auth, 1M context, function calling, structured output, native
    # multimodal (images, video, files), reasoning_effort exposed.
    #
    # litellm carries "zai/glm-5.3-flash" at exactly $0.15/$0.50, matching
    # the official price — so this needs a catalog entry and no adapter code
    # whatsoever if it is ever promoted.
    "glm-5.3-flash": LabModel(
        key="glm-5.3-flash",
        provider="zai",
        model_id="zai/glm-5.3-flash",
        api_base="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        context_window=1_000_000,
        cost_per_1m_input_usd=0.15,
        cost_per_1m_output_usd=0.50,
        cost_per_1m_cached_input_usd=0.03,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=True,
        supports_video=True,
        supports_reasoning_effort=True,
        weights_license="MIT",
        weights_public=True,
        commercial_use=True,
        notes=(
            "Output is 17% cheaper than gpt-4o-mini ($0.50 vs $0.60); input is "
            "identical. The real gap is cached input: $0.03 against gpt-4o-mini's "
            "$0.075, i.e. 2.5x cheaper on exactly the constant prefix our System "
            "Policy occupies.",
            "MIT weights, 320B-A18B MoE (~328 GB at full precision) — the only "
            "candidate here that is both cheap via API and legally self-hostable.",
            "Z.ai runs a points/quota scheme with a 50% off-peak discount; the "
            "per-token figures above are the documented list prices.",
        ),
    ),

    # ── Z.ai GLM-4.7-Flash — the genuinely free one ──────────────────────
    # Worth recording because the mission's premise ("cheaper or free") is
    # only half right about GLM-5.3-Flash: that model is the CHEAPEST PAID
    # model in the GLM-5 series at $0.15/$0.50, not free. The free tier sits
    # on the previous generation, and litellm prices both glm-4.7-flash and
    # glm-4.5-flash at exactly $0.00.
    #
    # A free model with 200K context and working tool calling is a serious
    # candidate for the cheap end of the router — but only where its rate
    # limit fits. A free tier is a quota, not capacity, so this can never be
    # a sole primary for a plan; it is a candidate for specific low-volume
    # task classes with a paid fallback behind it.
    "glm-4.7-flash": LabModel(
        key="glm-4.7-flash",
        provider="zai",
        model_id="zai/glm-4.7-flash",
        api_base="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        context_window=200_000,
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=False,
        supports_video=False,
        supports_reasoning_effort=False,
        weights_license="MIT",
        weights_public=True,
        commercial_use=True,
        notes=(
            "Free at the API. That makes cost-per-task zero and the VALUE score "
            "undefined rather than infinite — it has to be judged on whether "
            "quality clears the bar, with rate limit as the constraint.",
            "A free tier is a quota, not capacity. Reported limits are on the "
            "order of a few requests per minute and a few hundred per day, so "
            "this is a candidate for specific low-volume classes with a paid "
            "fallback, never a plan's sole primary.",
            "No vision. Narrower context than 5.3-Flash (200K vs 1M).",
        ),
    ),

    # ── DeepSeek V4.1-Flash ──────────────────────────────────────────────
    # Official docs (api-docs.deepseek.com) on 2026-09-12: the CURRENT model
    # id is "deepseek-flash"; "deepseek-v4-flash" and
    # "deepseek-v4-flash-vision-exp" are accepted legacy aliases.
    #
    # Two discrepancies worth recording rather than smoothing over:
    #   1. litellm has NO entry for "deepseek/deepseek-flash" — only the
    #      legacy ids. A promotion would need the current id registered, or
    #      cost tracking silently falls back to zero.
    #   2. litellm prices the legacy id at $0.44/$1.32; the provider documents
    #      $0.30/$1.20 at PEAK. The provider's figure is used here.
    "deepseek-flash": LabModel(
        key="deepseek-flash",
        provider="deepseek",
        model_id="deepseek/deepseek-flash",
        api_base="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        context_window=1_000_000,
        # PEAK rates. Recorded as the pessimistic case deliberately: a cost
        # model that assumes the discount is a cost model that is wrong half
        # the working week.
        cost_per_1m_input_usd=0.30,
        cost_per_1m_output_usd=1.20,
        cost_per_1m_cached_input_usd=0.006,
        offpeak_multiplier=0.5,
        offpeak_note=(
            "Peak is 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri; everything else "
            "is off-peak at half price. Off-peak lands at $0.15/$0.60 — exactly "
            "gpt-4o-mini's rate, not below it."
        ),
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=True,
        supports_video=False,
        supports_reasoning_effort=True,
        weights_license="MIT",
        weights_public=True,
        commercial_use=True,
        notes=(
            "At PEAK this is 2x the price of gpt-4o-mini, not cheaper. It only "
            "matches gpt-4o-mini off-peak, and only beats it decisively on cache "
            "hits ($0.003 off-peak vs $0.075) — a 25x gap on cached input.",
            "1M context, up to 384K output. Native vision (JPEG/PNG/GIF/WebP). "
            "No video.",
            "MIT weights. 2,500 concurrent connections per account; above that "
            "the API returns HTTP 429.",
        ),
    ),

    # ── Groq gpt-oss-120b — FREE CORE CANDIDATE (CP10-CP12) ──────────────
    # Free-tier limits confirmed live 2026-09-19 (console.groq.com/docs/rate-limits):
    # 30 RPM / 1,000 RPD / 8,000 TPM / 200,000 TPD — identical to the ACTIVE
    # groq-llama4-scout (gpt-oss-20b) entry in providers.py despite the 6x parameter
    # difference; tracked per model, not per account.
    #
    # Measured against the full 32-task text suite (CP11, 2 repeats, both arms):
    # overall quality 0.855 control / 0.885 policy — below gpt-4o-mini (0.888/0.927)
    # and mistral-small (0.884/0.921) on the same categories, but ahead of
    # deepseek-flash (0.819/0.880) at zero cost instead of 2.33x gpt-4o-mini's.
    # 2.3% call-level error rate from a real, reproducible Groq-side quirk: the
    # model can attempt a native tool call with none declared, and Groq's own
    # validator then rejects the model's OWN response with HTTP 400 ("Tool choice
    # is none, but model called a tool") — fixed structurally, for both this model
    # and gpt-oss-20b, in src/local_runtime/groq_candidate.py (CP12): that module
    # NEVER omits `tools`, sending an explicit empty list + tool_choice="none"
    # when none are wanted. Not a routing change — this LAB entry documents the
    # facts; the isolated candidate module is what a caller actually reaches, and
    # only when GROQ_OSS_120B_ENABLED=true (default false, dev/test only).
    "groq-oss-120b": LabModel(
        key="groq-oss-120b",
        provider="groq",
        model_id="groq/openai/gpt-oss-120b",
        api_base="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        context_window=131_072,
        # Paid (Developer-tier) reference price, for if-metered comparison only —
        # this candidate is reached solely within the free tier; see
        # src/local_runtime/groq_candidate.py's FreeTierGuard.
        cost_per_1m_input_usd=0.15,
        cost_per_1m_output_usd=0.60,
        supports_tools=True,
        supports_structured_output=True,
        supports_vision=False,
        supports_video=False,
        supports_reasoning_effort=False,
        weights_license="Apache-2.0",
        weights_public=True,
        commercial_use=True,
        notes=(
            "FREE CORE CANDIDATE, not FREE CORE: 2.3% error rate on the full 32-task "
            "suite (CP11), all from the tool-call quirk above; overall quality below "
            "gpt-4o-mini/mistral-small. Strongest categories: chat, coding, "
            "long_context, structured, tool all 1.000; weakest: uncertainty 0.477 "
            "(every model measured so far fails this class), research 0.833.",
            "Free tier is a quota, not capacity: 200K tokens/day measured consuming "
            "~82% in one CP11 validation pass (128 calls, full category coverage, "
            "2 repeats) — real production volume would exhaust it fast.",
            "No automatic paid fallback exists for this candidate by design (CP12): "
            "a caller sees a controlled GroqCandidateError, never a silent handoff "
            "to gpt-4o-mini/mistral-small/claude-opus.",
        ),
    ),

    # ── Local: llama.cpp server on this machine (llm.local) ──────────────
    # Not an API: an OpenAI-compatible llama-server bound to 127.0.0.1. Kept
    # here, not in PROVIDER_CATALOG, for the same structural reason as every
    # other candidate: routing cannot reach it. The Core side is
    # src/local_runtime/provider.py (LOCAL_LLM_ENABLED=false by default).
    #
    # Zero per-token price is NOT zero cost: the machine's CPU/RAM time is the
    # price, and it is paid whether or not the answer is any good.
    "local-qwen2.5-0.5b": LabModel(
        key="local-qwen2.5-0.5b",
        provider="local-llamacpp",
        model_id="local/qwen2.5-0.5b-instruct-q4_k_m",
        api_base="http://127.0.0.1:8181/v1",
        api_key_env="",
        context_window=4096,
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        supports_tools=False,              # not validated
        supports_structured_output=False,  # not validated
        weights_license="Apache-2.0",
        weights_public=True,
        commercial_use=True,
        notes=(
            "Plumbing validation only. Qwen2.5-0.5B-Instruct Q4_K_M (491 MB) on llama.cpp v0.4.1 "
            "(b10964), CPU: ~206 t/s prompt, ~57 t/s generation, ~530 MB RAM on an i7-1255U.",
            "Quality insufficient for anything a person reads: failed a one-word translation on "
            "2026-09-18. Not for reasoning, code, tools, facts or billed answers.",
            "Loopback only. No credential exists or is read.",
        ),
    ),
}


def lab_model(key: str) -> LabModel:
    try:
        return LAB_CATALOG[key]
    except KeyError:
        raise ValueError(
            f"Unknown lab model {key!r}; known: {sorted(LAB_CATALOG)}"
        ) from None


def lab_keys() -> tuple[str, ...]:
    return tuple(sorted(LAB_CATALOG))
