"""Etapa 5H — operational provider/model kill switch
(RoutingRequest.disabled_provider_keys), integrated into the EXISTING
chain-building/failover infrastructure (_build_chain()), never a second
routing implementation.

Covers: enabled (default, empty set) -> unaffected; disabled primary
candidate -> existing failover selects the next candidate; ALL candidates
disabled -> ProviderUnavailableError (same terminal exception as
exhausting a real chain); a disabled provider's litellm.acompletion is
NEVER called (proven by a spy, not just by asserting the return value);
forced_provider_key (Continuity) pinned to a disabled key -> empty chain,
ProviderUnavailableError, never calls that provider either; circuit
breaker/budget checks remain independent of (unaffected by) this switch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trinity_router import ModelRouter, ProviderUnavailableError, RoutingRequest, TaskType
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.selector import select_provider_chain


def _response(input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


def _primary_chain_keys(task_type=TaskType.CHAT, user_plan="start"):
    return select_provider_chain(task_type, user_plan, None)


# ═════════════════════════════════════════════════════════════════════════
# _build_chain() — unit-level, no litellm involved
# ═════════════════════════════════════════════════════════════════════════

def test_disabled_provider_keys_default_is_empty_and_unaffected():
    router = ModelRouter()
    from trinity_router.limits import get_plan_limits
    plan_limits = get_plan_limits("start")
    chain_normal = router._build_chain(TaskType.CHAT, "start", plan_limits)
    chain_explicit_empty = router._build_chain(TaskType.CHAT, "start", plan_limits, disabled_provider_keys=frozenset())
    assert chain_normal == chain_explicit_empty


def test_disabling_the_primary_candidate_excludes_it_from_the_chain():
    router = ModelRouter()
    from trinity_router.limits import get_plan_limits
    plan_limits = get_plan_limits("start")
    keys = _primary_chain_keys()
    assert len(keys) >= 2, "test needs a plan with at least 2 failover candidates"
    primary = keys[0]

    chain = router._build_chain(TaskType.CHAT, "start", plan_limits, disabled_provider_keys=frozenset({primary}))
    chain_keys = [k for k, _ in chain]
    assert primary not in chain_keys
    assert len(chain) == len(keys) - 1


def test_disabling_every_candidate_returns_an_empty_chain():
    router = ModelRouter()
    from trinity_router.limits import get_plan_limits
    plan_limits = get_plan_limits("start")
    keys = _primary_chain_keys()

    chain = router._build_chain(TaskType.CHAT, "start", plan_limits, disabled_provider_keys=frozenset(keys))
    assert chain == []


def test_forced_provider_key_that_is_disabled_returns_empty_chain():
    router = ModelRouter()
    from trinity_router.limits import get_plan_limits
    plan_limits = get_plan_limits("start")
    some_key = next(iter(PROVIDER_CATALOG))

    chain = router._build_chain(
        TaskType.CHAT, "start", plan_limits,
        forced_provider_key=some_key, disabled_provider_keys=frozenset({some_key}),
    )
    assert chain == []


def test_forced_provider_key_not_disabled_still_works():
    router = ModelRouter()
    from trinity_router.limits import get_plan_limits
    plan_limits = get_plan_limits("start")
    some_key = next(iter(PROVIDER_CATALOG))
    other_key = next(k for k in PROVIDER_CATALOG if k != some_key)

    chain = router._build_chain(
        TaskType.CHAT, "start", plan_limits,
        forced_provider_key=some_key, disabled_provider_keys=frozenset({other_key}),
    )
    assert chain == [(some_key, PROVIDER_CATALOG[some_key])]


# ═════════════════════════════════════════════════════════════════════════
# Full complete()/stream() calls — proves the disabled provider is never
# actually invoked, and that existing failover still works around it.
# ═════════════════════════════════════════════════════════════════════════

async def test_complete_fails_over_past_a_disabled_primary(monkeypatch):
    """Adaptive Intelligence Router V2: the real chain is built from
    GLOBAL candidate discovery, not select_provider_chain()'s 2-entry
    curated list — so disabling the legacy primary (groq-llama4-scout)
    fails over to whatever is the cheapest REMAINING candidate across the
    WHOLE catalog (gpt-4o-mini, for this trivial START-plan request), not
    necessarily select_provider_chain()'s own curated secondary."""
    keys = _primary_chain_keys()
    assert len(keys) >= 2
    primary = keys[0]
    secondary_key = "gpt-4o-mini"  # cheapest globally-eligible runner-up once groq is disabled

    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        if model == PROVIDER_CATALOG[primary].model_id:
            raise AssertionError("disabled provider must never be called")
        return _response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    result = await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
        user_plan="start", session_id="s1",
        disabled_provider_keys=frozenset({primary}),
    ))
    assert result is not None
    assert PROVIDER_CATALOG[primary].model_id not in calls
    assert PROVIDER_CATALOG[secondary_key].model_id in calls


async def test_complete_raises_provider_unavailable_when_all_disabled(monkeypatch):
    """V2: disabling just the 2 LEGACY curated keys is no longer enough
    to exhaust the chain — global discovery would still find the other 6
    catalog models. Disable the ENTIRE catalog to genuinely test "nothing
    left" under global discovery (a real bug this etapa's own regression
    run caught: an earlier draft short-circuited on an empty legacy pool
    and skipped global discovery entirely, which would have made THIS
    exact scenario silently succeed via an undisabled global candidate)."""
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(return_value=_response()))
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
            user_plan="start", session_id="s1",
            disabled_provider_keys=frozenset(PROVIDER_CATALOG.keys()),
        ))


async def test_forced_disabled_provider_never_called_and_raises(monkeypatch):
    some_key = next(iter(PROVIDER_CATALOG))
    acompletion_spy = AsyncMock(return_value=_response())
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
            user_plan="start", session_id="s1",
            forced_provider_key=some_key, disabled_provider_keys=frozenset({some_key}),
        ))
    acompletion_spy.assert_not_called()


async def test_circuit_breaker_still_applies_independently_of_disabled_keys(monkeypatch):
    """A key that's circuit-open and every OTHER key operationally
    disabled are two independent skip reasons — when they cover the WHOLE
    chain between them, the call still correctly exhausts to
    ProviderUnavailableError (proving neither mechanism silently
    "cancels out" or bypasses the other). V2: global candidate discovery
    means "the whole chain" is now the ENTIRE catalog, not just
    select_provider_chain()'s 2-entry curated list — so every OTHER
    catalog key is explicitly disabled here to genuinely narrow the
    global universe down to just `primary`, exactly like the pre-V2 test
    narrowed it down implicitly via the 2-entry curated chain."""
    primary = _primary_chain_keys()[0]
    other_keys = frozenset(PROVIDER_CATALOG.keys()) - {primary}

    acompletion_spy = AsyncMock(return_value=_response())
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter(circuit_breaker_failure_threshold=1)
    router._circuit_breakers.record_failure(primary)  # open the breaker for `primary`
    assert not router._circuit_breakers.is_available(primary)

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
            user_plan="start", session_id="s1",
            disabled_provider_keys=other_keys,  # disable every OTHER candidate
        ))
    acompletion_spy.assert_not_called()  # no candidate was ever actually called
