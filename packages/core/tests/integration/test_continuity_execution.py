"""Etapa 5F (second round, execution wiring) — proves the REAL activation
path: quota exhausted -> Continuity gate -> forced_provider_key pinning ->
real stream_chat()/stream_agent() execution -> financial true-up.

test_continuity_entitlement.py already proves the entitlement/financial
gate (check_and_debit_continuity/activate_continuity_or_raise/true_up)
correct in isolation — this file proves the same gate is actually wired
into POST /chat, /agent, /message, and that the wiring never lets
Continuity activate for anything OTHER than a genuine premium-quota
rejection (Request Control, Agent Scheduler, and provider failures all
stay untouched).

Concurrency invariants (budget/balance never exceeded under contention)
are deliberately NOT re-proven at the HTTP/streaming layer here — this
codebase's own precedent (test_entitlement.py's
test_concurrent_reservation_only_one_succeeds, and this etapa's own
test_continuity_entitlement.py::test_concurrent_continuity_debits_never_exceed_the_budget)
proves such invariants at the direct-service-call level, not through
TestClient/SSE, since the DB-level advisory lock is what's actually being
tested and an HTTP layer would only add unrelated flakiness.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from trinity_router import (
    ProviderUnavailableError,
    RoutingRequest,
    RoutingResult,
    RouterStreamChunk,
)
from trinity_router.providers import ProviderModel

from src.entitlements import get_plan_entitlement
from src.models.ledger import CreditTransaction, LedgerEntry
from src.models.message import Message
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import agent_service, chat_service
from src.services.continuity_entitlement_service import check_and_debit_continuity
from src.services.credit_service import CreditService

# ── Shared helpers ───────────────────────────────────────────────────────────

def _register_and_set_plan(client, db, email: str, plan: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Continuity Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.plan = plan
    db.commit()
    db.refresh(user)
    return {"token": data["access_token"], "user_id": data["user_id"], "plan": user.plan}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_credits(db, user_id: str, amount: str) -> None:
    CreditService(db).credit(
        user_id=user_id, currency="TCU", amount=Decimal(amount),
        idempotency_key=f"test-grant-{uuid.uuid4()}", transaction_type="grant", note="test setup",
    )


def _continuity_entitlement(plan: str, *, enabled: bool, daily_limit: float):
    base = get_plan_entitlement(plan)
    return replace(base, continuity_enabled=enabled, continuity_daily_tcu_limit=daily_limit)


_FAKE_MODEL_KEY = "continuity-test-model"


def _fake_continuity_model(supports_tools: bool = True) -> tuple[str, ProviderModel]:
    return _FAKE_MODEL_KEY, ProviderModel(
        provider="anthropic", model_id="continuity-test-model",
        context_window=200_000, cost_per_1m_input_usd=0.1, cost_per_1m_output_usd=0.3,
        supports_tools=supports_tools, supports_streaming=True,
        priority=99, modality="text", quality_class="low", speed_class="fast",
    )


def _exhausted_quota_exception():
    from fastapi import HTTPException
    return HTTPException(
        status_code=429,
        detail={"error": "daily_limit_exceeded", "message": "Limite diário de TCU atingido.",
                "daily_used": 100.0, "daily_limit": 100.0},
    )


def _make_chat_stream_capture(texts: list[str], cost_usd: float = 0.0):
    """MagicMock standing in for _router.stream — a plain function would
    work for chat_service's `async for chunk in _router.stream(request)`
    (a sync call returning an async generator), but tests here also need
    `.assert_not_called()`/`.call_args`, so it's wrapped as a MagicMock
    with side_effect. `captured` is kept for convenience/readability at
    call sites that already destructure it."""
    captured: list[RoutingRequest] = []
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model=_FAKE_MODEL_KEY,
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
    ))

    def _stream(request):
        captured.append(request)

        async def _gen():
            for chunk in chunks:
                yield chunk

        return _gen()

    return captured, MagicMock(side_effect=_stream)


def _make_agent_complete_capture(content: str, cost_usd: float = 0.0):
    """AsyncMock standing in for _router.complete (always awaited)."""
    captured: list[RoutingRequest] = []

    def _complete(request):
        captured.append(request)
        return RoutingResult(
            content=content, tool_calls=None, provider="anthropic", model=_FAKE_MODEL_KEY,
            input_tokens=10, output_tokens=5, cached_tokens=0,
            cost_usd=cost_usd, tcu=cost_usd * 100.0, failover_used=False, failover_reason=None,
        )

    return captured, AsyncMock(side_effect=_complete)


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


async def _drain(agen):
    async for _ in agen:
        pass


# ═════════════════════════════════════════════════════════════════════════
# A. HTTP-level: activation per plan, using the REAL production values
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("plan,daily_limit", [
    ("start", 10.0), ("pro", 30.0), ("max", 75.0), ("team", 150.0),
])
def test_chat_continuity_activates_for_each_paid_plan_within_official_budget(
    client, db, plan, daily_limit,
):
    """Real (non-monkeypatched) PlanEntitlement values gate a genuine HTTP
    /chat request end to end: quota exhausted -> Continuity -> 200 SSE."""
    user = _register_and_set_plan(client, db, f"{plan}_ok@test.com", plan)
    _grant_credits(db, user["user_id"], "1000")

    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    captured, stream_mock = _make_chat_stream_capture(["Hi from Continuity"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", stream_mock):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    assert any(e.get("type") == "done" for e in events)
    assert len(captured) == 1
    assert captured[0].forced_provider_key == _FAKE_MODEL_KEY

    # Financial trace: exactly one consume transaction, capped at the plan's budget.
    consume = db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user["user_id"], CreditTransaction.transaction_type == "consume",
    ).all()
    assert len(consume) == 1
    assert float(consume[0].amount) <= daily_limit


def test_chat_continuity_never_activates_for_free_plan(client, db):
    user = _register_and_set_plan(client, db, "free_denied@test.com", "free")
    _grant_credits(db, user["user_id"], "1000")  # money is not the reason for the 429
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429
    assert db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user["user_id"], CreditTransaction.transaction_type == "consume",
    ).count() == 0


# ═════════════════════════════════════════════════════════════════════════
# B. NORMAL path is completely unaffected
# ═════════════════════════════════════════════════════════════════════════

def test_chat_normal_path_never_touches_continuity_model_selection(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["all good"])

    with patch("src.services.continuity_mode.select_continuity_model") as select_mock, \
         patch("src.services.chat_service._router.stream", stream_mock):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 200
    select_mock.assert_not_called()


def test_chat_check_and_reserve_called_exactly_once_even_when_continuity_activates(client, db):
    user = _register_and_set_plan(client, db, "single_reserve@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["ok"])

    call_count = {"n": 0}
    real_exc = _exhausted_quota_exception()

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        raise real_exc

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_spy), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", stream_mock):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    assert call_count["n"] == 1  # never retried against the premium path


# ═════════════════════════════════════════════════════════════════════════
# C. Continuity's OWN budget exhausted -> controlled rejection, no loop
# ═════════════════════════════════════════════════════════════════════════

def test_chat_continuity_budget_exhausted_returns_429_not_infinite_retry(client, db, monkeypatch):
    user = _register_and_set_plan(client, db, "budget_gone@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=10.0),
    )
    # Pre-exhaust the whole Continuity daily budget before this request.
    check_and_debit_continuity(db, user_id=user["user_id"], plan="start", estimated_tcu=10.0, execution_id=str(uuid.uuid4()))
    db.commit()
    balance_before = CreditService(db).balance(user["user_id"], "TCU")

    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["should never run"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.chat_service._router.stream", stream_mock) as stream_spy:
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429  # falls through to the original 429, unchanged
    stream_spy.assert_not_called()  # provider never touched
    assert CreditService(db).balance(user["user_id"], "TCU") == balance_before  # no second debit


# ═════════════════════════════════════════════════════════════════════════
# D. Insufficient financial balance -> rejection, never a negative balance
# ═════════════════════════════════════════════════════════════════════════

def test_chat_insufficient_balance_returns_429_and_no_negative_balance(client, db):
    user = _register_and_set_plan(client, db, "broke@test.com", "start")
    # No credits granted at all.
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["should never run"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.chat_service._router.stream", stream_mock) as stream_spy:
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429
    stream_spy.assert_not_called()
    assert db.query(CreditTransaction).filter(CreditTransaction.user_id == user["user_id"]).count() == 0


# ═════════════════════════════════════════════════════════════════════════
# E. No duplicate ledger for a real end-to-end HTTP execution
# ═════════════════════════════════════════════════════════════════════════

def test_chat_continuity_creates_exactly_one_ledger_entry_pair_end_to_end(client, db):
    user = _register_and_set_plan(client, db, "ledger_once@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["ok"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", stream_mock):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    entries = db.query(LedgerEntry).filter(LedgerEntry.entry_type == "ai_cost").all()
    debit_credit_pairs = [(e.direction, e.account_from, e.account_to) for e in entries]
    assert len(debit_credit_pairs) == 2
    assert set(d for d, *_ in debit_credit_pairs) == {"debit", "credit"}


# ═════════════════════════════════════════════════════════════════════════
# F. Provider failures / Request Control / Agent Scheduler NEVER activate Continuity
# ═════════════════════════════════════════════════════════════════════════

def test_chat_provider_failure_with_quota_available_never_touches_continuity(client, auth_headers, db):
    """Quota is genuinely available here (no mocking of check_and_reserve) —
    only the PROVIDER fails. Continuity must never be considered at all."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    async def _raising_stream(request):
        raise ProviderUnavailableError("provider down")
        yield  # pragma: no cover

    with patch("src.services.chat_service._router.stream", _raising_stream), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock:
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 200  # 429 never happens; error arrives inside the SSE stream
    events = _parse_sse(resp.text)
    assert any(e.get("type") == "error" for e in events)
    select_mock.assert_not_called()
    assert db.query(CreditTransaction).count() == 0


def test_chat_request_control_rejection_never_reaches_continuity(client, auth_headers):
    from src.services.request_control import RequestRejectedError

    with patch("src.services.request_control.request_control.check_rate_limits",
               side_effect=RequestRejectedError("rate_limit_user", retry_after_seconds=1)), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock:
        resp = client.post("/sessions/any/chat", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 429
    select_mock.assert_not_called()


def test_agent_scheduler_rejection_never_reaches_continuity(client, auth_headers, db):
    from src.services.agent_scheduler import AgentSchedulerRejectedError

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.agent_scheduler.agent_scheduler.try_acquire",
               side_effect=AgentSchedulerRejectedError("concurrency_global")), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 429
    select_mock.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# G. Multimodal capability blocked before any provider/financial call
# ═════════════════════════════════════════════════════════════════════════

def test_message_multimodal_capability_rejected_before_any_ledger_write(client, db):
    user = _register_and_set_plan(client, db, "multimodal@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "generate an image of a cat"},
            headers=_auth(user["token"]),
        )

    assert resp.status_code == 429
    assert db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user["user_id"], CreditTransaction.transaction_type == "consume",
    ).count() == 0


# ═════════════════════════════════════════════════════════════════════════
# H. No client-controlled activation
# ═════════════════════════════════════════════════════════════════════════

def test_chat_client_supplied_continuity_flag_is_ignored(client, auth_headers, db):
    """ChatRequest has no continuity-related field; an extra client-supplied
    key must be silently dropped by the schema, never interpreted as an
    activation request. Quota is genuinely available here."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["normal"])

    with patch("src.services.chat_service._router.stream", stream_mock), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock:
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "hello", "continuity": True, "force_continuity": True},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    select_mock.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# I. Agent: requires_tools, browser-tool blocking, tool-capable model selection
# ═════════════════════════════════════════════════════════════════════════

def test_agent_continuity_requests_a_tool_capable_model(client, db):
    user = _register_and_set_plan(client, db, "agent_tools@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, complete_mock = _make_agent_complete_capture("done")

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock, \
         patch("src.services.agent_service._router.complete", complete_mock):
        select_mock.return_value = _fake_continuity_model(supports_tools=True)
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    assert select_mock.call_args.kwargs.get("requires_tools") is True


def test_chat_continuity_does_not_require_tools(client, db):
    user = _register_and_set_plan(client, db, "chat_no_tools@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["ok"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model") as select_mock, \
         patch("src.services.chat_service._router.stream", stream_mock):
        select_mock.return_value = _fake_continuity_model(supports_tools=False)
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    assert select_mock.call_args.kwargs.get("requires_tools") is False


@pytest.mark.asyncio
async def test_agent_continuity_removes_browser_tools_from_active_tools(db, monkeypatch):
    """Direct-generator proof (not HTTP) — mirrors test_request_control_streaming.py's
    style. Captures the `tools` list actually sent to the router."""
    from src.services.session_service import create_session

    user_id = str(uuid.uuid4())
    db.add(User(id=user_id, email=f"{uuid.uuid4()}@test.com", hashed_password="x", plan="start"))
    db.commit()
    session = create_session(db, user_id=user_id, model=None, project_id=None, agent_definition_id=None, is_temporary=False)

    captured, complete_mock = _make_agent_complete_capture("no tools needed")
    monkeypatch.setattr(agent_service._router, "complete", complete_mock)

    await _drain(agent_service.stream_agent(
        db, session, "hello",
        continuity_execution_id=str(uuid.uuid4()), continuity_estimated_tcu=1.0,
    ))

    assert len(captured) == 1
    tool_names = {t["function"]["name"] for t in captured[0].tools}
    assert tool_names.isdisjoint({"browser_navigate", "browser_get_text", "browser_screenshot"})


@pytest.mark.asyncio
async def test_agent_continuity_blocks_a_browser_tool_call_defense_in_depth(db, monkeypatch):
    """Even if a browser tool call somehow arrives (model hallucination,
    catalog drift), the dispatch loop itself must refuse it — not just the
    active_tools filter."""
    from types import SimpleNamespace

    from src.services.session_service import create_session

    user_id = str(uuid.uuid4())
    db.add(User(id=user_id, email=f"{uuid.uuid4()}@test.com", hashed_password="x", plan="start"))
    db.commit()
    session = create_session(db, user_id=user_id, model=None, project_id=None, agent_definition_id=None, is_temporary=False)

    tool_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="browser_navigate", arguments="{}"),
    )
    with_tool = RoutingResult(
        content=None, tool_calls=[tool_call], provider="anthropic", model=_FAKE_MODEL_KEY,
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )
    final = RoutingResult(
        content="cannot browse", tool_calls=None, provider="anthropic", model=_FAKE_MODEL_KEY,
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )
    monkeypatch.setattr(agent_service._router, "complete", AsyncMock(side_effect=[with_tool, final]))

    events = []
    async for event in agent_service.stream_agent(
        db, session, "browse the web",
        continuity_execution_id=str(uuid.uuid4()), continuity_estimated_tcu=1.0,
    ):
        events.append(event)

    tool_results = [
        json.loads(e[len("data: "):]) for e in events if '"type": "tool_result"' in e
    ]
    assert any(r["output"] == "Esta ação não está disponível agora." for r in tool_results)
    assert not any("Continuity" in json.dumps(r) or "tool" in r for r in tool_results), \
        "the client sees a neutral refusal and an activity key, never the internal mode or a raw tool field"


# ═════════════════════════════════════════════════════════════════════════
# J. Financial true-up wired into the REAL streaming execution (direct-generator)
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def continuity_chat_setup(db):
    """A user + session + an already-debited Continuity execution, ready
    for stream_chat()/stream_agent() to reconcile via true_up_continuity()
    in their own `finally` block — exactly the sessions.py contract."""
    from src.services.session_service import create_session

    # "max" rather than "start": these tests are about reconciling a debit
    # against what an execution really cost, and they work in round numbers
    # (a 10 TCU estimate, refunds down to 4). START's Continuity budget is
    # 4 TCU/day, so seeding 10 there would make every one of them a budget
    # test by accident. MAX's 30 holds the arithmetic without changing it.
    user_id = str(uuid.uuid4())
    db.add(User(id=user_id, email=f"{uuid.uuid4()}@test.com", hashed_password="x", plan="max"))
    db.commit()
    session = create_session(db, user_id=user_id, model=None, project_id=None, agent_definition_id=None, is_temporary=False)

    _grant_credits(db, user_id, "100")
    execution_id = str(uuid.uuid4())
    check_and_debit_continuity(db, user_id=user_id, plan="max", estimated_tcu=10.0, execution_id=execution_id)
    db.commit()
    assert CreditService(db).balance(user_id, "TCU") == Decimal("90")
    return {"user_id": user_id, "session": session, "execution_id": execution_id}


@pytest.mark.asyncio
async def test_chat_continuity_partial_refund_on_success(db, continuity_chat_setup, monkeypatch):
    _, stream_mock = _make_chat_stream_capture(["hi"], cost_usd=0.04)  # tcu = 4.0
    monkeypatch.setattr(chat_service._router, "stream", stream_mock)

    await _drain(chat_service.stream_chat(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    ))

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("96")  # refunded 6.0


@pytest.mark.asyncio
async def test_chat_continuity_full_refund_on_provider_error(db, continuity_chat_setup, monkeypatch):
    async def _raising_stream(request):
        raise ProviderUnavailableError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_service._router, "stream", _raising_stream)

    await _drain(chat_service.stream_chat(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    ))

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("100")  # fully refunded


@pytest.mark.asyncio
async def test_chat_continuity_full_refund_on_client_disconnect(db, continuity_chat_setup, monkeypatch):
    release_event = asyncio.Event()
    entered_event = asyncio.Event()

    async def _gated_stream(request):
        yield RouterStreamChunk(text="partial")
        entered_event.set()
        await release_event.wait()
        yield RouterStreamChunk(is_final=True, provider="anthropic", model=_FAKE_MODEL_KEY,
                                 input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0)

    monkeypatch.setattr(chat_service._router, "stream", _gated_stream)

    agen = chat_service.stream_chat(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    )
    await agen.__anext__()  # capability_status_event
    await agen.__anext__()  # "partial" text chunk
    await agen.aclose()  # simulates SSE client disconnect

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("100")  # full refund, no partial credit


@pytest.mark.asyncio
async def test_chat_continuity_full_refund_on_cancelled_error(db, continuity_chat_setup, monkeypatch):
    release_event = asyncio.Event()
    entered_event = asyncio.Event()

    async def _gated_stream(request):
        yield RouterStreamChunk(text="partial")
        entered_event.set()
        await release_event.wait()
        yield RouterStreamChunk(is_final=True, provider="anthropic", model=_FAKE_MODEL_KEY,
                                 input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0)

    monkeypatch.setattr(chat_service._router, "stream", _gated_stream)

    agen = chat_service.stream_chat(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    )

    async def consume():
        async for _ in agen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered_event.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("100")


@pytest.mark.asyncio
async def test_agent_continuity_partial_refund_on_success(db, continuity_chat_setup, monkeypatch):
    _, complete_mock = _make_agent_complete_capture("final answer", cost_usd=0.04)  # tcu = 4.0
    monkeypatch.setattr(agent_service._router, "complete", complete_mock)

    await _drain(agent_service.stream_agent(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    ))

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("96")


@pytest.mark.asyncio
async def test_agent_continuity_full_refund_on_provider_error(db, continuity_chat_setup, monkeypatch):
    async def _raising_complete(request):
        raise ProviderUnavailableError("down")

    monkeypatch.setattr(agent_service._router, "complete", _raising_complete)

    await _drain(agent_service.stream_agent(
        db, continuity_chat_setup["session"], "hi",
        continuity_execution_id=continuity_chat_setup["execution_id"], continuity_estimated_tcu=10.0,
    ))

    assert CreditService(db).balance(continuity_chat_setup["user_id"], "TCU") == Decimal("100")


# ═════════════════════════════════════════════════════════════════════════
# K. Message persistence remains correct under Continuity
# ═════════════════════════════════════════════════════════════════════════

def test_chat_continuity_persists_messages_correctly(client, db):
    user = _register_and_set_plan(client, db, "persist@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]
    _, stream_mock = _make_chat_stream_capture(["Continuity reply!"])

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", stream_mock):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "Save me"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    messages = db.query(Message).filter(Message.session_id == session_id).order_by(Message.created_at).all()
    assert len(messages) == 2
    assert messages[0].role == "user" and messages[0].content == "Save me"
    assert messages[1].role == "assistant" and messages[1].content == "Continuity reply!"

    # Continuity has no reservation_id -> routed through the record_usage() fallback.
    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
