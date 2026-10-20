# Adaptive Intelligence Router

Reference for `packages/model-router` (`trinity_router`). Everything here is
read off the code in that package — where a statement is enforced by a test,
the test file is named so the claim can be checked rather than trusted.

The router's job is one sentence: **pick the cheapest model that is actually
capable of this request, under this plan, and recover when the answer comes
back unusable.** It is not a fixed `model A → model B → model C` fallback
list, and it must not be turned into one.

---

## 1. The selection pipeline

Every call to `ModelRouter.complete()` / `.stream()` resolves a bounded,
ordered **chain of candidates** before any provider is contacted:

```
user input
  → capability            (Core: src/capability/classifier.py)
  → TaskType              (chat | agent — selector.py)
  → complexity score      (complexity.py, pure regex/heuristics, no LLM call)
  → required quality class max(complexity floor, plan floor)
  → eligible models       (providers.py: capability + tools + context window)
  → cost ranking          (rank_by_cost → cheapest capable first)
  → plan limits           (limits.py: token cap, cost cap, failover budget)
  → provider health       (circuit_breaker.py)
  → chain [(key, cfg), …]
```

Two properties matter and are tested:

- **The catalog is the source of selection.** Candidates are discovered from
  `PROVIDER_CATALOG`, filtered by real capability requirements, then ordered
  by real cost — not read from a hardcoded per-plan list.
  (`test_global_candidate_discovery.py`, `test_cost_aware_selection.py`,
  `test_model_eligibility.py`)
- **A quality floor can only ever be raised, never lowered.** The effective
  floor is `max(complexity.required_quality_class, plan_limits.min_quality_class)`.
  (`test_dynamic_routing_decisions.py`, `test_quality_entitlement_gate.py`)

When no model satisfies the requirements, the chain is built **empty** and the
request fails in a controlled way — the router never silently downgrades into
an incapable model. (`test_capability_gate_hardening.py`,
`test_capability_chain_invariants.py`)

`selector.py` still holds static chains; they are the fallback path and the
economic reference, not the primary mechanism.

---

## 2. Failover is bounded by the plan

`PlanLimits.max_failover_attempts` (`limits.py`) is the entire retry budget for
one request. Candidates tried = `max_failover_attempts + 1`.

| Plan  | Candidates | Quality floor | Quality ceiling | Max output tokens | Max est. cost/req |
|-------|-----------|---------------|-----------------|-------------------|-------------------|
| free  | 1         | ultra_low     | low             | see `limits.py`   | see `limits.py`   |
| start | 2         | ultra_low     | standard        | "                 | "                 |
| pro   | 3         | low           | premium         | "                 | "                 |
| max   | 4         | low           | premium         | "                 | "                 |
| team  | 4         | low           | premium         | "                 | "                 |

An unknown plan falls back to `start`. The numeric caps are deliberately not
duplicated here — `PLAN_LIMITS` in `limits.py` is the single source of truth.

**The plan differentiator is economics, not a pinned model**: budget, floor,
ceiling, failover width and entitlements. A plan is never "plan X = model Y".

---

## 3. Failure handling: two independent signals

### 3.1 Exception failures (pre-existing)

A provider call that raises is classified by `retry.py::classify_failure()`:

| Class | Behavior |
|-------|----------|
| `RETRYABLE` | exponential backoff, circuit breaker records a failure, next candidate |
| `NON_RETRYABLE_REQUEST` | raises `RequestRejectedByProviderError` immediately — no failover |
| other | circuit breaker records a failure, next candidate, no backoff |

`circuit_breaker.py` is per provider/model key (`failure_threshold=3`,
`cooldown_s=30`), never global.

### 3.2 Quality Recovery Layer (content failures)

An HTTP 200 whose **content** is unusable raises no exception, so none of the
above ever saw it. `quality_gate.py` is a pure, synchronous, no-LLM-call
evaluator that gives the router a second signal. It judges three things, in
this order:

| Reason | Meaning |
|--------|---------|
| `empty_response` | content is `None`, `""`, or whitespace only |
| `unexpected_refusal` | boilerplate safety refusal to a request that does **not** look sensitive |
| `language_mismatch` | user wrote in one of EN/PT/ES, the answer came back in a different one |

**Safety invariant — a legitimate refusal is always respected.** A refusal is
only treated as a failure when the user's own request does *not* match a
coarse sensitive-topic heuristic (self-harm, weapons, malware, illegal drugs,
child exploitation, extremist content). A request that already looks sensitive
is never touched and its refusal is returned as-is. The gate therefore cannot
make an unsafe request more likely to succeed.
(`test_quality_gate.py::test_legitimate_refusal_to_sensitive_request_is_respected`)

**Tool-call carve-out.** A response carrying `tool_calls` passes
unconditionally — not judged for emptiness, refusal, or language. A tool-call
turn legitimately has no prose, and recovering from one would throw away tool
calls the caller already holds.
(`test_quality_recovery.py::test_pure_tool_call_turn_costs_exactly_one_call`)

**Language detection is conservative.** EN/PT/ES stopword scoring only;
"unknown" on either side (short text, no hits, or a tie) suppresses the check
entirely, so ambiguity never triggers a recovery.

### 3.3 What recovery actually does

A quality rejection is a **soft** failure, and is handled as one:

- the provider's circuit breaker records a **success** — it answered fine at
  the HTTP level and must not be punished for this router's quality bar;
- **no backoff** is applied — waiting has no reason to help;
- the **same bounded chain** moves to its next candidate. No second router, no
  separate recovery chain, no extra attempts beyond `max_failover_attempts`.

(`test_quality_recovery.py::test_quality_recovery_never_trips_circuit_breaker`,
`::test_quality_recovery_applies_no_backoff_delay`)

### 3.4 Fail-open

If the chain runs out — every candidate rejected, or the remaining candidates
skipped because their circuit is open or the request exceeds their budget —
the router returns **the last response it actually obtained**, tagged
`failover_reason = "quality_gate_exhausted:<reason>"`. It does not raise
`ProviderUnavailableError` over an answer it already has.

`ProviderUnavailableError` is reserved for a genuine outage: no candidate ever
produced a response.
(`test_quality_recovery.py::test_all_candidates_fail_quality_returns_last_answer_instead_of_erroring`,
`::test_quality_rejection_still_answers_when_remaining_candidates_are_unavailable`,
`::test_real_provider_outage_still_raises`)

### 3.5 Streaming is observability-only, by design

`stream()` runs the same gate but **never acts on it**. Once
`litellm.acompletion(stream=True)` has returned, this method's contract is
that no failover happens again — retrying would duplicate or corrupt content
already sent to the client. A failed gate on this path increments
`router_quality_gate_flagged_total` and is tagged in the audit trail; that is
all. (`test_router_streaming_resilience.py`)

This asymmetry is deliberate. Do not "fix" it for symmetry.

---

## 4. Observability

Per-attempt structured audit events (`audit.py`) carry provider, model,
status, `failover_reason`, `failure_class`, latency, cost and TCU, correlated
by `logical_request_id`. Counters (`metrics.py`) include:

| Counter | Meaning |
|---------|---------|
| `router_provider_attempts_total` | labeled by outcome, incl. `quality_rejected` |
| `router_quality_recovery_total` | responses rejected and retried, by provider/model/reason |
| `router_quality_gate_flagged_total` | streamed responses that would have been rejected |
| `router_circuit_breaker_transitions_total` | breaker state changes |
| `router_chain_exhausted_total` | real outages |

Audit events and metrics never record prompt or response text.

Both registries are merged by Core's `/health/metrics` (**admin-only**).

---

## 5. Providers

`providers.py` holds the catalog: Anthropic, OpenAI, Google/Gemini, Groq, xAI,
Mistral. A provider whose API key is absent is **not deleted and not
disabled** — its candidates simply fail at call time and the chain recovers,
or they are excluded at eligibility time. Missing credentials degrade
gracefully; they never require a code change.

Live per-provider availability tests (`test_groq_model_availability.py`,
`test_mistral_model_availability.py`, `test_xai_model_availability.py`) skip
themselves when the corresponding key is unset.

---

## 6. Local development gotcha

`packages/core` depends on `packages/model-router` as a **non-editable path
dependency**. Editing the router source does **not** update Core's virtualenv,
and plain `uv sync` will not notice either — the version is unchanged. After
touching `packages/model-router`, run:

```bash
cd packages/core
uv sync --dev --reinstall-package trinity-model-router
```

CI and the production Docker build are unaffected: both start from a clean
checkout with no pre-existing virtualenv.

---

## 7. Test suite

```bash
cd packages/model-router
.venv/Scripts/python.exe -m pytest -q      # Windows
python -m pytest -q                        # POSIX
```

Live provider-availability tests skip without their API key.
