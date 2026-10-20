"""
Smoke tests: capability-specific routing through the real /message pipeline.

Each test has two assertions that together prove end-to-end routing:
  (a) capability reached RoutingRequest correctly (captured from mocked router call)
  (b) that (capability, user_plan) produces the correct chain via select_provider_chain

This mirrors the pattern used in test_routing_request_capability.py (model-router unit tests)
and test_user_plan_wiring.py (core integration tests) — no new infrastructure needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from trinity_router import RoutingRequest, RoutingResult, RouterStreamChunk
from trinity_router.selector import TaskType, select_provider_chain

from src.models.user import User


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _register_and_set_plan(client, db, email: str, plan: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email,
        "password": "securepass123",
        "full_name": "Routing Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.plan = plan
    db.commit()
    db.refresh(user)
    return {"token": data["access_token"], "user_id": data["user_id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_session(client, headers) -> str:
    return client.post("/sessions", json={}, headers=headers).json()["id"]


def _message(client, headers, session_id, content):
    return client.post(
        f"/sessions/{session_id}/message",
        json={"content": content},
        headers=headers,
    )


def _make_agent_result() -> RoutingResult:
    return RoutingResult(
        content="done",
        tool_calls=None,
        provider="anthropic",
        model="claude-test",
        input_tokens=5,
        output_tokens=2,
        cached_tokens=0,
        cost_usd=0.0,
        tcu=0.0,
        failover_used=False,
        failover_reason=None,
    )


def _capture_agent() -> tuple[list[RoutingRequest], object]:
    """Returns (captured, async_fn). captured collects every RoutingRequest seen."""
    captured: list[RoutingRequest] = []

    async def _fn(request: RoutingRequest) -> RoutingResult:
        captured.append(request)
        return _make_agent_result()

    return captured, _fn


def _capture_chat() -> tuple[list[RoutingRequest], object]:
    """Returns (captured, async_gen_fn). captured collects every RoutingRequest seen."""
    captured: list[RoutingRequest] = []

    async def _fn(request: RoutingRequest):
        captured.append(request)
        yield RouterStreamChunk(text="ok")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=2, cached_tokens=0,
            cost_usd=0.0, tcu=0.0,
        )

    return captured, _fn


# ─── CODE + start → ["claude-sonnet"] ───────────────────────────────────────

def test_code_start_selects_the_curated_code_chain(client, db):
    """
    End-to-end: CODE message (with real execution intent) + start plan.
    (a) RoutingRequest.capability == "code", .user_plan == "start"
    (b) select_provider_chain(AGENT, "start", "code") == ["claude-sonnet"]

    Execution Mode etapa (2026-09-07): a pure text-generation CODE message
    ("write a function that reverses a list") no longer dispatches to
    Agent at all — infer_execution_mode() correctly keeps it on CHAT,
    since it needs no tools (see test_execution_mode.py). This test is
    specifically about the AGENT-dispatch chain-selection behavior, so
    the message now also carries genuine execution intent to actually
    reach that path.
    """
    user = _register_and_set_plan(client, db, "cri_code_start@test.com", "start")
    session_id = _new_session(client, _auth(user["token"]))
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = _message(
            client, _auth(user["token"]), session_id,
            "Debug this code, save the fix to disk, and run the tests.",
        )

    assert resp.status_code == 200
    assert len(captured) == 1
    req = captured[0]

    # (a) capability and plan reached the router correctly
    assert req.capability == "code"
    assert req.user_plan == "start"

    # (b) (capability, plan) → correct chain
    chain = select_provider_chain(req.task_type, req.user_plan, req.capability)
    # Held ["claude-sonnet", "gemini-25-flash"] until 2026-09-13. The alias was
    # removed from the catalog as unreachable, and this slot was deliberately
    # NOT refilled — unlike PRO/MAX, this chain carries no provider-diversity
    # invariant, and putting another model here would be inventing a routing
    # decision nobody measured. START/code therefore has no fallback on the
    # LEGACY path; the dynamic path that actually runs is unaffected and still
    # serves it from groq-llama4-scout -> gpt-4o-mini.
    assert chain == ["claude-sonnet"]
    assert "gpt-4o-mini" not in chain


# ─── CODE + pro → standard pro task chain ────────────────────────────────────

def test_code_pro_selects_standard_chain(client, db):
    """
    End-to-end: CODE message (with real execution intent) + pro plan.
    (a) RoutingRequest.capability == "code", .user_plan == "pro"
    (b) select_provider_chain returns the standard pro chain (no capability override for pro)

    See test_code_start_selects_the_curated_code_chain's docstring for why
    the message text now carries execution intent (2026-09-07).
    """
    user = _register_and_set_plan(client, db, "cri_code_pro@test.com", "pro")
    session_id = _new_session(client, _auth(user["token"]))
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = _message(
            client, _auth(user["token"]), session_id,
            "Debug this code, save the fix to disk, and run the tests.",
        )

    assert resp.status_code == 200
    req = captured[0]
    assert req.capability == "code"
    assert req.user_plan == "pro"

    chain = select_provider_chain(req.task_type, req.user_plan, req.capability)
    expected = select_provider_chain(TaskType.AGENT, "pro")
    assert chain == expected  # no override for code/pro


# ─── CODE + max → standard max task chain ────────────────────────────────────

def test_code_max_selects_standard_chain(client, db):
    """
    End-to-end: CODE message (with real execution intent) + max plan.
    (a) RoutingRequest.capability == "code", .user_plan == "max"
    (b) select_provider_chain returns the standard max chain (no capability override for max)

    See test_code_start_selects_the_curated_code_chain's docstring for why
    the message text now carries execution intent (2026-09-07).
    """
    user = _register_and_set_plan(client, db, "cri_code_max@test.com", "max")
    session_id = _new_session(client, _auth(user["token"]))
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = _message(
            client, _auth(user["token"]), session_id,
            "Debug this code, save the fix to disk, and run the tests.",
        )

    assert resp.status_code == 200
    req = captured[0]
    assert req.capability == "code"
    assert req.user_plan == "max"

    chain = select_provider_chain(req.task_type, req.user_plan, req.capability)
    expected = select_provider_chain(TaskType.AGENT, "max")
    assert chain == expected  # no override for code/max


# ─── FILES + start → ["claude-sonnet", "gemini-25-flash-lite"] ───────────────

def test_files_start_selects_long_context_chain(client, db):
    """
    End-to-end: FILES message + start plan.
    (a) RoutingRequest.capability == "files", .user_plan == "start"
    (b) select_provider_chain returns ["claude-sonnet", "gemini-25-flash-lite"] — no gpt-4o-mini
    """
    user = _register_and_set_plan(client, db, "cri_files_start@test.com", "start")
    session_id = _new_session(client, _auth(user["token"]))
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = _message(
            client, _auth(user["token"]), session_id,
            "Read the file and parse the CSV",
        )

    assert resp.status_code == 200
    assert len(captured) == 1
    req = captured[0]

    assert req.capability == "files"
    assert req.user_plan == "start"

    chain = select_provider_chain(req.task_type, req.user_plan, req.capability)
    assert chain == ["claude-sonnet", "gemini-25-flash-lite"]
    assert "gpt-4o-mini" not in chain


# ─── CHAT + start → standard chat chain, no override ─────────────────────────

def test_chat_start_selects_standard_chain(client, db):
    """
    End-to-end: CHAT message + start plan.
    (a) RoutingRequest.capability == "chat", .user_plan == "start"
    (b) select_provider_chain returns the standard chat chain (no capability override)
    """
    user = _register_and_set_plan(client, db, "cri_chat_start@test.com", "start")
    session_id = _new_session(client, _auth(user["token"]))
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        resp = _message(
            client, _auth(user["token"]), session_id,
            "Hello, how are you today?",
        )

    assert resp.status_code == 200
    assert len(captured) == 1
    req = captured[0]

    assert req.capability == "chat"
    assert req.user_plan == "start"

    chain = select_provider_chain(req.task_type, req.user_plan, req.capability)
    expected = select_provider_chain(TaskType.CHAT, "start")
    assert chain == expected
    assert "groq-llama4-scout" in chain  # standard start/CHAT chain preserved (cheapest primary)


# ─── CODE: classify() called exactly once through /message ────────────────────

def test_code_classify_called_exactly_once(client, db):
    """classify() must fire exactly once per CODE /message request — no double-classification."""
    user = _register_and_set_plan(client, db, "cri_code_classify@test.com", "start")
    session_id = _new_session(client, _auth(user["token"]))

    import src.capability.classifier as _clf
    original = _clf.classify
    call_count = {"n": 0}

    def _counting(content):
        call_count["n"] += 1
        return original(content)

    _, mock_fn = _capture_agent()

    with patch("src.api.routes.sessions.classify", side_effect=_counting), \
         patch("src.services.agent_service.classify", side_effect=_counting), \
         patch("src.services.agent_service._router.complete", mock_fn):
        _message(
            client, _auth(user["token"]), session_id,
            "Write a function that reverses a list",
        )

    assert call_count["n"] == 1, (
        f"classify() called {call_count['n']} times — expected exactly 1"
    )
