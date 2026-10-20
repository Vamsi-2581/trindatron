import os

# CP8: must be set before ANY `src.*` import below — src/config.py's Settings
# singleton reads it once, at import time, and everything downstream keys off
# that one value:
#   * src/database.py's _build_engine() ignores DATABASE_URL entirely and
#     builds the isolated in-memory engine `_engine` below points at too
#     (see its own docstring for why tests must never open trinity_dev.db);
#   * src/main.py's lifespan skips every startup reconciliation call and every
#     recurring background loop (execution worker, agent scheduler, payment
#     order expiry, webhook nonce cleanup, entitlement reservation reclaim,
#     artifact retention, media provider resume) — see test_testing_isolation.py
#     for the tests that prove both of these hold.
os.environ.setdefault("TESTING", "true")

import pytest
from fastapi.testclient import TestClient

from src.database import Base, SessionLocal, engine, get_db
from src.main import app
from src.observability.metrics import reset_all as _reset_observability_metrics
from src.services.agent_scheduler import agent_scheduler
from src.services.request_control import request_control

# CP8: no separate test engine any more — src.database.engine (built by
# _build_engine() under settings.testing) already IS the isolated in-memory
# database. `_engine`/`_TestingSession` keep their historical names (several
# integration tests import `_TestingSession` from this module by name to get a
# session_factory for media_service.run_job/resume_job/resume_provider_jobs)
# but now bind to that one real engine instead of a private duplicate.
_engine = engine
_TestingSession = SessionLocal

# CP8 — network safety net (independent of, and in addition to, settings.testing
# skipping the lifespan's own provider-resume call): every hosted media/voice
# adapter (Higgsfield, fal, xai, Veo, OpenAI images, ElevenLabs, Groq, Gemini —
# src/media/hosted.py's default_client(), src/media/higgsfield.py's
# HiggsfieldClient) opens a plain httpx.AsyncClient() with no custom transport,
# so httpx builds its own default one, which opens sockets through httpcore's
# AutoBackend -> AnyIOBackend (async) / SyncBackend (sync) — the two places that
# actually call socket/anyio connect_tcp. Patched one layer BELOW httpx's own
# transport classes on purpose: src/capability/web_research/guard.py's own
# guarded_transport() is a real httpx.AsyncHTTPTransport too (that's how it stays
# a drop-in transport for httpx), just wired to a *custom* httpcore backend
# (PublicOnlyBackend) that in its own tests wraps a fully fake resolver/backend,
# never AnyIOBackend/SyncBackend. Patching at the httpx-transport level would
# have also intercepted that — production SSRF-guard code under test, not an
# accidental real call. Patching here instead means only a genuinely unmocked
# connection attempt is ever seen: httpx.MockTransport (every provider adapter
# test) and any test-provided fake resolver/backend (guard.py's own tests) never
# reach AnyIOBackend/SyncBackend at all.
from httpcore import AnyIOBackend, SyncBackend

_ALLOWED_NETWORK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_real_async_connect_tcp = AnyIOBackend.connect_tcp
_real_sync_connect_tcp = SyncBackend.connect_tcp


class BlockedRealNetworkCall(RuntimeError):
    """Raised instead of letting a test open a real TCP connection to a non-loopback host."""


def _guard_or_raise(host: str, port: int, blocked: list[str]) -> None:
    if host in _ALLOWED_NETWORK_HOSTS:
        return
    blocked.append(f"{host}:{port}")
    raise BlockedRealNetworkCall(
        f"Test attempted a real network connection to {host}:{port}. External providers "
        "must be mocked (httpx.MockTransport / an injected client_factory / a fake "
        "resolver+backend), never reached for real from the test suite."
    )


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Structural proof, not a convention: NOTHING in the test suite can complete
    a real TCP connection to anything but loopback. Applies to every test
    automatically (autouse) — see test_provider_network_safety.py for the tests
    that exercise it directly against media_service.resume_job()/higgsfield.py,
    and test_testing_isolation.py for the TestClient-boot-makes-no-call proof.
    Yields the list of blocked attempts so a test can assert on *what* was
    blocked, not just that nothing blew up.
    """
    blocked: list[str] = []

    async def guarded_async_connect_tcp(self, host, port, *args, **kwargs):
        _guard_or_raise(host, port, blocked)
        return await _real_async_connect_tcp(self, host, port, *args, **kwargs)

    def guarded_sync_connect_tcp(self, host, port, *args, **kwargs):
        _guard_or_raise(host, port, blocked)
        return _real_sync_connect_tcp(self, host, port, *args, **kwargs)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", guarded_async_connect_tcp)
    monkeypatch.setattr(SyncBackend, "connect_tcp", guarded_sync_connect_tcp)
    yield blocked


@pytest.fixture(autouse=True)
def _reset_request_control():
    """Etapa 5B: request_control is a process-wide singleton (same pattern
    as _router/_browser_governor), so its rate-limit windows and
    concurrency counters would otherwise accumulate across every test in
    the whole pytest session — eventually rejecting unrelated later tests
    with a 429 that has nothing to do with what they're testing. Purely
    additive: does not touch the db/client fixtures below."""
    request_control.reset()
    yield
    request_control.reset()


@pytest.fixture(autouse=True)
def _reset_agent_scheduler():
    """Etapa 5D: agent_scheduler is a process-wide singleton with the exact
    same cross-test-leakage risk request_control had (see
    _reset_request_control above) — its global/per-user concurrency
    counters must not accumulate across tests."""
    agent_scheduler.reset()
    yield
    agent_scheduler.reset()


@pytest.fixture(autouse=True)
def _reset_observability_metrics_fixture():
    """Etapa 5G: core_metrics/router_metrics are process-wide singletons
    with the exact same cross-test-leakage risk as request_control/
    agent_scheduler above — their counters/histograms must not accumulate
    across tests."""
    _reset_observability_metrics()
    yield
    _reset_observability_metrics()


@pytest.fixture(autouse=True)
def _enable_code_workspace_arbitrary_root():
    """Security Hardening (SANDBOX-01): code_workspace_arbitrary_root_enabled
    defaults to False (safe-by-default for the hosted multi-tenant
    deployment — see config.py's own docstring). The EXISTING test suite's
    own convention (tests/integration/test_projects.py,
    test_code_execute_route.py, test_code_task.py, test_code_sandbox_real.py,
    etc.) predates this flag and freely creates CodeWorkspaces with an
    arbitrary root_path (a tmp_path directory standing in for "the user's
    own local project checkout") — matching the flag's own intended
    desktop/local-dev use case. Enabled here for the whole suite so none
    of those pre-existing tests need editing; tests that specifically
    prove the production-safe-default behavior (test_sandbox_ssrf_audit.py)
    explicitly disable it for themselves instead."""
    from src.config import settings
    settings.code_workspace_arbitrary_root_enabled = True
    yield
    settings.code_workspace_arbitrary_root_enabled = False


@pytest.fixture(autouse=True)
def _no_leaked_plan_entitlement_patch(request):
    """A test must leave every module's get_plan_entitlement as it found it.

    Plan entitlements gate money (continuity debits, TCU windows). A patch that
    survives its test silently rewrites them for the rest of the suite: a
    mock.patch entered in two threads at once once left a fake 10-TCU START
    entitlement installed, so FREE later debited 0 TCU and MAX/TEAM were
    refused — ten unrelated tests failed, depending on thread timing. This
    catches such a leak at the test that caused it, restores the real function
    so nothing after it is affected, and fails that test by name.
    """
    import sys

    from src.entitlements import get_plan_entitlement as real

    yield
    leaked = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("src.") or module is None:
            continue
        current = getattr(module, "get_plan_entitlement", real)
        if current is not real and getattr(current, "__module__", "") != "src.entitlements":
            leaked.append(name)
            setattr(module, "get_plan_entitlement", real)
    if leaked:
        pytest.fail(f"{request.node.nodeid} left get_plan_entitlement patched in: {', '.join(sorted(leaked))}")


@pytest.fixture(autouse=True)
def _enable_legacy_client_confirm():
    """billing_client_confirm_enabled defaults to False: an order with no
    payment adapter has no evidence it was paid. The billing suite predates
    that and confirms "test_provider" orders from the client throughout, so
    the legacy flow stays on for it; tests/integration/test_billing_unpaid_confirm.py
    turns it off to prove the production default."""
    from src.config import settings
    settings.billing_client_confirm_enabled = True
    yield
    settings.billing_client_confirm_enabled = False


@pytest.fixture(autouse=True)
def _reset_auth_login_rate_limiters():
    """Security Hardening: src/api/routes/auth.py's own
    _login_ip_rate_limiter/_login_email_rate_limiter are module-level
    singletons (same convention as billing.py's _order_rate_limiter/
    _webhook_rate_limiter) — global here rather than file-local since
    multiple test files across the suite call POST /auth/login."""
    from src.api.routes import auth as auth_routes
    from src.config import settings
    from src.services.request_control import InMemoryRateLimiter
    auth_routes._login_ip_rate_limiter = InMemoryRateLimiter()
    auth_routes._login_email_rate_limiter = InMemoryRateLimiter()
    auth_routes._register_ip_rate_limiter = InMemoryRateLimiter()
    auth_routes._refresh_ip_rate_limiter = InMemoryRateLimiter()
    from src.api.routes import auth_account as account_routes
    account_routes._email_ip_rate_limiter = InMemoryRateLimiter()
    account_routes._email_address_rate_limiter = InMemoryRateLimiter()
    account_routes._change_password_rate_limiter = InMemoryRateLimiter()
    # Every TestClient request comes from the same address, and many tests
    # register several accounts. The signup limit is not what they exercise;
    # tests/integration/test_free_plan_protection.py sets a low value itself.
    original_register_limit = settings.auth_register_ip_rate_limit
    settings.auth_register_ip_rate_limit = 10_000
    yield
    settings.auth_register_ip_rate_limit = original_register_limit
    auth_routes._login_ip_rate_limiter = InMemoryRateLimiter()
    auth_routes._login_email_rate_limiter = InMemoryRateLimiter()
    auth_routes._register_ip_rate_limiter = InMemoryRateLimiter()


@pytest.fixture(scope="function", autouse=False)
def db():
    Base.metadata.create_all(bind=_engine)
    session = _TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_engine)


@pytest.fixture(scope="function")
def client(db):
    """CP8: `override_get_db` no longer hands out the literal `db` fixture
    Session — it creates a fresh one per call, exactly like the real
    src.database.get_db() already does (settings.testing points SessionLocal at
    the same isolated in-memory engine `db` above uses). So a request's session
    and the test's own `db` session are always distinct Python objects sharing
    one underlying SQLite connection (StaticPool) — never the same mutable
    Session handed to both the pytest thread and the TestClient's internal ASGI
    thread. That shared, non-thread-safe Session was the root cause of the
    non-deterministic test_admin_economics.py failure found in CP7 (see
    test_testing_isolation.py for the regression test). A test still sees its
    own `db.add()`/`db.commit()` reflected in the HTTP response because both
    sessions read the same physical connection — it just never sees them
    through the SAME Session object the request used.

    The override itself stays registered (functionally identical to get_db, so
    this is not a behavior change) because a handful of existing tests reach
    into `client.app.dependency_overrides[get_db]()` directly to get a session
    on the test database from outside a request (e.g. to seed data through a
    service class before a later HTTP call) — removing the key outright broke
    them with a bare KeyError.
    """
    def override_get_db():
        session = _TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def registered_user(client, db):
    """A START subscriber — the customer most of this suite exercises.

    Signup itself lands on FREE (see tests/integration/test_free_plan_protection.py).
    Until 2026-09-13 it silently landed on START, and the tests using this
    fixture — agent loops, CODE, memories — were written against that. They
    test paid features, so the plan is now set explicitly, the way a
    confirmed payment would set it, instead of relying on the bug.
    """
    resp = client.post("/auth/register", json={
        "email": "trinity@test.com",
        "password": "securepass123",
        "full_name": "Trinity Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    from src.models.user import User
    user = db.query(User).filter(User.id == data["user_id"]).one()
    user.plan = "start"
    db.commit()
    return {"token": data["access_token"], "user_id": data["user_id"], "email": data["email"]}


@pytest.fixture
def auth_headers(registered_user):
    return {"Authorization": f"Bearer {registered_user['token']}"}


@pytest.fixture
def db2_user_factory(db):
    """Create additional users directly via the ORM (no password hashing,
    no HTTP round-trip) — for tests that only need a second/third distinct
    user_id to prove cross-user isolation, not a real login flow."""
    from src.models.user import User

    def _make(email: str, *, plan: str = "pro"):
        user = User(email=email, hashed_password="x", full_name="Test User", plan=plan, timezone="UTC")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _make


@pytest.fixture
def execution_session_override():
    """Durable Execution: execution_worker.py's poll loop, and any
    ExecutionProvider that opens its own session (ModelRouterExecutionProvider),
    run in a background asyncio.Task that outlives the request, so neither
    can receive a session via Depends(get_db) (which the `client` fixture
    above overrides per-test). This fixture points execution_service's ONE
    overridable session factory (`_session_factory`, imported by
    execution_worker.py too — see that module's docstring) at the SAME
    test engine/sessionmaker `db`/`client` use, so a background write is
    visible to the test's assertions. Restores the real factory after the
    test — opt-in (not autouse) so every other test is completely
    unaffected."""
    from src.services import execution_service

    original = execution_service._session_factory
    execution_service._session_factory = _TestingSession
    yield
    execution_service._session_factory = original


@pytest.fixture
def run_worker_until_terminal(execution_session_override):
    """Durable Execution test helper: repeatedly runs one worker poll
    cycle (execution_worker.run_worker_once()) until `job_id` reaches a
    terminal status or `timeout` elapses — the test-side equivalent of
    "wait for the durable worker to pick this up, possibly across several
    retry cycles". Requires execution_session_override (declared as a
    dependency here, not just via module-level pytestmark, so this helper
    is self-contained even if a test file forgets the marker).

    Unlike Execution Runtime V1's `_wait_for_job()` (which awaited one
    specific asyncio.Task the SAME create_job() call had already spawned),
    this drives the ACTUAL claim/execute/release cycle the durable worker
    uses in production — including picking a job back up after it was
    released for retry (next_run_at in the future), exactly like a real
    poll loop would, just called directly instead of waiting on a real
    timer (retry tests additionally shrink execution_worker._BACKOFF_BASE_SECONDS
    so a released job's next_run_at is already due well within `timeout`).
    """
    import asyncio
    import time as _time

    from src.models.execution import EXECUTION_TERMINAL_STATUSES, ExecutionJob
    from src.services import execution_worker

    async def _run(db, job_id: str, *, timeout: float = 10.0, poll_sleep: float = 0.02):
        deadline = _time.monotonic() + timeout
        while True:
            await execution_worker.run_worker_once()
            db.expire_all()
            job = db.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
            if job is not None and job.status in EXECUTION_TERMINAL_STATUSES:
                return job
            if _time.monotonic() >= deadline:
                return job
            await asyncio.sleep(poll_sleep)

    return _run


@pytest.fixture
def client_with_production_loops(db, monkeypatch):
    """CP8: main.py's lifespan no longer starts any of the seven production
    background loops for a normal `client` (see the module-level comment above
    that fixture on why — CP7's root-cause race). One test deliberately needs
    the real, timer-driven exception:
    tests/integration/test_execution_research_job.py::test_R12_http_route_e2e_auth_and_polling
    tests the ACTUAL end-to-end path (HTTP create -> the real background
    execution worker claims and runs it -> HTTP polling observes 'completed'),
    on purpose not using run_worker_until_terminal's manual single-step drive.

    A plain `def` test (not `async def`, since it polls via synchronous
    `client.get()` calls) cannot itself drive a pytest-asyncio-scoped
    asyncio.Task forward between calls — nothing would pump that loop while the
    test body blocks. So instead of creating a task on a loop nothing drives,
    this fixture flips settings.testing to False just long enough for a FRESH
    TestClient's lifespan to start for real, the exact same way a real process
    would: every one of the seven loops runs on TestClient's own internal ASGI
    thread/loop, which already reliably interleaves with the test's synchronous
    `client.get()` calls (the pre-CP8 behavior this one test always relied on).
    This is still fully isolated: src.database.engine was already built once,
    at import time, from settings.testing being true at THAT moment, and never
    changes — so every loop this starts still only ever touches the isolated
    in-memory test database, and the autouse network guard (which does not key
    off settings.testing at all) still applies.
    """
    from src.config import settings

    monkeypatch.setattr(settings, "testing", False)

    def override_get_db():
        session = _TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
