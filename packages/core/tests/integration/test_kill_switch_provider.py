"""Etapa 5H — provider/model kill switch, proven end-to-end through the
REAL HTTP /agent path and the REAL Model Router (litellm.acompletion is
the only thing mocked — trinity_router's own chain-building/failover logic
runs unmodified). Complements model-router's own
tests/test_router_disabled_provider.py (unit-level proof of
_build_chain()'s filtering) by proving Core's admin API -> runtime_settings
-> chat_service.py/agent_service.py -> RoutingRequest.disabled_provider_keys
data flow is actually wired end to end, not just each piece in isolation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from trinity_router.providers import PROVIDER_CATALOG

from src.models.user import User
from src.services.runtime_settings import DISABLED_PROVIDER_KEYS, set_setting


def _response():
    msg = MagicMock(); msg.content = "The answer is 42."; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


def _make_admin(client, db, email: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Admin",
    })
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.is_admin = True
    db.commit()
    return {"token": data["access_token"]}


def test_disabling_the_primary_provider_makes_agent_fail_over_for_real(client, db, auth_headers, monkeypatch):
    """"start" plan's real AGENT chain is ['claude-sonnet', 'gpt-4o-mini']
    (see trinity_router.selector.select_provider_chain) — disabling
    claude-sonnet via the admin API must make a REAL /agent request use
    gpt-4o-mini instead, proven by which `model=` litellm.acompletion was
    actually called with."""
    admin = _make_admin(client, db, "provider_admin@test.com")
    resp = client.put(
        "/admin/settings/disabled_provider_keys",
        json={"value": ["claude-sonnet"]},
        headers={"Authorization": f"Bearer {admin['token']}"},
    )
    assert resp.status_code == 200

    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        return _response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    # /agent's REAL RoutingRequest.task_type is get_task_type(classify(content))
    # (see agent_service.py) — /agent forces the EXECUTOR (always runs
    # stream_agent's ReAct loop), not the routing task_type. Content must
    # classify as an AGENT-task-type capability to exercise the
    # ['claude-sonnet', 'gpt-4o-mini'] chain this test targets.
    resp = client.post(
        f"/sessions/{session_id}/agent",
        json={"content": "search the web for python tutorials"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert "claude-sonnet-5" not in calls  # the disabled provider's litellm model id
    assert "gpt-6-luna" in calls  # model behind the gpt-4o-mini alias since 2026-10-20


def test_disabling_every_agent_candidate_returns_a_provider_error_to_the_client(client, db, auth_headers, monkeypatch):
    """Adaptive Intelligence Router V2: candidate discovery is GLOBAL
    (the whole PROVIDER_CATALOG, not just select_provider_chain()'s
    2-entry legacy AGENT list) — disabling only ['claude-sonnet',
    'gpt-4o-mini'] is no longer enough to exhaust the chain, since 6
    other catalog models remain undisabled and eligible. Disable the
    ENTIRE catalog to genuinely test "every candidate gone"."""
    admin = _make_admin(client, db, "provider_admin2@test.com")
    client.put(
        "/admin/settings/disabled_provider_keys",
        json={"value": list(PROVIDER_CATALOG.keys())},
        headers={"Authorization": f"Bearer {admin['token']}"},
    )

    acompletion_spy = AsyncMock(return_value=_response())
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    resp = client.post(f"/sessions/{session_id}/agent", json={"content": "search the web for python tutorials"}, headers=auth_headers)

    assert resp.status_code == 200  # SSE stream already started (200), error arrives IN the stream
    assert "error" in resp.text or "unavailable" in resp.text.lower()
    acompletion_spy.assert_not_called()


def test_provider_disable_does_not_affect_other_users_normal_requests(client, db, auth_headers, monkeypatch):
    """No disabled_provider_keys set at all — a normal request must reach
    an eligible model from the real, undisabled chain in a single attempt
    (no failover triggered by kill-switch state leaking from another test).

    Since the Adaptive Intelligence Router, "eligible model chosen" is no
    longer always the static chain's first entry: this request's low
    complexity (simple research/tool-use, no code/reasoning/multi-step
    signals) only requires "low" quality, so the cheapest eligible
    candidate in the ['claude-sonnet' (standard), 'gpt-4o-mini' (low)]
    pool — gpt-4o-mini — legitimately wins on cost. That is the intended
    "cheapest capable" behavior, not a kill-switch leak; what this test
    actually guards is a SINGLE call with no failover/disabled-provider
    interference."""
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        return _response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    resp = client.post(f"/sessions/{session_id}/agent", json={"content": "search the web for python tutorials"}, headers=auth_headers)

    assert resp.status_code == 200
    assert calls == ["gpt-6-luna"]  # cheapest capable for this low-complexity request; no failover
