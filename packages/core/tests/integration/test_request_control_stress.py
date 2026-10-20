"""Etapa 5B, section N — local mocked stress test: at least 10, 50, and 100
simultaneous requests, proving Request Control specifically bounds
concurrency under load (not just "the app doesn't crash").

Design note on why this drives `request_control` directly instead of
firing 50-100 genuinely concurrent requests through the full HTTP+SQLite
stack: SQLite (this suite's test database) has its own writer-serialization
behavior that has nothing to do with Request Control — pushing 50-100
truly concurrent DB-writing requests through it hits a SQLite-specific
wall (production runs on PostgreSQL with a real connection pool, already
exercised by the separate Postgres integration suite). Section N's actual
target is Request Control's own admission/rejection/release behavior under
load, so these tests call the exact same process-wide `request_control`
singleton and the exact same three operations sessions.py's
`_enter_request_control()` uses in production — `check_rate_limits()`,
`acquire_concurrency()`, `RequestControlToken.release()` — with each
simulated "request" holding its slot for a short, realistic duration via
asyncio.sleep(), all launched together via asyncio.gather() so all N
genuinely race for capacity at once. A companion test
(`test_10_simultaneous_requests_through_the_real_http_endpoint`) proves the
same guarantee end-to-end through the real /chat endpoint (mocked provider
only) at a scale (10) that stays well clear of the SQLite wall — confirming
the two layers are correctly wired together, not just each in isolation.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from trinity_router import RouterStreamChunk

from src.database import Base, get_db
from src.main import app
from src.services.request_control import RequestRejectedError, request_control

pytestmark = pytest.mark.asyncio


# ── Request-Control-only bursts (no DB/HTTP) — the core of section N ───────

async def _run_control_only_burst(monkeypatch, n: int, cap: int, hold_s: float = 0.05):
    monkeypatch.setattr(request_control._global_concurrency, "_limit", cap)
    monkeypatch.setattr(request_control._user_concurrency, "_limit", cap)
    monkeypatch.setattr(request_control, "_user_rate_limit", n + 10)
    monkeypatch.setattr(request_control, "_ip_rate_limit", n + 10)

    results: list[str] = []
    max_observed = 0
    stop = asyncio.Event()

    async def observer():
        nonlocal max_observed
        while not stop.is_set():
            max_observed = max(max_observed, request_control._global_concurrency.active_count)
            await asyncio.sleep(0.002)

    async def one_simulated_request(i: int):
        try:
            await request_control.check_rate_limits(
                user_id=f"stress-user-{i}", client_ip=f"10.0.{i // 250}.{i % 250}",
            )
            token = request_control.acquire_concurrency(f"stress-user-{i}")
        except RequestRejectedError:
            results.append("rejected")
            return
        try:
            await asyncio.sleep(hold_s)  # simulate an in-flight streaming request
            results.append("accepted")
        finally:
            token.release()

    observer_task = asyncio.create_task(observer())
    await asyncio.gather(*(one_simulated_request(i) for i in range(n)))
    stop.set()
    await observer_task

    accepted = results.count("accepted")
    rejected = results.count("rejected")
    assert accepted + rejected == n
    assert accepted <= cap, f"{accepted} accepted but cap was {cap}"
    assert max_observed <= cap, f"active_count peaked at {max_observed}, above cap {cap}"
    assert request_control._global_concurrency.active_count == 0, "slot leaked after burst drained"
    return accepted, rejected


@pytest.mark.parametrize("n,cap", [(10, 5), (50, 10), (100, 20)])
async def test_request_control_bounds_concurrency_under_a_simultaneous_burst(monkeypatch, n, cap):
    accepted, rejected = await _run_control_only_burst(monkeypatch, n, cap)
    assert accepted >= 1
    assert rejected >= 1, "burst must actually exceed capacity for this to be a meaningful test"


async def test_request_control_accepts_fresh_work_after_a_100_request_burst_drains(monkeypatch):
    await _run_control_only_burst(monkeypatch, n=100, cap=20)

    # Fresh capacity after the burst fully drains — no lingering rejection,
    # no leaked slot from the storm that just happened.
    monkeypatch.setattr(request_control._global_concurrency, "_limit", 50)
    monkeypatch.setattr(request_control._user_concurrency, "_limit", 50)
    await request_control.check_rate_limits(user_id="recovery-user", client_ip="10.0.0.1")
    token = request_control.acquire_concurrency("recovery-user")
    assert request_control._global_concurrency.active_count == 1
    token.release()
    assert request_control._global_concurrency.active_count == 0


# ── One real end-to-end HTTP burst, small enough to avoid the SQLite wall ──

def _make_stream_mock():
    async def _stream(request):
        yield RouterStreamChunk(text="ok")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-sonnet-test",
            input_tokens=3, output_tokens=3, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )
    return _stream


@pytest.fixture
async def async_client(tmp_path):
    """A genuinely concurrent HTTP burst needs each simulated request to get
    its OWN SQLAlchemy Session on its own thread — exactly like production's
    get_db() (src/database.py). The single shared-Session `db` fixture used
    elsewhere in this suite is fine for the low, carefully-orchestrated
    concurrency of other tests, but sharing one Session object across
    genuinely concurrent asyncio.to_thread() calls corrupts its internal
    state (observed directly: SQLAlchemy raises "Session is already
    flushing") — a test-fixture artifact of sharing a Session, not a src/
    bug. File-based SQLite (not :memory:) is used so each thread's own
    connection still sees the same schema/data without needing a single
    shared StaticPool connection, which would reintroduce the same
    bottleneck. Test-only plumbing — no src/ changes; production already
    creates a fresh Session per request against real PostgreSQL.
    """
    db_path = tmp_path / "stress.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    engine.dispose()


async def test_10_simultaneous_requests_through_the_real_http_endpoint(async_client, monkeypatch):
    cap = 5
    n = 10
    monkeypatch.setattr(request_control._global_concurrency, "_limit", cap)
    monkeypatch.setattr(request_control._user_concurrency, "_limit", cap)
    monkeypatch.setattr(request_control, "_user_rate_limit", n + 10)
    monkeypatch.setattr(request_control, "_ip_rate_limit", n + 10)
    monkeypatch.setattr("src.services.chat_service._router.stream", _make_stream_mock())

    accounts = []
    for i in range(n):
        resp = await async_client.post("/auth/register", json={
            "email": f"http-stress-{i}@test.com", "password": "securepass123", "full_name": f"Stress {i}",
        })
        assert resp.status_code == 201
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        session_id = (await async_client.post("/sessions", json={}, headers=headers)).json()["id"]
        accounts.append((headers, session_id))

    async def one(headers, session_id):
        return await async_client.post(
            f"/sessions/{session_id}/chat", json={"content": "burst"}, headers=headers,
        )

    responses = await asyncio.gather(*(one(h, sid) for h, sid in accounts))
    statuses = [r.status_code for r in responses]

    assert all(s in (200, 429) for s in statuses), f"unexpected status codes: {set(statuses)}"
    assert statuses.count(200) <= cap
    assert statuses.count(200) + statuses.count(429) == n
    assert request_control._global_concurrency.active_count == 0
