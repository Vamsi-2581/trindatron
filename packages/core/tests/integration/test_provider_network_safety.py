"""CP8 — structural proof that resuming a hosted-provider media job can never
reach a real external network from inside the test suite.

Found in CP7: `main.py`'s `_resume_media_provider_jobs()` ran on every
TestClient boot, against what was then the real `trinity_dev.db`. Had a
Higgsfield job ever been left `status="running"` there with a real
`provider_request_id`, resuming it during a test run would have made a
genuine authenticated call to Higgsfield's API — a real, paid, external side
effect from nothing more than running `pytest`. `test_testing_isolation.py`
proves the first backstop (the lifespan hook never runs at all under
settings.testing). This file proves the SECOND, independent one: even calling
the resume code directly — on purpose, bypassing that guard — with no mocked
HTTP client at all, the exact code path `resume_job()` -> `higgsfield.resume()`
-> a real `httpx.AsyncClient()` uses is intercepted before a socket opens, by
conftest.py's autouse `block_real_network` fixture.
"""
from __future__ import annotations

from src.media import higgsfield
from src.media.engines import ProviderHandle
from src.models.media import MediaJob
from src.services import media_service
from tests.conftest import BlockedRealNetworkCall, _TestingSession


def _a_job_left_running_by_a_previous_process(db, user_id: str) -> MediaJob:
    """Exactly the shape resume_provider_jobs()/resume_job() look for: a job a
    hosted provider accepted whose process died before it saw the answer —
    with a real-looking Higgsfield request id and status/cancel URLs, the same
    fields the real CP4 submission actually recorded."""
    job = MediaJob(
        user_id=user_id, kind="image", operation="generate", status="running", progress=0.5,
        prompt="a lighthouse at dusk",
        params={"width": 1024, "height": 768, "seed": 1, "steps": 1},
        estimated_cost_usd="0.0032",
    )
    db.add(job)
    db.flush()
    handle = ProviderHandle(
        provider=higgsfield.PROVIDER, model="higgsfield-ai/soul/v2/standard", route="soul_2@higgsfield",
        request_id="req-real-looking-1",
        status_url="https://api.higgsfield.ai/requests/req-real-looking-1/status",
        cancel_url="https://api.higgsfield.ai/requests/req-real-looking-1/cancel",
        kind="image",
    )
    job.provider_request_id = handle.request_id
    job.provider_ref = handle.as_dict()
    db.commit()
    db.refresh(job)
    return job


async def test_resuming_a_running_job_with_no_mock_never_reaches_the_real_network(
        db, registered_user, block_real_network, monkeypatch):
    """No client_factory is passed anywhere below — resume_job() falls all the
    way through to higgsfield.resume()'s own default: a genuine
    httpx.AsyncClient() with the library's real transport. If conftest.py's
    network guard were not in place, this test would either hang trying to
    resolve api.higgsfield.ai or (worse, on a machine with real internet and a
    real key) actually poll Higgsfield.

    Fake (never-real) credentials are required here: HiggsfieldClient._auth()
    raises EngineUnavailable before ever building the request when
    higgsfield.credentials() is None, so a genuinely unconfigured environment
    (no HIGGSFIELD_API_KEY at all, e.g. CI) never reaches the network guard's
    interception point either — the job still ends up "failed", but for the
    wrong reason, leaving block_real_network empty.

    monkeypatch.setenv() alone does NOT reliably fix this: media_settings()
    (src/media/settings.py) is decorated with @lru_cache — a process-wide
    singleton, populated on its FIRST call anywhere in the whole test session
    (almost certainly during app/TestClient startup, enumerating providers)
    and never re-read afterward. Setting the env var later, from inside this
    test, changes os.environ but not the already-cached MediaSettings
    instance — credentials() keeps returning whatever it saw the first time.
    Locally this was masked in both directions: a real HIGGSFIELD_API_KEY in
    this machine's own .env got cached early and happened to be non-None too;
    with no real key anywhere, nothing populates the cache before this test's
    own first call, so the env var change (by lucky ordering) was seen after
    all. CI has no key anywhere AND app startup runs first, caching "no
    credentials" before this test ever gets a turn — env var timing that
    can't be relied on either way.

    The fix instead patches the exact function HiggsfieldClient._auth() calls
    — higgsfield.credentials() itself — bypassing media_settings()'s cache
    entirely rather than racing it.
    """
    monkeypatch.setattr(higgsfield, "credentials", lambda: ("test-id", "test-secret"))
    job = _a_job_left_running_by_a_previous_process(db, registered_user["user_id"])

    await media_service.resume_job(job.id, session_factory=_TestingSession)

    db.expire_all()
    resumed = db.query(MediaJob).filter(MediaJob.id == job.id).one()
    # The guard raised before any status was ever read back, so the job could
    # only end up failed — never "completed" with a real answer, never billed.
    assert resumed.status == "failed"
    assert resumed.cost_usd is None
    assert block_real_network, "expected the network guard to have intercepted at least one request"
    assert any("higgsfield.ai" in url for url in block_real_network), block_real_network


async def test_resume_provider_jobs_schedules_but_the_task_itself_still_never_calls_out(
        db, registered_user, block_real_network, monkeypatch):
    """resume_provider_jobs() (the function _resume_media_provider_jobs() calls
    at startup) fires resume_job() as a background asyncio task. Prove the
    same guarantee holds end to end through that scheduling path too, not just
    through a direct resume_job() call.

    Same fake-credentials requirement as the direct-call test above, and the
    same reason a bare monkeypatch.setenv() doesn't reliably work: see that
    test's own docstring — media_settings() is @lru_cache'd process-wide, so
    an env var set from inside a test can lose the race against whatever
    populated the cache first (app startup, in CI). Patching
    higgsfield.credentials() directly — the exact function
    HiggsfieldClient._auth() calls — sidesteps the cache instead of racing it.
    """
    import asyncio

    monkeypatch.setattr(higgsfield, "credentials", lambda: ("test-id", "test-secret"))
    job = _a_job_left_running_by_a_previous_process(db, registered_user["user_id"])

    resumed_count = media_service.resume_provider_jobs(session_factory=_TestingSession)
    assert resumed_count == 1
    for _ in range(200):
        await asyncio.sleep(0)
        db.expire_all()
        if db.query(MediaJob).filter(MediaJob.id == job.id).one().status == "failed":
            break

    db.expire_all()
    assert db.query(MediaJob).filter(MediaJob.id == job.id).one().status == "failed"
    assert any("higgsfield.ai" in url for url in block_real_network), block_real_network


def test_block_real_network_actually_blocks_a_plain_httpx_call(block_real_network):
    """Sanity check on the guard itself, independent of any media/provider code:
    a bare httpx.Client() hitting a real host raises, loopback does not."""
    import httpx
    import pytest

    with pytest.raises(BlockedRealNetworkCall):
        httpx.Client().get("https://example.com")
    assert any("example.com" in url for url in block_real_network)

    # Loopback is never blocked — but nothing listens on this port, so the
    # (permitted) attempt fails with a connection error instead, proving the
    # guard let it past rather than intercepting it.
    with pytest.raises(httpx.ConnectError):
        httpx.Client().get("http://127.0.0.1:1")
