"""Etapa 5B — HTTP-level tests for Request Control against the real
FastAPI app (auth, session lookup, TCU reservation, and Request Control all
wired together exactly as in production).

Covers items 1-7 and 14-16 of the user's test list:
  1.  user under limit still works
  2.  user over rate limit gets 429 (+ Retry-After)
  3.  IP over limit gets 429 (+ Retry-After)
  4.  user concurrency limit enforced
  5.  global concurrency enforced
  6.  user A doesn't block user B while global capacity remains
  7.  global limit exhausted rejects new requests
  14. a rejected request never calls check_and_reserve / consumes TCU
  15. existing TCU behavior intact for accepted requests
  16. existing endpoints keep working with Request Control wired in

Concurrency tests (4-7) need two requests genuinely in flight at once, so
they use httpx.AsyncClient + ASGITransport (like
test_event_loop_concurrency.py), with the mocked provider stream gated by
an asyncio.Event so request A can be held open on purpose.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from trinity_router import RouterStreamChunk

from src.database import get_db
from src.main import app
from src.services.request_control import request_control

pytestmark = pytest.mark.asyncio


def _make_stream_mock(texts: list[str]):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=0.0, tcu=0.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _make_gated_stream_mock(release_event: asyncio.Event, entered_event: asyncio.Event):
    async def _stream(request):
        entered_event.set()
        await release_event.wait()
        yield RouterStreamChunk(text="held")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-sonnet-test",
            input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    return _stream


@pytest.fixture
async def async_client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _register(client: httpx.AsyncClient, email: str) -> dict:
    resp = await client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "RC Test",
    })
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── 1. Baseline: a normal request under every limit still works ─────────────

async def test_request_under_limits_succeeds(async_client, monkeypatch):
    headers = await _register(async_client, "rc-baseline@test.com")
    session_id = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["hi"]))
        resp = await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=headers,
        )
    assert resp.status_code == 200


# ── 2/3. Rate limits ──────────────────────────────────────────────────────────

async def test_user_over_rate_limit_gets_429_with_retry_after(async_client, monkeypatch):
    headers = await _register(async_client, "rc-user-rate@test.com")
    session_id = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]

    monkeypatch.setattr(request_control, "_user_rate_limit", 1)

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["one"]))
        first = await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "first"}, headers=headers,
        )
        assert first.status_code == 200

        second = await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "second"}, headers=headers,
        )
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) >= 1
    assert second.json()["detail"] == "Too many requests. Please try again shortly."


async def test_ip_over_rate_limit_gets_429_across_different_users(async_client, monkeypatch):
    headers_a = await _register(async_client, "rc-ip-a@test.com")
    headers_b = await _register(async_client, "rc-ip-b@test.com")
    session_a = (await async_client.post("/sessions", json={}, headers=headers_a)).json()["id"]
    session_b = (await async_client.post("/sessions", json={}, headers=headers_b)).json()["id"]

    # Both requests share the same simulated client (httpx ASGITransport ->
    # same request.client.host), so the IP dimension is what's exercised
    # here even though the two users are otherwise independent.
    monkeypatch.setattr(request_control, "_ip_rate_limit", 1)

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["one"]))
        first = await async_client.post(
            f"/sessions/{session_a}/chat", json={"content": "first"}, headers=headers_a,
        )
        assert first.status_code == 200

        second = await async_client.post(
            f"/sessions/{session_b}/chat", json={"content": "second"}, headers=headers_b,
        )
    assert second.status_code == 429
    assert "Retry-After" in second.headers


# ── 4-7. Concurrency ──────────────────────────────────────────────────────────

async def test_user_concurrency_limit_enforced(async_client, monkeypatch):
    headers = await _register(async_client, "rc-user-conc@test.com")
    session_a = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]
    session_b = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]

    monkeypatch.setattr(request_control._user_concurrency, "_limit", 1)

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(
        "src.services.chat_service._router.stream",
        _make_gated_stream_mock(release_event, entered_event),
    )

    task_a = asyncio.create_task(async_client.post(
        f"/sessions/{session_a}/chat", json={"content": "slow"}, headers=headers,
    ))
    await asyncio.wait_for(entered_event.wait(), timeout=5)

    # Same user, second in-flight request — must be rejected while A holds the slot.
    resp_b = await async_client.post(
        f"/sessions/{session_b}/chat", json={"content": "second"}, headers=headers,
    )
    assert resp_b.status_code == 429

    release_event.set()
    resp_a = await asyncio.wait_for(task_a, timeout=5)
    assert resp_a.status_code == 200


async def test_global_concurrency_limit_enforced_then_recovers(async_client, monkeypatch):
    headers_a = await _register(async_client, "rc-global-a@test.com")
    headers_b = await _register(async_client, "rc-global-b@test.com")
    session_a = (await async_client.post("/sessions", json={}, headers=headers_a)).json()["id"]
    session_b = (await async_client.post("/sessions", json={}, headers=headers_b)).json()["id"]

    monkeypatch.setattr(request_control._global_concurrency, "_limit", 1)

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(
        "src.services.chat_service._router.stream",
        _make_gated_stream_mock(release_event, entered_event),
    )

    task_a = asyncio.create_task(async_client.post(
        f"/sessions/{session_a}/chat", json={"content": "slow"}, headers=headers_a,
    ))
    await asyncio.wait_for(entered_event.wait(), timeout=5)

    # Different user — global capacity (not the user dimension) is what's
    # exhausted here, so even an otherwise-unrelated user gets rejected.
    resp_b = await async_client.post(
        f"/sessions/{session_b}/chat", json={"content": "second"}, headers=headers_b,
    )
    assert resp_b.status_code == 429

    release_event.set()
    resp_a = await asyncio.wait_for(task_a, timeout=5)
    assert resp_a.status_code == 200

    # Global slot released — the process recovers, a fresh request succeeds.
    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["ok"]))
        resp_recover = await async_client.post(
            f"/sessions/{session_b}/chat", json={"content": "third"}, headers=headers_b,
        )
    assert resp_recover.status_code == 200


async def test_user_a_does_not_block_user_b_while_global_capacity_remains(async_client, monkeypatch):
    headers_a = await _register(async_client, "rc-independent-a@test.com")
    headers_b = await _register(async_client, "rc-independent-b@test.com")
    session_a = (await async_client.post("/sessions", json={}, headers=headers_a)).json()["id"]
    session_b = (await async_client.post("/sessions", json={}, headers=headers_b)).json()["id"]

    # Per-user limit of 1 each, but plenty of global capacity — user A being
    # at its own limit must not affect user B at all.
    monkeypatch.setattr(request_control._user_concurrency, "_limit", 1)
    monkeypatch.setattr(request_control._global_concurrency, "_limit", 10)

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(
        "src.services.chat_service._router.stream",
        _make_gated_stream_mock(release_event, entered_event),
    )

    task_a = asyncio.create_task(async_client.post(
        f"/sessions/{session_a}/chat", json={"content": "slow"}, headers=headers_a,
    ))
    await asyncio.wait_for(entered_event.wait(), timeout=5)

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["ok"]))
        resp_b = await async_client.post(
            f"/sessions/{session_b}/chat", json={"content": "unrelated"}, headers=headers_b,
        )
    assert resp_b.status_code == 200

    release_event.set()
    resp_a = await asyncio.wait_for(task_a, timeout=5)
    assert resp_a.status_code == 200


# ── 14. Rejected requests never touch TCU ────────────────────────────────────

async def test_rejected_request_never_calls_check_and_reserve(async_client, monkeypatch):
    headers = await _register(async_client, "rc-no-tcu@test.com")
    session_id = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]

    # Exhaust the user's rate limit with one real, allowed request first —
    # limit=1 (not 0): production config never allows 0 (config.py's
    # validator requires >= 1), so this exercises the actual boundary a
    # real deployment can reach, not an artificial always-reject state.
    monkeypatch.setattr(request_control, "_user_rate_limit", 1)
    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["warm-up"]))
        warm_up = await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "warm-up"}, headers=headers,
        )
    assert warm_up.status_code == 200

    called = {"count": 0}

    def _spy_check_and_reserve(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("check_and_reserve must not be called for a rejected request")

    monkeypatch.setattr("src.api.routes.sessions.check_and_reserve", _spy_check_and_reserve)

    resp = await async_client.post(
        f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=headers,
    )
    assert resp.status_code == 429
    assert called["count"] == 0


# ── 15. Accepted requests still get correct TCU/usage behavior ─────────────

async def test_accepted_request_still_records_usage(async_client, monkeypatch, db):
    from src.models.usage import UsageRecord

    headers = await _register(async_client, "rc-usage-intact@test.com")
    session_id = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["usage", " check"]))
        resp = await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=headers,
        )
    assert resp.status_code == 200

    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1


# ── 16. Existing endpoints keep working ─────────────────────────────────────

async def test_non_streaming_endpoints_unaffected_by_request_control(async_client):
    headers = await _register(async_client, "rc-endpoints@test.com")
    created = await async_client.post("/sessions", json={}, headers=headers)
    assert created.status_code == 201
    session_id = created.json()["id"]

    listed = await async_client.get("/sessions", headers=headers)
    assert listed.status_code == 200

    detail = await async_client.get(f"/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200

    deleted = await async_client.delete(f"/sessions/{session_id}", headers=headers)
    assert deleted.status_code == 204
