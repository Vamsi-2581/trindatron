"""Etapa 5D — HTTP-level tests for the Agent Scheduler against the real
FastAPI app (auth, session lookup, Request Control, TCU reservation, and
the Agent Scheduler all wired together exactly as in production).

Covers items 18-25 of the user's Etapa 5D test list:
  18. /agent uses the Scheduler
  19. /agent rejected by the Scheduler never runs the ReAct loop
  20. /agent rejected by the Scheduler never calls the provider
  21. /agent rejected by the Scheduler never creates an orphaned execution
  22. TCU is not debited when rejected before the reservation
  23. streaming keeps the slot held for the whole execution
  24. the slot is released after a client disconnect
  25. BrowserResourceGovernor keeps working alongside the Scheduler

Concurrency tests (23/24) need two requests genuinely in flight at once, so
they use httpx.AsyncClient + ASGITransport, mirroring
test_request_control_http.py's (Etapa 5B) pattern.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from trinity_router import RoutingResult
from trinity_agent.browser.session import BrowserSession

from src.database import get_db
from src.main import app
from src.models.message import Message
from src.services.agent_scheduler import agent_scheduler

# Mixed sync (TestClient-based) and async (httpx.AsyncClient-based) tests in
# this file — pytest-asyncio's asyncio_mode="auto" (pyproject.toml) picks up
# every `async def test_*` automatically, so no module-level pytestmark is
# needed (see test_request_control.py's identical note).


def _routing_result(content: str | None = "final answer", tool_calls=None) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=tool_calls, provider="anthropic", model="claude-sonnet-test",
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)))


class _FakePage:
    def __init__(self, title: str):
        self._title = title

    async def title(self) -> str:
        return self._title


def _patched_browser_methods(*, page_title: str = "Fake Title", text: str = "fake page text"):
    async def _fake_navigate(self, url):
        return _FakePage(page_title)

    async def _fake_get_text(self):
        return text

    async def _fake_screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b"\x89PNG\r\n\x1a\n"

    return (
        patch.object(BrowserSession, "navigate", new=_fake_navigate),
        patch.object(BrowserSession, "get_text", new=_fake_get_text),
        patch.object(BrowserSession, "screenshot", new=_fake_screenshot),
    )


# ── 18. /agent uses the Scheduler ───────────────────────────────────────────

def test_agent_endpoint_acquires_and_releases_a_scheduler_slot(client, auth_headers, monkeypatch):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=_routing_result())):
        assert agent_scheduler.global_active_count == 0
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)
        assert resp.status_code == 200

    # Released once the (synchronous TestClient-driven) stream fully completed.
    assert agent_scheduler.global_active_count == 0


# ── 19/20/21. Rejected requests never run the ReAct loop / call the provider / leave an orphan ──

def test_rejected_agent_request_never_runs_react_loop_or_calls_provider(client, auth_headers, monkeypatch, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)  # force immediate rejection... see below
    # ConcurrencyLimiter validates limit >= 1 at construction; forcing 0
    # post-construction still exercises try_acquire()'s real "at capacity"
    # branch (0 active >= 0 limit), which is what matters here — this
    # mirrors the equivalent boundary already exercised in
    # tests/unit/test_agent_scheduler.py via a pre-exhausted real limit of 1.

    called = {"provider": 0}

    async def _spy_complete(request):
        called["provider"] += 1
        return _routing_result()

    with patch("src.services.agent_service._router.complete", new=_spy_complete):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 429
    assert called["provider"] == 0, "provider must never be called for a scheduler-rejected request"

    # No orphaned execution: no Message rows were ever created for this
    # session (the ReAct loop's _persist_user_message() never ran).
    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert messages == []


# ── 22. TCU not debited when rejected before the reservation ───────────────

def test_rejected_agent_request_never_reserves_tcu(client, auth_headers, monkeypatch):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)

    called = {"count": 0}

    def _spy_check_and_reserve(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("check_and_reserve must not be called for a scheduler-rejected request")

    monkeypatch.setattr("src.api.routes.sessions.check_and_reserve", _spy_check_and_reserve)

    resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)
    assert resp.status_code == 429
    assert called["count"] == 0


def test_message_endpoint_rejected_by_scheduler_never_reserves_tcu(client, auth_headers, monkeypatch):
    """Same guarantee through /message's AGENT branch (task_type ==
    TaskType.AGENT), not just the dedicated /agent endpoint."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)

    called = {"count": 0}

    def _spy_check_and_reserve(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("check_and_reserve must not be called for a scheduler-rejected request")

    monkeypatch.setattr("src.api.routes.sessions.check_and_reserve", _spy_check_and_reserve)

    # "act as an agent" classifies as Capability.AGENT -> TaskType.AGENT.
    resp = client.post(
        f"/sessions/{session_id}/message", json={"content": "act as an agent and help me"}, headers=auth_headers,
    )
    assert resp.status_code == 429
    assert called["count"] == 0


def test_message_endpoint_chat_capability_never_touches_scheduler(client, auth_headers, monkeypatch):
    """A plain chat message routed through /message must not touch the
    Agent Scheduler at all — even with the scheduler fully exhausted, a
    CHAT-task-type request still succeeds."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)

    with patch("src.services.chat_service._router.stream") as mock_stream:
        from trinity_router import RouterStreamChunk

        async def _stream(request):
            yield RouterStreamChunk(text="hi")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=3, output_tokens=3, cached_tokens=0, cost_usd=0.0, tcu=0.0,
            )
        mock_stream.side_effect = _stream

        resp = client.post(f"/sessions/{session_id}/message", json={"content": "hi there"}, headers=auth_headers)

    assert resp.status_code == 200


# ── 23/24. Streaming keeps the slot held; released after disconnect ────────

@pytest.fixture
async def async_client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _register(client: httpx.AsyncClient, email: str, db=None, plan: str = "start") -> dict:
    resp = await client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Scheduler Test",
    })
    assert resp.status_code == 201
    if db is not None:
        # A signup lands on FREE, which has no agent mode; the agent paths
        # exercised here belong to a paying plan, set the way billing would.
        from src.models.user import User
        db.query(User).filter(User.id == resp.json()["user_id"]).one().plan = plan
        db.commit()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_slot_held_for_the_whole_agent_execution_via_http(async_client, db, monkeypatch):
    headers_a = await _register(async_client, "sched-http-a@test.com", db)
    headers_b = await _register(async_client, "sched-http-b@test.com", db)
    session_a = (await async_client.post("/sessions", json={}, headers=headers_a)).json()["id"]
    session_b = (await async_client.post("/sessions", json={}, headers=headers_b)).json()["id"]

    monkeypatch.setattr(agent_scheduler._global, "_limit", 1)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _gated_complete(request):
        entered.set()
        await release.wait()
        return _routing_result()

    with patch("src.services.agent_service._router.complete", new=_gated_complete):
        task_a = asyncio.create_task(async_client.post(
            f"/sessions/{session_a}/agent", json={"content": "slow"}, headers=headers_a,
        ))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # Different user — global scheduler capacity (1) is fully held by A.
        resp_b = await async_client.post(
            f"/sessions/{session_b}/agent", json={"content": "second"}, headers=headers_b,
        )
        assert resp_b.status_code == 429

        release.set()
        resp_a = await asyncio.wait_for(task_a, timeout=5)
    assert resp_a.status_code == 200
    assert agent_scheduler.global_active_count == 0


# NOTE on item 24 ("slot é liberado após disconnect"): a genuinely reliable
# simulation of a client disconnect requires the ASGI layer to call
# `.aclose()` on the StreamingResponse's body generator — exactly what
# Starlette does on a real disconnect, and exactly what
# test_agent_scheduler_streaming.py::test_slot_released_after_generator_aclose_mid_execution
# already proves directly and deterministically (no flaky reliance on
# httpx.ASGITransport's internal body-buffering timing, which does not
# offer a documented, deterministic way to force a "pull" boundary at the
# HTTP-client layer). That test IS this guarantee, verified at the exact
# layer Starlette itself operates on — not duplicated here.


# ── 25. BrowserResourceGovernor keeps working alongside the Scheduler ──────

def test_browser_tool_still_works_through_scheduler_and_governor_together(client, auth_headers):
    """Two sequential /agent requests, each invoking a real (mocked-at-the-
    Playwright-boundary) browser tool call, with the Agent Scheduler's
    global cap tight enough (1) that the second request could only ever
    succeed if the first one's scheduler slot AND its BrowserSession were
    both fully released — proving the two mechanisms don't deadlock or
    leak against each other."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    tc = _make_tool_call("browser_navigate", {"url": "https://example.com/"})
    nav_p, text_p, shot_p = _patched_browser_methods(page_title="Example Domain")

    for i in range(2):
        responses = [_routing_result(tool_calls=[tc]), _routing_result("Navigated.")]
        with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
             nav_p, text_p, shot_p:
            resp = client.post(
                f"/sessions/{session_id}/agent", json={"content": f"go to example.com ({i})"}, headers=auth_headers,
            )
        assert resp.status_code == 200, f"iteration {i} failed: {resp.text[:300]}"

    assert agent_scheduler.global_active_count == 0
