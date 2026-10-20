"""
Provider catalog — all model configurations, no API keys.

API keys are read by litellm from environment variables at call time.
This module only stores model metadata (costs, limits, capabilities).

All prices validated against litellm.model_cost on 2026-08-20, except
gemini-25-flash-lite (re-validated 2026-09-03 after Google retired
gemini-2.5-flash-lite). See that entry's own comment below.

RETIRED_MODEL_IDS at the bottom of this module is the guard that keeps a
retired id from coming back: litellm.model_cost still lists both of the
above, so the presence of a price is NOT evidence a model can be called.
context_window = max INPUT tokens (from litellm max_input_tokens).

Economic tiers (input / output cost per 1M tokens):
  ULTRA_LOW  groq-llama4-scout   $0.075 / $0.30 — CHAT only; GPT-OSS 20B on Groq; 131K ctx
  LOW        gpt-4o-mini          $0.10 / $0.50  — CHAT + AGENT; now gpt-6-luna; 922K ctx
  LOW        mistral-small        $0.15 / $0.60  — CHAT + AGENT (primary for capability="agent"); 262K ctx; tools OK
  LOW        gemini-25-flash-lite $0.30 / $2.50  — CHAT + files; 1M ctx; tools OK
  STANDARD   claude-haiku         $1.00 / $5.00  — (reserved for intent classifier)
  STANDARD   xai-grok             $1.25 / $2.50  — CHAT; in catalog only, not yet in any chain
  STANDARD   claude-sonnet        $2.00 / $10.00 — primary AGENT + complex CHAT
  STANDARD   gpt-4o               $2.00 / $10.00 — PRO fallback; now gpt-6.1-sol
  PREMIUM    claude-opus          $5.00 / $25.00 — MAX primary

Privacy:
  Groq: does NOT use API data for training.
  Google Gemini paid API: does NOT use data for training.
  OpenAI API: does NOT use API data for training by default.
  Anthropic API: does NOT use API data for training.
  NOT in catalog: OpenRouter free models (often used for training); Gemini free tier
  (Google may use free-tier data for training).

Adaptive Intelligence Router — V1 scope note: get_eligible_models()/
rank_by_cost()/select_cheapest_capable() below only ever see whatever
subset of THIS catalog router.py's curated selector.select_provider_chain()
already picked for a given (task_type, plan, capability) — never the
full catalog. "Cheapest capable" is therefore cheapest capable WITHIN
that bounded, hand-curated pool, not a global comparison across every
provider/model in existence.
"""
from __future__ import annotations

from dataclasses import dataclass

# Ordered weakest -> strongest. Used only to compare two quality_class values
# (e.g. select_cheapest_capable's min_quality_class filter) — never to
# compute a price or drive routing on its own.
QUALITY_CLASS_ORDER: tuple[str, ...] = ("ultra_low", "low", "standard", "premium")


@dataclass(frozen=True)
class ProviderModel:
    """Immutable descriptor for one model available from a provider.

    Never contains API keys, auth tokens, or any secret material.
    Priority: lower value = preferred when multiple providers could serve the request.

    quality_class/speed_class (Etapa 5E): coarse, human-assigned tiers —
    quality_class mirrors the "Economic tiers" already documented in this
    module's and selector.py's docstrings (ULTRA_LOW/LOW/STANDARD/PREMIUM),
    now formalized as a field so select_cheapest_capable() below can filter
    by a minimum quality bar. speed_class is a reasonable default based on
    each model's known general latency characteristics (e.g. Groq's LPU
    inference is exceptionally fast) — NOT a measured, continuously-updated
    benchmark; treat it as a coarse hint only.

    Adaptive Intelligence Router additions — capability profile fields.
    Populated ONLY with defensible, documented values (a real, named model
    family's known relative characteristics); anything not independently
    verifiable for THIS deployment defaults to an explicit "unknown"/False
    rather than being invented (mission requirement: "não inventar
    capacidade de provider" — e.g. never claim computer-use support just
    because a provider sells a computer-use PRODUCT elsewhere; this router
    never wires up a computer-use tool schema, so every model defaults
    False here regardless of what the underlying provider otherwise
    offers). See each catalog entry below for the specific reasoning per
    model.
    reasoning_level/coding_level: "unknown" | "low" | "medium" | "high" —
      coarse, human-assigned; not a benchmarked score.
    supported_task_types/supported_capabilities: what this model is
      MECHANICALLY able to do (any litellm text-completion call, optionally
      with tool-calling) — NOT a statement of which task_types/capabilities
      this router currently CHOOSES to use it for (that remains
      selector.py's curated chains, a separate, deliberate reliability/
      business decision layer this field must never bypass).
    supports_agent: real technical tool-calling ability (mirrors
      supports_tools — genuine agentic use needs reliable tool-calling,
      which is exactly what that flag already encodes).
    supports_reasoning: whether this router wires up a distinct extended-
      reasoning/thinking-mode parameter for this model. It does not today
      for ANY model (no such parameter is passed in router.py's
      litellm.acompletion() calls) — always False until that changes.
    supports_computer_use: whether this router wires up a computer-use
      tool schema for this model. It does not today for ANY model —
      always False regardless of what the provider sells as a separate
      product.
    """
    provider: str                    # "anthropic" | "openai" | "google" | "groq"
    model_id: str                    # litellm model string, e.g. "claude-sonnet-5"
    context_window: int              # max input tokens (from litellm max_input_tokens)
    cost_per_1m_input_usd: float
    cost_per_1m_output_usd: float
    #: Price of an input token the provider served from its PROMPT CACHE.
    #:
    #: None means "not verified for this model" — never "no discount". The
    #: distinction is load-bearing: cost feeds TCU, TCU spends a user's
    #: quota, so assuming a discount that does not exist would under-bill,
    #: and assuming full price where a discount does exist over-bills. Only
    #: a model whose cache hit was OBSERVED against the live API carries a
    #: number here; see each entry's own comment for the measurement.
    #:
    #: This is a pricing fact about an existing model, not a routing input.
    #: rank_by_cost() deliberately does NOT read it — ranking must stay
    #: predictable rather than depend on whether a prefix happens to be warm
    #: at the moment the chain is built.
    cost_per_1m_cached_input_usd: float | None = None
    #: Some providers bill EVERY token of a request at a higher rate once the
    #: prompt reaches a size threshold. Official xAI pricing for
    #: grok-4.20-0309-non-reasoning (docs.x.ai, read 2026-09-13): prompts of
    #: 200k tokens or more bill input, cached input and output at 2x. A
    #: catalog that ignores the tier under-bills exactly the long-context
    #: requests (files, research) that cost the most, so TCU would stop
    #: bounding real spend. None = no tier for this model.
    long_context_threshold_tokens: int | None = None
    long_context_price_multiplier: float = 1.0
    supports_tools: bool = True
    supports_streaming: bool = True
    #: Extra litellm.acompletion() keyword arguments this model needs on every
    #: call, as ((name, value), ...) — a tuple of pairs for the same reason as
    #: capability_priority below. Empty for almost every model; see the
    #: gpt-4o-mini entry for the one that needs it.
    call_params: tuple[tuple[str, str], ...] = ()
    priority: int = 0                # lower = preferred
    #: Per-capability overrides of `priority`, as ((capability, priority), ...).
    #:
    #: `priority` is global, and it only ever acts as the tiebreak between
    #: candidates of EQUAL blended cost (see rank_by_cost). That global tiebreak
    #: cannot express "this model is better at one specific kind of work",
    #: which is exactly what measurement produces: gpt-4o-mini and
    #: mistral-small are identically priced and identically classed, so one
    #: integer silently decides every tie for every capability at once.
    #:
    #: A tuple of pairs rather than a dict because this dataclass is frozen;
    #: a dict field would make it unhashable. Empty for every model that has
    #: no measured per-capability difference, which is most of them.
    capability_priority: tuple[tuple[str, int], ...] = ()
    modality: str = "text"           # "text" | "image" | "video" | "audio"
    quality_class: str = "standard"  # "ultra_low" | "low" | "standard" | "premium"
    speed_class: str = "standard"    # "fast" | "standard" | "slow"
    reasoning_level: str = "unknown"      # "unknown" | "low" | "medium" | "high"
    coding_level: str = "unknown"         # "unknown" | "low" | "medium" | "high"
    supports_agent: bool = False
    supports_reasoning: bool = False
    supports_computer_use: bool = False
    supported_task_types: frozenset[str] = frozenset()
    supported_capabilities: frozenset[str] = frozenset()

    def priority_for(self, capability: str | None) -> int:
        """The tiebreak priority for this capability, falling back to the
        global one. Never consulted unless blended cost is already equal."""
        if capability:
            for cap, prio in self.capability_priority:
                if cap == capability:
                    return prio
        return self.priority


# Every model in this catalog is a general-purpose text/chat model reached
# via a plain litellm.acompletion() call (see router.py) — mechanically
# capable of any TEXT-executor capability (capability_router.py maps
# CHAT/RESEARCH/CODE/WORK/FILES/AUTOMATION/AGENT all to ExecutorKind.TEXT;
# IMAGE/VIDEO/AUDIO are separate, unimplemented executors no catalog entry
# here claims). This is a mechanical fact about what a text-completion
# call can be used for, NOT a statement that selector.py's curated chains
# currently route every capability to every model — that remains a
# separate, deliberate business/reliability decision layer.
_TEXT_CAPABILITIES: frozenset[str] = frozenset({
    "chat", "research", "code", "work", "files", "automation", "agent",
})
_TEXT_TASK_TYPES: frozenset[str] = frozenset({"chat", "agent"})


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------
# Keys are internal aliases used by the selector — never exposed to clients.
# litellm reads the actual API keys from environment variables:
#   ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY (set in .env)
#
# Prices sourced from litellm.model_cost (validated 2026-08-20).
# To update prices: edit this file and run `uv pip install --reinstall`.
# ---------------------------------------------------------------------------

PROVIDER_CATALOG: dict[str, ProviderModel] = {
    # ── Groq ───────────────────────────────────────────────────────────────
    # Privacy: Groq does NOT use API data for training.
    # Env var: GROQ_API_KEY (optional — router gracefully falls back if missing)
    # GPT-OSS 20B: OpenAI's smaller open-weight reasoning model, hosted on Groq.
    # Replaces the former meta-llama/llama-4-scout-17b-16e-instruct entry
    # (live-verified 2026-09-06: that model ID returns Groq error
    # code=model_not_found — "does not exist or you do not have access to
    # it" — for our actual account/project, confirmed via GET
    # /openai/v1/models against this project's GROQ_API_KEY, which does not
    # list it at all). gpt-oss-20b is confirmed present in that same models
    # list (active=true), live-tested successfully via litellm, and its
    # context_window/pricing/supported_features below are taken directly
    # from that endpoint's response for this model, not assumed from docs.
    # Kept out of AGENT chains — claude-sonnet remains the agent primary.
    "groq-llama4-scout": ProviderModel(
        provider="groq",
        model_id="groq/openai/gpt-oss-20b",
        context_window=131_072,
        cost_per_1m_input_usd=0.075,
        cost_per_1m_output_usd=0.30,
        supports_tools=True,
        priority=65,
        quality_class="ultra_low",
        speed_class="fast",  # Groq LPU inference — among the fastest available
        # 20B open-weight reasoning model; conservatively kept at the same
        # reasoning/coding levels as the model it replaces rather than
        # inflated on the strength of Groq's own "reasoning" feature flag
        # alone. supports_agent mirrors supports_tools (the actual
        # technical requirement, confirmed via supported_features=["tools",
        # ...] in the /v1/models response) — selector.py's own chains still
        # deliberately exclude it from AGENT for reliability, a separate
        # policy layer this field does not override.
        reasoning_level="low", coding_level="low", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    # ── Anthropic ──────────────────────────────────────────────────────────
    "claude-opus": ProviderModel(
        provider="anthropic",
        model_id="claude-opus-4-7",
        context_window=1_000_000,
        cost_per_1m_input_usd=5.0,
        cost_per_1m_output_usd=25.0,
        priority=5,
        quality_class="premium",
        speed_class="standard",
        reasoning_level="high", coding_level="high", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    "claude-sonnet": ProviderModel(
        provider="anthropic",
        model_id="claude-sonnet-5",
        context_window=1_000_000,
        cost_per_1m_input_usd=2.0,
        cost_per_1m_output_usd=10.0,
        priority=10,
        quality_class="standard",
        speed_class="standard",
        reasoning_level="high", coding_level="high", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    # claude-haiku: in catalog for future classifier use; not in routing chains.
    # Live-tested 2026-08-20: works, supports tools.
    "claude-haiku": ProviderModel(
        provider="anthropic",
        model_id="claude-haiku-4-5",
        context_window=200_000,
        cost_per_1m_input_usd=1.0,
        cost_per_1m_output_usd=5.0,
        priority=20,
        quality_class="standard",
        speed_class="fast",
        reasoning_level="medium", coding_level="medium", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    # ── OpenAI ─────────────────────────────────────────────────────────────
    # OpenAI shuts down gpt-4o, gpt-4o-mini, gpt-4.1 and gpt-4.1-mini on
    # 2027-01-14 (deprecations page, entry of 2026-10-14). Both aliases below
    # were moved to GPT-6 models on 2026-10-20, keeping the alias names the
    # same way groq-llama4-scout and gemini-25-flash-lite did: the keys are
    # stored in kill switches and runtime settings, the model behind them is
    # not. The retired ids are in RETIRED_MODEL_IDS.
    #
    # Both new ids need litellm >= 1.104.0. Live-verified 2026-10-20 that
    # 1.97.0 sends max_tokens (OpenAI answers 400 "use max_completion_tokens")
    # and rejects reasoning_effort for them; 1.104.0 handles both.
    "gpt-4o": ProviderModel(
        provider="openai",
        # gpt-6.1-sol: same STANDARD price class as gpt-4o ($2.00/$10.00 against
        # $2.50/$10.00). Live-verified 2026-10-20 through litellm: plain chat,
        # tool calling and streaming all work.
        model_id="gpt-6.1-sol",
        context_window=922_000,
        cost_per_1m_input_usd=2.0,
        cost_per_1m_output_usd=10.0,
        # Prompt caching MEASURED 2026-10-20 with a 1,829-token system prefix:
        # call 1 cached 0, calls 2-4 cached 1,812. Official rate $0.10, a
        # twentieth of the input price (gpt-4o's was half).
        cost_per_1m_cached_input_usd=0.10,
        priority=30,
        quality_class="standard",
        speed_class="standard",
        reasoning_level="medium", coding_level="medium", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    "gpt-4o-mini": ProviderModel(
        provider="openai",
        # gpt-6-luna: the replacement OpenAI names for all four retiring
        # models, and cheaper than gpt-4o-mini was ($0.10/$0.50 against
        # $0.15/$0.60).
        model_id="gpt-6-luna",
        context_window=922_000,
        cost_per_1m_input_usd=0.10,
        cost_per_1m_output_usd=0.50,
        # Prompt caching MEASURED 2026-10-20 with a 1,829-token system prefix:
        # call 1 cached 0, calls 2-4 cached 1,812. Automatic, like gpt-4o-mini;
        # the official cached rate is a tenth of the input price.
        cost_per_1m_cached_input_usd=0.01,
        # Luna reasons at "medium" effort by default. Measured 2026-10-20 on
        # the same tool-calling request: 5.1 s at the default, 1.5 s at
        # "none", both returning the right tool call. "none" keeps this the
        # fast, non-reasoning economy model gpt-4o-mini was.
        call_params=(("reasoning_effort", "none"),),
        priority=40,
        quality_class="low",
        speed_class="fast",
        reasoning_level="low", coding_level="low", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    # ── Google ─────────────────────────────────────────────────────────────
    # gemini-1.5-*: removed — broken litellm entries or missing from model_cost.
    # gemini-2.0-flash-lite: DEPRECATED by Google — removed.
    # gemini-2.5-flash: retired by Google on 2026-09-11, exactly as
    # gemini-2.5-flash-lite was before it. Its successor gemini-3.6-flash was
    # carried here under the alias "gemini-25-flash" until 2026-09-13, when
    # the alias was removed as unreachable: at $0.75/$3.75 its blended cost
    # ranked 6th, chains hold at most 4 candidates, and its "low" class
    # excluded it whenever the floor rose to "standard" — so no dynamic chain
    # could ever contain it. The retired id stays in RETIRED_MODEL_IDS below;
    # removing the alias must not weaken the guard against reintroducing it.
    # gemini-3.5-flash-lite: gemini-2.5-flash-lite was retired by Google
    # ("no longer available to new users", confirmed via a live 404 from
    # generativelanguage.googleapis.com on 2026-09-03) -- replaced with its
    # named successor. Re-validated against litellm.model_cost on
    # 2026-09-03: max_input_tokens=1_048_576 (unchanged), tools=True
    # (unchanged), but pricing is now $0.30/$2.50 per 1M (was $0.10/$0.40)
    # -- the same tier as gemini-25-flash (non-lite), not ultra_low anymore.
    "gemini-25-flash-lite": ProviderModel(
        provider="google",
        model_id="gemini/gemini-3.5-flash-lite",
        context_window=1_048_576,
        cost_per_1m_input_usd=0.30,
        cost_per_1m_output_usd=2.50,
        supports_tools=True,
        priority=60,
        quality_class="low",
        reasoning_level="medium", coding_level="medium", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
        speed_class="fast",
    ),
    # ── xAI ────────────────────────────────────────────────────────────────
    # Env var: XAI_API_KEY. LiteLLM supports xAI natively (litellm_provider=
    # "xai", model_cost has full metadata for this exact id) — no custom
    # HTTP adapter needed, same as every other text provider in this catalog.
    # Live-verified 2026-09-07: direct HTTPS call to api.x.ai/v1 (bypassing
    # LiteLLM entirely) AND litellm.completion() both succeed with this
    # exact model id, confirmed via a real inference call, not just the
    # account's GET /v1/models listing it as available.
    # In catalog only for now, like claude-haiku above — not yet added to
    # any selector.py chain, so it does not change any plan's routing
    # behavior until that's a deliberate follow-up decision.
    "xai-grok": ProviderModel(
        provider="xai",
        model_id="xai/grok-4.20-0309-non-reasoning",
        context_window=1_000_000,
        cost_per_1m_input_usd=1.25,
        cost_per_1m_output_usd=2.50,
        # Cached input MEASURED 2026-09-13: xAI returns its own bill per call
        # (usage.cost_in_usd_ticks), and over 170 benchmark calls that bill
        # matched input $1.25 / cached $0.20 / output $2.50 exactly (ratio
        # 1.0000). Official pricing confirms $0.20. Without it every cache hit
        # was billed at the full $1.25.
        cost_per_1m_cached_input_usd=0.20,
        long_context_threshold_tokens=200_000,
        long_context_price_multiplier=2.0,
        supports_tools=True,
        priority=35,
        quality_class="standard",
        speed_class="standard",
        reasoning_level="medium", coding_level="medium", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
    # ── Mistral ────────────────────────────────────────────────────────────
    # Env var: MISTRAL_API_KEY. LiteLLM supports Mistral natively
    # (litellm_provider="mistral", model_cost has full metadata for this
    # exact id) — no custom HTTP adapter needed. Live-verified 2026-09-07:
    # direct HTTPS call to api.mistral.ai/v1 (bypassing LiteLLM) AND
    # litellm.completion() both succeed with this exact model id, confirmed
    # via real inference AND a real tool-calling round trip, not just the
    # account's GET /v1/models listing it as available.
    # Pricing identical to gpt-4o-mini ($0.15/$0.60) -> same LOW economic
    # tier and priority band, not the STANDARD tier of claude-haiku/xai-grok
    # above (Mistral's own "reasoning": true capability flag reflects
    # general model quality, which the catalog's reasoning_level below
    # already captures — it is NOT the same thing as this dataclass's
    # supports_reasoning field, which means something narrower: whether
    # router.py wires up a DISTINCT extended-reasoning/thinking-mode
    # parameter for a model. It does not, for any model today, including
    # this one — left at its default False; setting it True here without
    # actually wiring such a parameter would misrepresent what the router
    # does).
    # In catalog only for now, like claude-haiku/xai-grok above — not
    # added to any selector.py chain; V2's global candidate discovery
    # picks it up automatically wherever capability+quality tier qualify.
    "mistral-small": ProviderModel(
        provider="mistral",
        model_id="mistral/mistral-small-latest",
        context_window=262_144,
        cost_per_1m_input_usd=0.15,
        cost_per_1m_output_usd=0.60,
        supports_tools=True,
        # Prompt caching MEASURED 2026-09-12: call 1 cached 0, calls 2-4
        # cached 1072 of 1105 prompt tokens (97.0%). Automatic, like OpenAI.
        # The cached read is a TENTH of the standard input rate, not a half -
        # which is why billing the full prompt over-charged this entry 7.02x
        # on a cache hit, the largest error of any model in the catalog and
        # the reason this field exists. It lands on the primary for
        # capability="agent", so it was spending agent users' quota fastest.
        cost_per_1m_cached_input_usd=0.015,
        priority=45,  # tiebreak only: identical blended cost to gpt-4o-mini
        # (priority=40) at any input/output ratio, since both catalog
        # entries share the exact same $0.15/$0.60 pricing. gpt-4o-mini
        # wins that tie EVERYWHERE EXCEPT capability="agent" — see below.
        # Everywhere else mistral-small remains the very next pick after it.
        capability_priority=(("agent", 35),),
        # 2026-10-20: the tie below no longer exists. The gpt-4o-mini alias now
        # carries gpt-6-luna at $0.10/$0.50, cheaper than this entry, so cost
        # ranks it first for agent work too and this override only applies if
        # the two are ever priced the same again. The measurement it rests on
        # was against gpt-4o-mini, not luna; re-run the agent class of
        # bench/MODEL_COMPARISON.md before reading it as a statement about luna.
        # Measured, not assumed. bench/MODEL_COMPARISON.md: on the agent
        # task class mistral-small scored 0.917 against gpt-4o-mini's 0.879
        # (both with the System Policy), the best agent-class result of any
        # economy model, and the report notes that one task in that class
        # was graded on unfair phrasing in a way that UNDERSTATES mistral's
        # score. Same price, same quality_class, so nothing else in the
        # ranking moves: this only settles the tie the two of them are
        # already in, and only for agent work.
        #
        # Scoped to the CAPABILITY string, not to TaskType.AGENT, and that
        # distinction is the whole point. core's CAPABILITY_TASK_TYPE maps
        # code, research, files, work, automation and presentation onto
        # TaskType.AGENT as well, so keying on the task type would hand
        # coding and research to mistral-small too — which the same
        # benchmark does NOT support (those classes are at ceiling, where
        # it found no evidence to prefer either model, and gpt-4o-mini is
        # the stronger all-round economy model). 35 rather than 39 leaves
        # room to place another model between them later without renumbering.
        quality_class="low",
        speed_class="fast",
        reasoning_level="medium", coding_level="medium", supports_agent=True,
        supported_task_types=_TEXT_TASK_TYPES, supported_capabilities=_TEXT_CAPABILITIES,
    ),
}


# ---------------------------------------------------------------------------
# Global candidate discovery — Adaptive Intelligence Router V2.
#
# get_candidate_models() is the technical-universe discovery step: every
# catalog entry that could MECHANICALLY serve this capability/task_type
# and isn't operationally disabled — full stop. It deliberately does NOT
# consult selector.select_provider_chain() or any hand-curated list: that
# curated list remains available for legacy/fallback callers (see
# router.py), but no longer BOUNDS the candidate universe for the real
# adaptive path. "Cheapest capable" only means something if "capable" was
# judged against the real technical universe, not an arbitrarily-curated
# subset of it (mission: "não filtrar por posição histórica da chain").
#
# Three-stage pipeline this module now implements end to end:
#   1. Provider Catalog  — the technical universe (PROVIDER_CATALOG itself).
#   2. Eligibility        — which of those models CAN execute (this
#                            function, for capability/task_type/disabled;
#                            get_eligible_models() below, for quality/tools/
#                            context).
#   3. Selection           — which eligible candidate is cheapest
#                            (rank_by_cost() below).
# ---------------------------------------------------------------------------


def get_candidate_models(
    *,
    capability: str | None = None,
    task_type: str | None = None,
    disabled_provider_keys: frozenset[str] = frozenset(),
    catalog: dict[str, ProviderModel] | None = None,
) -> list[tuple[str, ProviderModel]]:
    """Return every (key, ProviderModel) pair in the catalog that is
    MECHANICALLY able to serve this capability/task_type and is not
    operationally disabled — the GLOBAL candidate universe, before any
    quality/cost/tool/context filtering (get_eligible_models()) or cost
    ranking (rank_by_cost()).

    Only objective, catalog-declared facts gate a candidate here:
    `supported_capabilities`/`supported_task_types` (what a model can
    mechanically do) and `disabled_provider_keys` (an operational kill
    switch). Provider identity, model name, or historical chain position
    play NO role — this is the whole point: a model that was never added
    to selector.py's curated lists (e.g. claude-haiku, reserved there for
    a future classifier — see PROVIDER_CATALOG's own comment on it) is
    still a legitimate candidate here if it's technically capable.

    `catalog` defaults to the real PROVIDER_CATALOG; overridable for
    tests that want a smaller, synthetic universe without monkeypatching
    the module-level catalog.
    """
    source = catalog if catalog is not None else PROVIDER_CATALOG
    return [
        (key, cfg) for key, cfg in source.items()
        if key not in disabled_provider_keys
        and (capability is None or capability in cfg.supported_capabilities)
        and (task_type is None or task_type in cfg.supported_task_types)
    ]


# ---------------------------------------------------------------------------
# Model eligibility + cost-aware selection — Adaptive Intelligence Router.
#
# get_eligible_models() is the single, shared eligibility filter (mission
# requirement: get_eligible_models(...) as a central function). Both
# select_cheapest_capable() (returns the single best candidate — the
# original Etapa 5E function, UNCHANGED behavior, now implemented on top
# of the shared filter) and rank_by_cost() (returns the full ordered
# chain — cheapest capable, next cheapest capable, ... — used by
# router.py to build a complexity-aware failover chain) are built on it.
#
# Pure and side-effect-free throughout this section: never mutates
# `candidates`, never performs I/O, never consults health/circuit-breaker
# state (that is availability, a separate axis router.py already applies
# before/independently of this — see that module's own docstring on why
# this stays a pure function, not a second health-tracking system).
# ---------------------------------------------------------------------------


def get_eligible_models(
    candidates: list[ProviderModel],
    *,
    min_context_window: int = 0,
    requires_tools: bool = False,
    requires_streaming: bool = False,
    min_quality_class: str | None = None,
) -> list[ProviderModel]:
    """Return every candidate that satisfies ALL stated requirements, in
    the SAME relative order as `candidates` (callers that care about cost
    ordering call rank_by_cost() on the result; callers that care about
    the original priority ordering can rely on this preserving it).

    A candidate that fails ANY requirement is excluded entirely, never
    merely deprioritized — this is what prevents "cheapest" from ever
    silently degrading into "cheapest incapable".

    Objective, static eligibility criteria only (context window, tools,
    streaming, quality floor) — does NOT check disabled-provider-key sets
    or circuit-breaker state. Those already have a correct, single home
    (router.py's chain-building and per-attempt loop respectively); this
    function does not duplicate them.
    """
    if min_quality_class is not None and min_quality_class not in QUALITY_CLASS_ORDER:
        raise ValueError(f"unknown quality_class: {min_quality_class!r}")
    min_rank = QUALITY_CLASS_ORDER.index(min_quality_class) if min_quality_class is not None else 0

    return [
        c for c in candidates
        if c.context_window >= min_context_window
        and (not requires_tools or c.supports_tools)
        and (not requires_streaming or c.supports_streaming)
        and QUALITY_CLASS_ORDER.index(c.quality_class) >= min_rank
    ]


def _blended_cost(
    c: ProviderModel, *, input_tokens_estimate: int, max_output_tokens_estimate: int,
) -> float:
    """Blended cost estimate — input+output, priced via each candidate's
    own cost_per_1m_*_usd. Not a single-dimension input-only or output-
    only comparison — a model with very cheap input but expensive output
    could otherwise look artificially cheap for output-heavy tasks."""
    return (
        (input_tokens_estimate / 1_000_000) * c.cost_per_1m_input_usd
        + (max_output_tokens_estimate / 1_000_000) * c.cost_per_1m_output_usd
    )


def rank_by_cost(
    candidates: list[ProviderModel],
    *,
    max_output_tokens_estimate: int = 4_096,
    input_tokens_estimate: int = 2_000,
    capability: str | None = None,
) -> list[ProviderModel]:
    """Sort candidates cheapest-first (blended cost), priority as the
    tiebreak for equal cost. Does NOT filter — callers pass an
    already-eligible list (see get_eligible_models()). This is the
    ordering `router.py` turns into a failover chain: cheapest capable
    first, next cheapest capable second, and so on.

    `capability` selects the per-capability tiebreak (ProviderModel.
    priority_for). It changes NOTHING about which models are eligible and
    nothing about cost — cost is compared first and wins outright, so a
    capability override can only ever reorder models that already cost the
    same. Omitting it reproduces the previous global-priority behaviour
    exactly.
    """
    return sorted(
        candidates,
        key=lambda c: (
            _blended_cost(
                c, input_tokens_estimate=input_tokens_estimate,
                max_output_tokens_estimate=max_output_tokens_estimate,
            ),
            c.priority_for(capability),
        ),
    )


def select_cheapest_capable(
    candidates: list[ProviderModel],
    *,
    min_context_window: int = 0,
    requires_tools: bool = False,
    requires_streaming: bool = False,
    min_quality_class: str | None = None,
    max_output_tokens_estimate: int = 4_096,
    input_tokens_estimate: int = 2_000,
) -> ProviderModel | None:
    """Return the cheapest candidate that satisfies every stated
    requirement, or None if no candidate qualifies. See get_eligible_models()
    and rank_by_cost(), which this is a thin composition of."""
    eligible = get_eligible_models(
        candidates,
        min_context_window=min_context_window,
        requires_tools=requires_tools,
        requires_streaming=requires_streaming,
        min_quality_class=min_quality_class,
    )
    if not eligible:
        return None
    ranked = rank_by_cost(
        eligible,
        max_output_tokens_estimate=max_output_tokens_estimate,
        input_tokens_estimate=input_tokens_estimate,
    )
    return ranked[0]


# ---------------------------------------------------------------------------
# Retired models — the guard against a dead id coming back
# ---------------------------------------------------------------------------
# Every id here was verified RETIRED against the live provider API: listed by
# the models endpoint, priced by litellm, and answering 404 "no longer
# available to new users" on an actual inference call. That combination is
# exactly why this list has to exist and be maintained by hand — neither the
# catalog listing nor the presence of a price tells you a model can be
# called, so there is nothing to derive this from automatically.
#
# tests/test_retired_models.py asserts no PROVIDER_CATALOG entry points at
# one of these. The failure mode it prevents is real and has now happened
# twice: a model is retired, calls start 404ing in production, and the entry
# looks perfectly healthy in code review because its price is still valid.
RETIRED_MODEL_IDS: frozenset[str] = frozenset({
    # Retired 2026-09-03. Replaced by gemini/gemini-3.5-flash-lite.
    "gemini/gemini-2.5-flash-lite",
    # Retired 2026-09-11. Google's own 404 names gemini-3.6-flash as the
    # successor; that is what the catalog now points at.
    "gemini/gemini-2.5-flash",
    # Documented as deprecated by Google and removed from the catalog before
    # either of the above — kept here so it cannot be reintroduced.
    "gemini/gemini-2.0-flash-lite",
    # OpenAI shutdown 2027-01-14 (deprecations entry of 2026-10-14). Moved off
    # on 2026-10-20, before the date, so they are listed ahead of the 404.
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
})
