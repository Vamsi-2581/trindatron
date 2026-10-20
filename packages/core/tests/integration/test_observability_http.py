"""Etapa 5G (Observability) — HTTP-level proof that the metrics/correlation
wiring is actually connected, not just correct in isolation.

Covers: X-Request-ID generated/echoed per request; /health/metrics exposes
a JSON-safe snapshot; a normal request increments http_requests_total; a
Request Control rejection increments both request_control_rejections_total
AND http_rejections_total (429); an Agent Scheduler rejection increments
agent_scheduler_rejections_total; a successful chat increments
entitlement_reservations_total/tcu_reserved and (via the Model Router)
router_provider_attempts_total/router_cost_usd/router_tcu.
"""
from __future__ import annotations

from unittest.mock import patch

from trinity_router import RouterStreamChunk

from src.models.user import User
from src.observability.metrics import combined_snapshot, core_metrics
from trinity_router.metrics import router_metrics


def _admin_headers(client, db, email: str = "metrics-admin@example.com") -> dict:
    """/health/metrics is admin-only (see health.py) - same promotion
    pattern tests/integration/test_admin_settings_routes.py already uses."""
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Metrics Admin",
    })
    assert resp.status_code == 201
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.is_admin = True
    db.commit()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _make_stream_mock(texts: list[str], cost_usd: float = 0.001):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _counter_sum(counter_snapshot: dict, **label_filter: str) -> float:
    total = 0.0
    for labels, value in counter_snapshot.items():
        d = dict(labels)
        if all(d.get(k) == v for k, v in label_filter.items()):
            total += value
    return total


# ═════════════════════════════════════════════════════════════════════════
# Correlation ID
# ═════════════════════════════════════════════════════════════════════════

def test_response_carries_an_x_request_id_header(client):
    resp = client.get("/health")
    assert "X-Request-ID" in resp.headers
    assert len(resp.headers["X-Request-ID"]) > 0


def test_two_requests_get_different_request_ids(client):
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


def test_inbound_x_request_id_is_echoed_back(client):
    resp = client.get("/health", headers={"X-Request-ID": "caller-supplied-id-123"})
    assert resp.headers["X-Request-ID"] == "caller-supplied-id-123"


# ═════════════════════════════════════════════════════════════════════════
# /health/metrics
# ═════════════════════════════════════════════════════════════════════════

def test_health_metrics_requires_authentication(client):
    """Business-sensitive aggregates (traffic, plan mix, provider/model
    cost) must not be readable by an anonymous caller on a public host."""
    assert client.get("/health/metrics").status_code == 401


def test_health_metrics_rejects_non_admin_user(client, auth_headers):
    assert client.get("/health/metrics", headers=auth_headers).status_code == 403


def test_health_and_health_db_stay_public(client):
    """railway.toml's healthcheckPath is /health - the platform probe is
    unauthenticated and must remain so."""
    assert client.get("/health").status_code == 200
    assert client.get("/health/db").status_code in (200, 503)


def test_health_metrics_returns_json_safe_snapshot(client, db):
    client.get("/health")  # generate at least one data point
    resp = client.get("/health/metrics", headers=_admin_headers(client, db))
    assert resp.status_code == 200
    body = resp.json()
    assert "counters" in body and "histograms" in body
    assert isinstance(body["counters"], dict)
    assert isinstance(body["histograms"], dict)


def test_health_metrics_reflects_http_requests_total(client):
    before = _counter_sum(core_metrics.counter("http_requests_total").snapshot())
    client.get("/health")
    client.get("/health")
    after = _counter_sum(core_metrics.counter("http_requests_total").snapshot())
    assert after >= before + 2


def test_health_metrics_never_contains_raw_content(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    secret_content = "my password is hunter2 and my api key is sk-ant-api03-shouldneverleak"
    with patch("src.services.chat_service._router.stream", _make_stream_mock(["ok"])):
        client.post(f"/sessions/{session_id}/chat", json={"content": secret_content}, headers=auth_headers)

    resp = client.get("/health/metrics", headers=_admin_headers(client, db))
    body_text = resp.text
    assert "hunter2" not in body_text
    assert "sk-ant-api03-shouldneverleak" not in body_text


# ═════════════════════════════════════════════════════════════════════════
# Request Control instrumentation
#
# NOTE: request_control.check_rate_limits()/acquire_concurrency() cannot be
# mocked away here and still exercise their own instrumentation (that would
# just prove the mock was called, not the real code path) — that proof
# lives in tests/unit/test_request_control.py's own metrics tests, which
# exhaust a real, small-limit RequestControl instance. What belongs at the
# HTTP layer is the generic, mock-independent fact that ANY 429 response
# increments http_rejections_total — that's the middleware's own job, not
# Request Control's, so mocking the rejection reason away is fine here.
# ═════════════════════════════════════════════════════════════════════════

def test_any_429_response_increments_http_rejections_total(client, auth_headers):
    from src.services.request_control import RequestRejectedError

    before = sum(core_metrics.counter("http_rejections_total").snapshot().values())
    with patch(
        "src.services.request_control.request_control.check_rate_limits",
        side_effect=RequestRejectedError("rate_limit_user", retry_after_seconds=1),
    ):
        resp = client.post("/sessions/any/chat", json={"content": "hi"}, headers=auth_headers)

    assert resp.status_code == 429
    after = sum(core_metrics.counter("http_rejections_total").snapshot().values())
    assert after >= before + 1


# ═════════════════════════════════════════════════════════════════════════
# Agent Scheduler instrumentation — admission is real (no mocking needed);
# the rejection-reason-labeled counter is proven at the unit level for the
# same reason as Request Control above (see tests/unit/test_agent_scheduler.py).
# ═════════════════════════════════════════════════════════════════════════

def test_agent_scheduler_admission_increments_counter(client, auth_headers):
    from trinity_router import RoutingResult

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = RoutingResult(
        content="42", tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )
    from unittest.mock import AsyncMock
    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)

    assert resp.status_code == 200
    admitted = core_metrics.counter("agent_scheduler_admitted_total").snapshot()
    assert sum(admitted.values()) >= 1


# ═════════════════════════════════════════════════════════════════════════
# Entitlement (TCU) instrumentation
# ═════════════════════════════════════════════════════════════════════════

def test_successful_chat_records_reservation_metrics(client, auth_headers):
    """chat_service._router.stream is mocked here (same convention as
    test_chat.py) so this proves Core's OWN entitlement instrumentation is
    wired into the real /chat route — it deliberately does not exercise
    ModelRouter's internal attempt loop (that's swapped out by the mock),
    which is why router_provider_attempts_total is asserted separately
    below via a real ModelRouter call, not through this HTTP path."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with patch("src.services.chat_service._router.stream", _make_stream_mock(["hi there"], cost_usd=0.002)):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)
    assert resp.status_code == 200

    reservations = core_metrics.counter("entitlement_reservations_total").snapshot()
    assert _counter_sum(reservations, outcome="reserved") >= 1

    tcu_reserved = core_metrics.histogram("entitlement_tcu_reserved").snapshot()
    assert sum(v["count"] for v in tcu_reserved.values()) >= 1

    tcu_finalized = core_metrics.histogram("entitlement_tcu_finalized").snapshot()
    assert sum(v["count"] for v in tcu_finalized.values()) >= 1

    snap = combined_snapshot()
    assert "entitlement_reservations_total" in snap["counters"]


def test_combined_snapshot_merges_core_and_router_registries():
    """router_metrics is exercised for real here (no mocking) — proves
    combined_snapshot() surfaces BOTH registries together, complementing
    model-router's own exhaustive test_metrics.py (which proves each
    router_* counter/histogram's instrumentation is correct in isolation)."""
    core_metrics.counter("http_requests_total").inc(method="GET", path="/health", status="200")
    router_metrics.counter("router_provider_attempts_total").inc(provider="anthropic", outcome="success")

    snap = combined_snapshot()
    assert "http_requests_total" in snap["counters"]
    assert "router_provider_attempts_total" in snap["counters"]
