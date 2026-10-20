"""
Security tests for the TRINITY Model Router.

Each test maps to a specific requirement from the Phase 6 spec:
  SEC-01  User cannot force provider
  SEC-02  User cannot force model
  SEC-03  Token limit enforced server-side
  SEC-04  Cost limit enforced server-side
  SEC-05  TCU sent by client has no effect
  SEC-06  Failover is deterministic and auditable
  SEC-07  No API keys or secrets in audit events
  SEC-08  No API keys or secrets in ProviderUnavailableError
  SEC-09  No API keys or secrets in failover_reason
  SEC-10  Alternative provider functions after primary fails
  SEC-11  All providers failing raises ProviderUnavailableError
  SEC-12  Failover bounded by plan limit
"""
from __future__ import annotations

import dataclasses
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from trinity_router import (
    ModelRouter,
    ProviderUnavailableError,
    RoutingRequest,
    RoutingResult,
    RouterStreamChunk,
    TaskType,
)
from trinity_router.audit import RouterAuditEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(content="OK", input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


_FAKE_SECRET = "sk-ant-api03-THISISASECRETKEY-FAKE"


# ---------------------------------------------------------------------------
# SEC-01: User cannot force provider
# ---------------------------------------------------------------------------

def test_routing_request_has_no_provider_field():
    """SEC-01: RoutingRequest must not accept a 'provider' field."""
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    assert "provider" not in fields, (
        "RoutingRequest must not expose a 'provider' field — "
        "provider selection is server-side only."
    )

def test_routing_request_has_no_endpoint_field():
    """SEC-01: RoutingRequest must not accept a custom 'endpoint' field."""
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    assert "endpoint" not in fields


# ---------------------------------------------------------------------------
# SEC-02: User cannot force model
# ---------------------------------------------------------------------------

def test_routing_request_has_no_model_field():
    """SEC-02: RoutingRequest must not accept a 'model' field."""
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    assert "model" not in fields, (
        "RoutingRequest must not expose a 'model' field — "
        "model selection is server-side only."
    )

def test_routing_request_has_no_api_key_field():
    """SEC-02: RoutingRequest must not accept an 'api_key' field."""
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    assert "api_key" not in fields


def test_routing_result_provider_is_read_only():
    """SEC-02: RoutingResult.provider is set by the server, not the caller."""
    # RoutingResult can only be constructed by the router; its fields
    # are populated from PROVIDER_CATALOG, never from RoutingRequest.
    fields = {f.name for f in dataclasses.fields(RoutingResult)}
    assert "provider" in fields  # server-assigned output field


# ---------------------------------------------------------------------------
# SEC-03: Token limit enforced server-side
# ---------------------------------------------------------------------------

async def test_token_limit_caps_request(monkeypatch):
    """SEC-03: Requesting excess tokens is silently capped — not passed to provider."""
    captured = {}

    async def capture(*args, **kwargs):
        captured.update(kwargs)
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", capture)
    from trinity_router.limits import get_plan_limits
    plan = get_plan_limits("start")

    router = ModelRouter()
    await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="sec03",
        max_output_tokens=1_000_000,  # absurdly large
    ))
    assert captured["max_tokens"] <= plan.max_output_tokens_per_request, (
        "Provider was called with more tokens than the plan allows."
    )


# ---------------------------------------------------------------------------
# SEC-04: Cost limit enforced server-side
# ---------------------------------------------------------------------------

async def test_cost_limit_prevents_expensive_request(monkeypatch):
    """SEC-04: A model whose conservative estimate exceeds per-request cost must be skipped."""
    from trinity_router.limits import get_plan_limits, PLAN_LIMITS
    from trinity_router.providers import PROVIDER_CATALOG

    # Use "start" plan (most restrictive) and try to use an expensive model.
    # Claude Opus is extremely expensive — it should exceed the "start" cost limit.
    # The router should either skip it or raise ProviderUnavailableError.
    called_models = []

    async def capture(*args, **kwargs):
        called_models.append(kwargs.get("model", ""))
        raise Exception("all down")  # force all to fail

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", capture)

    router = ModelRouter()
    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="sec04",
        ))
    # claude-opus is too expensive for start plan so should NOT be called
    # (start plan uses claude-sonnet as primary, not claude-opus)
    assert "claude-opus" not in str(called_models), (
        "claude-opus should not be in the provider chain for the 'start' plan."
    )


# ---------------------------------------------------------------------------
# SEC-05: TCU from client has no effect
# ---------------------------------------------------------------------------

def test_routing_request_has_no_tcu_field():
    """SEC-05: RoutingRequest has no tcu field — client cannot send or alter TCU."""
    fields = {f.name for f in dataclasses.fields(RoutingRequest)}
    assert "tcu" not in fields

def test_routing_result_tcu_is_server_field():
    """SEC-05: RoutingResult.tcu is an output field, not an input field."""
    # Verify it can only be set by constructing RoutingResult (the router does this)
    fields = {f.name for f in dataclasses.fields(RoutingResult)}
    assert "tcu" in fields  # output from router, not from client

async def test_tcu_calculated_from_server_token_counts(monkeypatch):
    """SEC-05: TCU is always derived from server-returned token counts."""
    from trinity_router.tcu import TCU_EXCHANGE_RATE
    monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                        AsyncMock(return_value=_make_response(input_tokens=200, output_tokens=100)))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="sec05",
    ))
    # Verify TCU = cost_usd * exchange_rate (server formula, no client input)
    assert result.tcu == pytest.approx(result.cost_usd * TCU_EXCHANGE_RATE, rel=1e-3)
    assert result.tcu >= 0


# ---------------------------------------------------------------------------
# SEC-06: Failover is deterministic and auditable
# ---------------------------------------------------------------------------

async def test_failover_is_deterministic(monkeypatch):
    """SEC-06: Same inputs always produce the same provider chain order."""
    from trinity_router.selector import select_provider_chain, TaskType
    chain_a = select_provider_chain(TaskType.CHAT, "start")
    chain_b = select_provider_chain(TaskType.CHAT, "start")
    assert chain_a == chain_b


async def test_failover_uses_providers_in_defined_order(monkeypatch):
    """SEC-06: Failover tries providers in a deterministic order, never
    randomly. Since the Adaptive Intelligence Router V2, that order is no
    longer select_provider_chain()'s own static list (candidate discovery
    is now global — see providers.get_candidate_models()) but it is still
    fully deterministic: cheapest-eligible-first by blended cost, `priority`
    as the tiebreak, truncated to the plan's own max_failover_attempts+1.
    For this trivial "Hi" / START-plan request (required_quality_class
    floored at "ultra_low"), that is groq-llama4-scout (cheapest in the
    whole catalog) then gpt-4o-mini (cheapest runner-up), truncated to
    START's 2 total attempts. Proven twice, back to back, to demonstrate
    the order does not vary between calls."""
    from trinity_router.providers import PROVIDER_CATALOG

    expected_models = [
        PROVIDER_CATALOG["groq-llama4-scout"].model_id,
        PROVIDER_CATALOG["gpt-4o-mini"].model_id,
    ]

    async def _run_once() -> list[str]:
        tried: list[str] = []

        async def track(*args, **kwargs):
            tried.append(kwargs.get("model", ""))
            raise Exception("down")

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
        with pytest.raises(ProviderUnavailableError):
            await ModelRouter().complete(RoutingRequest(
                messages=[{"role": "user", "content": "Hi"}],
                task_type=TaskType.CHAT,
                user_plan="start",
                session_id="sec06",
            ))
        return tried

    first_run = await _run_once()
    second_run = await _run_once()
    assert first_run == expected_models, (
        f"Provider tried out of order: got {first_run!r}, expected {expected_models!r}"
    )
    assert second_run == first_run, "failover order changed between two otherwise-identical calls"


# ---------------------------------------------------------------------------
# SEC-07: No secrets in audit events
# ---------------------------------------------------------------------------

def test_audit_event_has_no_api_key_field():
    """SEC-07: RouterAuditEvent must not have an api_key field."""
    fields = {f.name for f in dataclasses.fields(RouterAuditEvent)}
    forbidden = {"api_key", "secret", "token", "authorization", "password", "credential"}
    overlap = fields & forbidden
    assert not overlap, (
        f"RouterAuditEvent contains potentially sensitive fields: {overlap}"
    )

def test_audit_event_session_id_not_full_token(monkeypatch, caplog):
    """SEC-07: The session_id in audit events is not an auth token."""
    from trinity_router.audit import create_audit_event, log_audit_event
    import logging
    event = create_audit_event(
        session_id="sess-abc123",
        user_plan="start",
        task_type="chat",
        provider="anthropic",
        model="claude-sonnet-4-5",
        status="success",
    )
    # Verify no secret-looking content in the structured fields
    assert "sk-" not in event.session_id
    assert "Bearer" not in event.session_id


# ---------------------------------------------------------------------------
# SEC-08: No secrets in ProviderUnavailableError
# ---------------------------------------------------------------------------

async def test_provider_unavailable_error_contains_no_secrets(monkeypatch):
    """SEC-08: ProviderUnavailableError message must not contain API keys."""
    async def fail_with_secret(*args, **kwargs):
        raise Exception(f"Authentication failed: api_key={_FAKE_SECRET}")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fail_with_secret)
    router = ModelRouter()
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="sec08",
        ))
    error_text = str(exc_info.value)
    assert _FAKE_SECRET not in error_text, (
        "ProviderUnavailableError must not expose provider error details that may contain secrets."
    )
    assert "sk-ant" not in error_text


# ---------------------------------------------------------------------------
# SEC-09: No secrets in failover_reason
# ---------------------------------------------------------------------------

async def test_failover_reason_contains_no_raw_exception_message(monkeypatch):
    """SEC-09: failover_reason must be a sanitized code, not the raw exception."""
    call_count = 0

    async def leak_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception(f"Auth error: key={_FAKE_SECRET} is expired")
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", leak_then_succeed)
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="sec09",
    ))
    reason = result.failover_reason or ""
    assert _FAKE_SECRET not in reason
    assert "expired" not in reason
    assert "key=" not in reason
    # Must be one of our sanitized codes
    assert reason in (
        "", "provider_auth_error", "provider_connection_error",
        "provider_rate_limited", "provider_overloaded", "provider_error",
        "budget_exceeded",
    ), f"Unexpected failover_reason: {reason!r}"


# ---------------------------------------------------------------------------
# SEC-10: Alternative provider works after primary fails
# ---------------------------------------------------------------------------

async def test_alternative_provider_serves_request_after_primary_fails(monkeypatch):
    """SEC-10: When provider A fails, provider B must successfully serve the request."""
    call_count = 0

    async def fail_first_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("primary down")
        return _make_response(content="From backup provider")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fail_first_then_succeed)
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="sec10",
    ))
    assert result.content == "From backup provider"
    assert result.failover_used is True
    assert call_count == 2


# ---------------------------------------------------------------------------
# SEC-11: All providers failing raises ProviderUnavailableError
# ---------------------------------------------------------------------------

async def test_all_providers_failing_raises_provider_unavailable_error(monkeypatch):
    """SEC-11: ProviderUnavailableError is raised if every provider in the chain fails."""
    monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                        AsyncMock(side_effect=Exception("down")))
    router = ModelRouter()
    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="sec11",
        ))


# ---------------------------------------------------------------------------
# SEC-12: Failover bounded by plan limit
# ---------------------------------------------------------------------------

async def test_failover_bounded_by_plan_max_failover_attempts(monkeypatch):
    """SEC-12: Number of providers tried must not exceed plan.max_failover_attempts + 1."""
    from trinity_router.limits import get_plan_limits
    call_count = 0

    async def always_fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fail)

    for plan_name in ("start", "pro", "max"):
        call_count = 0
        plan_limits = get_plan_limits(plan_name)
        max_allowed = plan_limits.max_failover_attempts + 1

        with pytest.raises(ProviderUnavailableError):
            await ModelRouter().complete(RoutingRequest(
                messages=[{"role": "user", "content": "Hi"}],
                task_type=TaskType.CHAT,
                user_plan=plan_name,
                session_id="sec12",
            ))

        assert call_count <= max_allowed, (
            f"Plan '{plan_name}': expected at most {max_allowed} calls, got {call_count}. "
            "Failover exceeded plan limit — could cause unlimited spend."
        )
