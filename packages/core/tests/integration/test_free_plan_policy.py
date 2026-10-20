"""FREE plan policy closure (2026-09-07).

Proves, end to end through the real HTTP routes, that FREE is genuinely
the cheapest/most restricted plan:
  - min_quality_class="ultra_low" (corrected — was "low", which wrongly
    excluded groq-llama4-scout, the cheapest model in the catalog, from
    every FREE request).
  - max_quality_class="low" (corrected — was "standard"; FREE must never
    reach "standard"/"premium" even though "standard" is mathematically
    affordable at FREE's own cost ceiling — commercial policy is
    stricter than pure affordability).
  - Agent mode is now actually enforced (PlanEntitlement.supports_agent_
    mode=False for FREE was previously declared but never checked before
    dispatching to stream_agent()).
  - max_agents=0 is now actually enforced (agent_scheduler.py's
    check_plan_concurrency(), previously declared but never read).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.entitlements import get_plan_entitlement
from src.models.user import User
from trinity_router import free_pool
from trinity_router.limits import get_plan_limits


@pytest.fixture
def free_user(client, db):
    """Register a real user and set plan="free" directly (mirrors
    test_entitlement.py's plan_user fixture — not imported from there
    since fixtures are file-local, not in conftest.py)."""
    counter = {"n": 0}

    def _make():
        counter["n"] += 1
        email = f"freeuser{counter['n']}@test.com"
        resp = client.post("/auth/register", json={
            "email": email, "password": "TestPass123!", "full_name": "Free User",
        })
        assert resp.status_code == 201
        token = resp.json()["access_token"]
        user_id = resp.json()["user_id"]
        headers = {"Authorization": f"Bearer {token}"}

        user = db.query(User).filter(User.id == user_id).first()
        user.plan = "free"
        db.commit()

        sess_resp = client.post("/sessions", json={"title": "test"}, headers=headers)
        session_id = sess_resp.json()["id"]
        return headers, user_id, session_id

    return _make


# ── Values (report requirement: show current values before behavior tests) ──

class TestFreePlanValuesThemselves:
    def test_free_quality_floor_and_ceiling(self):
        free = get_plan_limits("free")
        assert free.min_quality_class == "ultra_low"
        assert free.max_quality_class == "low"

    def test_free_entitlement_flags(self):
        e = get_plan_entitlement("free")
        assert e.supports_agent_mode is False
        assert e.max_agents == 0
        assert e.max_tcu_per_request == 2.0
        assert e.daily_tcu_limit == 10.0

    def test_free_router_limits_unchanged_by_this_etapa(self):
        # This etapa touches only min/max_quality_class -- these numbers
        # were already correct and are explicitly NOT altered.
        free = get_plan_limits("free")
        assert free.max_output_tokens_per_request == 1_024
        assert free.max_estimated_cost_per_request_usd == 0.02
        assert free.max_failover_attempts == 0


# ── FREE + CHAT trivial -> cheapest eligible model in the WHOLE catalog ──

def test_free_chat_trivial_picks_cheapest_model_in_catalog(client, free_user):
    from trinity_router.providers import PROVIDER_CATALOG

    headers, _, session_id = free_user()
    captured: dict = {}

    async def _acompletion(*, model, **kwargs):
        captured["model"] = model

        class _StreamChunk:
            choices = [type("C", (), {"delta": type("D", (), {"content": "hi"})()})]
            usage = None

        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if captured.get("_yielded"):
                    raise StopAsyncIteration
                captured["_yielded"] = True
                return _StreamChunk()

        return _Stream()

    with patch("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion)):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "hi"},
            headers=headers,
        )
    assert resp.status_code == 200
    # min_quality_class="ultra_low" (corrected) lets FREE reach
    # groq-llama4-scout, the cheapest model in the whole catalog. Where the Free
    # Compute Pool has a source configured, a free model at least as good goes
    # first — cheaper still (it costs nothing) and billed as groq-llama4-scout.
    free_first = [source.model.model_id for source in free_pool.configured_sources()]
    expected = free_first[0] if free_first else PROVIDER_CATALOG["groq-llama4-scout"].model_id
    assert captured.get("model") == expected


# ── FREE + AGENT -> blocked by entitlement, before ANY provider/scheduler/TCU work ──

def test_free_agent_blocked_by_entitlement_before_provider(client, free_user):
    headers, _, session_id = free_user()

    with patch("src.services.agent_service._router") as mock_router:
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "do something"},
            headers=headers,
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "agent_mode_not_entitled"
    mock_router.complete.assert_not_called()  # zero provider calls
    mock_router.stream.assert_not_called()


def test_free_agent_via_message_route_also_blocked(client, free_user):
    """/message dispatches to stream_agent() whenever classified capability
    resolves to TaskType.AGENT — the gate must apply there too, not just
    the dedicated /agent endpoint."""
    headers, _, session_id = free_user()

    with patch("src.services.agent_service._router") as mock_router:
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Plan and execute this multi-step task: rename all variables in this function."},
            headers=headers,
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "agent_mode_not_entitled"
    mock_router.complete.assert_not_called()


# ── FREE + max_agents=0 -> concurrency-rejected even if agent mode were bypassed ──

def test_free_agent_scheduler_rejects_on_plan_concurrency_directly():
    """Defense-in-depth check at the scheduler level (independent of the
    entitlement-gate route check above): even if something else ever
    called agent_scheduler.check_plan_concurrency() for a FREE user, it
    must reject unconditionally — effective_limit = min(global, 0) = 0."""
    from src.services.agent_scheduler import AgentSchedulerRejectedError, InMemoryAgentScheduler

    scheduler = InMemoryAgentScheduler(global_max_concurrent=10, user_max_concurrent=2)
    with pytest.raises(AgentSchedulerRejectedError) as exc_info:
        scheduler.check_plan_concurrency("some-user", plan_max_agents=0)
    assert exc_info.value.reason == "concurrency_plan_limit"


def test_other_plans_concurrency_unaffected_by_the_new_check():
    """min(global=2, plan_limit) for start(5)/pro(20)/max&team(None) never
    goes below the scheduler's own existing default of 2 -- the new check
    is a true no-op for every plan except FREE, exactly as required
    ("não altere a política dos outros planos")."""
    from src.services.agent_scheduler import InMemoryAgentScheduler

    scheduler = InMemoryAgentScheduler(global_max_concurrent=10, user_max_concurrent=2)
    for plan_max_agents in (5, 20, None):  # start, pro, max/team
        scheduler.check_plan_concurrency("user-x", plan_max_agents=plan_max_agents)  # must not raise


# ── FREE + STANDARD/PREMIUM -> quality_not_entitled, zero provider call ──

def test_free_standard_task_blocked_via_chat_route(client, free_user):
    headers, _, session_id = free_user()
    acompletion_spy = AsyncMock()

    long_reasoning_text = (
        "Why does this function behave this way, and how would you compare it "
        "to an alternative? Please debug this code and fix the error: "
        "```python\ndef f(): pass\n```"
    )
    with patch("trinity_router.router.litellm.acompletion", acompletion_spy):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": long_reasoning_text},
            headers=headers,
        )
    assert resp.status_code == 200  # SSE always 200; the error is IN the stream
    acompletion_spy.assert_not_called()
    body = resp.text
    assert '"error": "quality_not_entitled"' in body or '"error":"quality_not_entitled"' in body


# ── FREE + provider failure -> zero failover ──

def test_free_provider_failure_never_fails_over(client, free_user):
    headers, _, session_id = free_user()
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        raise Exception("provider down")

    with patch("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion)):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "hi"},
            headers=headers,
        )
    assert resp.status_code == 200
    # Exactly one PAID attempt — FREE's max_failover_attempts=0. The Free Compute Pool's sources
    # (trinity_router/free_pool.py) may go first where configured: they cost nothing and sit outside
    # the plan's failover allowance, so the cost this rule bounds is unchanged.
    free_models = {source.model.model_id for source in free_pool.FREE_SOURCES}
    paid = [model for model in calls if model not in free_models]
    assert len(paid) == 1
    assert calls[:len(calls) - 1] == [model for model in calls if model in free_models]  # free ones first


# ── Execution Mode etapa (2026-09-07): FREE + CODE, decoupled from Agent ────
#
# Before this etapa, ANY capability that mapped to task_type=AGENT via the
# old CAPABILITY_TASK_TYPE table (CODE included) was blocked for FREE by
# agent_mode_not_entitled — even a pure text-generation code question.
# infer_execution_mode() now only escalates to AGENT when the request
# itself expresses real execution intent, independent of topic.

def test_free_code_text_only_via_message_route_not_blocked_by_agent_gate(client, free_user):
    """FREE + CODE text-only ('write a function...') must NOT hit
    agent_mode_not_entitled — it should proceed through the normal
    quality/capability/budget pipeline like any CHAT-dispatched request."""
    headers, _, session_id = free_user()

    async def _acompletion(*, model, **kwargs):
        class _StreamChunk:
            choices = [type("C", (), {"delta": type("D", (), {"content": "ok"})()})]
            usage = None

        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if getattr(self, "_done", False):
                    raise StopAsyncIteration
                self._done = True
                return _StreamChunk()

        return _Stream()

    with patch("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion)):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "write a function to add two numbers"},
            headers=headers,
        )
    assert resp.status_code == 200
    assert "agent_mode_not_entitled" not in resp.text


def test_free_code_with_execution_intent_via_message_route_is_blocked(client, free_user):
    """FREE + CODE with real execution intent ('open the repository...')
    must still be blocked by the Agent entitlement gate — FREE's 'no
    Agent' rule is unaffected by this etapa, only the FALSE POSITIVES
    (pure text-generation tasks wrongly requiring Agent) are fixed."""
    headers, _, session_id = free_user()

    with patch("src.services.agent_service._router") as mock_router:
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "open the repository, modify the files and run the tests"},
            headers=headers,
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "agent_mode_not_entitled"
    mock_router.complete.assert_not_called()


def test_free_premium_text_only_via_message_route_is_quality_not_entitled_not_agent_gate(client, free_user):
    """FREE + a premium-scoring, text-only (no execution intent) request
    must be rejected by quality_not_entitled, NOT agent_mode_not_entitled
    -- proving the two gates are now independently reachable, each for
    its own genuine reason."""
    headers, _, session_id = free_user()
    acompletion_spy = AsyncMock()

    long_text = (
        "Explain why this design choice matters, and compare it step by step to "
        "alternative approaches, including how the current function signature "
        "affects long-term maintainability. "
        + ("Consider this scenario carefully and weigh the tradeoffs of each "
           "alternative in depth. " * 60)
    )
    with patch("trinity_router.router.litellm.acompletion", acompletion_spy):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": long_text},
            headers=headers,
        )
    assert resp.status_code == 200  # CHAT-dispatched -> SSE, error is IN the stream
    acompletion_spy.assert_not_called()
    body = resp.text
    assert '"error": "quality_not_entitled"' in body or '"error":"quality_not_entitled"' in body
    assert "agent_mode_not_entitled" not in body
