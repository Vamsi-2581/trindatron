"""From "create a presentation" to a file the right person can download.

The previous suite (test_presentation_capability.py) covers classification,
routing and the engine handshake. This one covers everything that had to
exist for a deck to actually reach a user: object storage, the reservation
that is sized to the deck rather than to a flat per-request figure, the
gateway that keeps the model choice and the API key on our side, ownership
on download, and retention.

Where a real credential is required, the test says so instead of asserting a
fiction: there are no R2 credentials in this environment, so R2 is tested at
the layer that can be proven offline — the exact bytes it signs — and the
local store carries the end-to-end paths.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.config import settings
from src.models.execution import ExecutionArtifact, ExecutionJob
from src.models.presentation_llm_call import PresentationLlmCall
from src.models.user import User
from src.presentation.cost import USD_PER_TCU, estimate
from src.presentation.image_policy import decide as decide_images
from src.presentation.presenton import PresentonProvider, engine_environment
from src.presentation.provider import ImageStrategy
from src.services import artifact_service
from src.services.entitlement_service import check_and_reserve, remaining_budget_tcu
from src.storage.local import LocalObjectStore
from src.storage.object_store import ObjectStoreError
from src.storage.r2 import R2ObjectStore

FAKE_PPTX = b"PK\x03\x04" + b"\x00" * 512


def _TestingSessionFactory(db):
    """The streaming path writes through _session_factory rather than the
    request-scoped session (a StreamingResponse outlives the request). Point
    it at the test's own session so the row is visible to assertions."""
    return lambda: db


# ── Object storage ───────────────────────────────────────────────────────

@pytest.fixture
def local_store(tmp_path):
    return LocalObjectStore(root=str(tmp_path / "objects"))


async def test_local_store_round_trips_bytes(local_store):
    assert await local_store.is_available()
    stored = await local_store.put(
        key="presentations/u1/j1/deck.pptx", data=FAKE_PPTX, content_type="application/x"
    )
    assert stored.size_bytes == len(FAKE_PPTX)
    assert stored.backend == "local"
    assert await local_store.get(key="presentations/u1/j1/deck.pptx") == FAKE_PPTX


async def test_local_store_has_no_signed_url(local_store):
    """Returning None is the supported answer, not a failure — the download
    route streams when there is no direct URL. A store that fabricated a URL
    here would diverge silently from production."""
    await local_store.put(key="a/b.pptx", data=FAKE_PPTX, content_type="application/x")
    assert await local_store.signed_url(key="a/b.pptx", expires_in=300) is None


async def test_local_store_refuses_a_key_that_escapes_its_root(local_store):
    with pytest.raises(ObjectStoreError):
        await local_store.put(
            key="../../../etc/passwd", data=b"x", content_type="text/plain"
        )


async def test_local_store_delete_is_idempotent(local_store):
    await local_store.is_available()
    await local_store.delete(key="never/written.pptx")  # must not raise


def test_r2_signs_a_presigned_url_without_leaking_the_secret():
    """R2 cannot be round-tripped here (no credentials), so what is asserted
    is the thing that would actually be wrong if the signer were wrong: the
    URL's shape, its expiry, and the absence of the secret."""
    store = R2ObjectStore(
        endpoint="https://acct.r2.cloudflarestorage.com",
        bucket="decks",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="SECRETEXAMPLEKEY",
    )
    url = asyncio.run(
        store.signed_url(key="presentations/u1/j1/deck.pptx", expires_in=300,
                         filename="Quarterly Review.pptx")
    )
    assert url.startswith("https://acct.r2.cloudflarestorage.com/decks/presentations/")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=300" in url
    assert "SECRETEXAMPLEKEY" not in url
    # The download filename is inside the signature, so a recipient cannot
    # rewrite it on a URL they were handed.
    assert "response-content-disposition" in url


def test_r2_is_unavailable_when_unconfigured():
    store = R2ObjectStore(endpoint="", bucket="", access_key_id="", secret_access_key="")
    assert asyncio.run(store.is_available()) is False


# ── The reservation is sized to the deck ─────────────────────────────────

def _user(db, plan="pro"):
    user = User(
        email=f"deck-{plan}-{datetime.now().timestamp()}@example.com",
        hashed_password="x", plan=plan, timezone="America/Sao_Paulo",
    )
    db.add(user)
    db.commit()
    return user


def test_a_deck_reserves_its_estimate_not_the_flat_per_request_figure(db):
    """The whole economic point. PRO's flat per-request figure is 8 TCU and a
    10-slide deck estimates well above that; reserving 8 would under-reserve
    by a factor the ADMIT+CLAMP pass exists to prevent."""
    user = _user(db, "pro")
    est = estimate(n_slides=10, image_strategy=ImageStrategy.STOCK_FIRST)
    assert est.tcu > 8.0, "test is meaningless if the deck is cheaper than the flat figure"

    reservation_id = check_and_reserve(
        db, user_id=user.id, session_id=None, plan="pro",
        user_timezone=user.timezone, estimated_tcu=est.tcu,
    )
    from src.models.usage import UsageRecord

    reserved = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert float(reserved.tcu) == pytest.approx(est.tcu, rel=1e-6)


def test_an_estimate_larger_than_the_balance_is_clamped_to_the_balance(db):
    """ADMIT + CLAMP still applies: a large estimate never reserves more than
    the user can actually spend, so a concurrent request reads a true figure.

    The binding window here is the DAILY one, not the session: the seeded
    spend predates the session that this reservation starts, so it falls
    outside the 5-hour window and inside the day. Asserting against
    remaining_budget_tcu() rather than a hand-computed number keeps the test
    about the invariant — reserve no more than what is genuinely left —
    instead of about which window happens to bind.
    """
    user = _user(db, "start")
    from src.models.usage import UsageRecord

    # created_at is set explicitly, an hour back. Left to default it lands
    # within the same second as the session this reservation starts, and
    # whether it counts against the 5-hour window or only the daily one
    # becomes a sub-second race — which would make the test flaky rather
    # than wrong. An hour ago is unambiguously outside the new session and
    # inside the day, so the daily window is deterministically the binding
    # one.
    db.add(UsageRecord(
        user_id=user.id, session_id=None, message_id=None, model="m",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.28, tcu=28.0, duration_ms=0, status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    ))
    db.commit()

    remaining = remaining_budget_tcu(db, user.id, "start", user.timezone)
    assert remaining is not None and remaining < 500.0

    reservation_id = check_and_reserve(
        db, user_id=user.id, session_id=None, plan="start",
        user_timezone=user.timezone, estimated_tcu=500.0,
    )
    reserved = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert float(reserved.tcu) == pytest.approx(remaining, rel=1e-6)
    assert float(reserved.tcu) < 500.0


def test_omitting_the_estimate_keeps_the_old_flat_behaviour(db):
    """Chat and agent turns cannot predict their cost and must be unaffected
    by this parameter existing."""
    user = _user(db, "pro")
    from src.entitlements import get_plan_entitlement
    from src.models.usage import UsageRecord

    reservation_id = check_and_reserve(
        db, user_id=user.id, session_id=None, plan="pro", user_timezone=user.timezone,
    )
    reserved = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert float(reserved.tcu) == pytest.approx(get_plan_entitlement("pro").max_tcu_per_request)


def test_remaining_budget_counts_pending_reservations(db):
    """A preflight that ignored pending rows would let two decks start on the
    strength of the same budget."""
    user = _user(db, "pro")
    before = remaining_budget_tcu(db, user.id, "pro", user.timezone)
    check_and_reserve(
        db, user_id=user.id, session_id=None, plan="pro",
        user_timezone=user.timezone, estimated_tcu=20.0,
    )
    after = remaining_budget_tcu(db, user.id, "pro", user.timezone)
    assert after == pytest.approx(before - 20.0, abs=1e-6)


def test_remaining_budget_does_not_start_a_session_window(db):
    """Asking what you could afford must not start the 5-hour clock."""
    from src.models.entitlement_session import EntitlementSession

    user = _user(db, "pro")
    remaining_budget_tcu(db, user.id, "pro", user.timezone)
    assert db.query(EntitlementSession).filter(
        EntitlementSession.user_id == user.id
    ).first() is None


def test_free_plan_has_no_presentations():
    from src.services import presentation_service as ps

    assert ps.plan_allows_presentations("free") is False
    assert ps.plan_allows_presentations("pro") is True


# ── Image policy: stock first, generation only where it is earned ────────

@pytest.mark.parametrize("plan", ["start", "pro"])
def test_plans_that_cannot_afford_generation_never_get_it(plan):
    d = decide_images(plan=plan, content="a futuristic sci-fi mascot concept illustration")
    assert d.strategy is ImageStrategy.STOCK_FIRST
    assert d.downgraded_for_plan is True


def test_a_photographable_subject_uses_stock_even_on_the_top_plan():
    d = decide_images(plan="max", content="Q3 sales results for our team in the São Paulo office")
    assert d.strategy is ImageStrategy.STOCK_FIRST
    assert d.downgraded_for_plan is False


def test_an_unphotographable_subject_earns_generation_on_a_plan_that_can_pay():
    d = decide_images(plan="max", content="a diagram of our execution pipeline architecture")
    assert d.strategy is ImageStrategy.GENERATED


def test_asking_for_no_images_is_always_honoured():
    d = decide_images(plan="start", content="anything at all",
                      requested=ImageStrategy.NONE)
    assert d.strategy is ImageStrategy.NONE


@pytest.mark.parametrize("brief", [
    "an abstract of our Q3 results for the board",
    "um resumo abstrato dos resultados do trimestre",
    "a summary report on product growth this quarter",
])
def test_business_prose_does_not_accidentally_buy_generated_images(brief):
    """"Abstract" is a business noun far more often than an art style, and a
    false positive here multiplies a deck's cost by 13. The word only counts
    when it is actually qualifying an image ("an abstract background"), which
    the next test pins from the other side."""
    assert decide_images(plan="max", content=brief).strategy is ImageStrategy.STOCK_FIRST


@pytest.mark.parametrize("brief", [
    "a deck with an abstract background and bold type",
    "um fundo abstrato para os slides",
])
def test_an_actual_request_for_abstract_imagery_still_generates(brief):
    """The tightening must not have removed the real signal."""
    assert decide_images(plan="max", content=brief).strategy is ImageStrategy.GENERATED


def test_generation_costs_an_order_of_magnitude_more_than_stock():
    """The number the whole policy exists to avoid spending by accident."""
    stock = estimate(n_slides=10, image_strategy=ImageStrategy.STOCK_FIRST)
    generated = estimate(n_slides=10, image_strategy=ImageStrategy.GENERATED)
    assert generated.tcu > stock.tcu * 10


# ── Engine environment: the Gemini mapping that is easy to get wrong ─────

def test_engine_environment_pins_the_model_and_never_leaves_it_to_a_default():
    """Presenton's GOOGLE_MODEL default is models/gemini-2.0-flash and its
    image setting "gemini_flash" is a different model again — neither is what
    TRINDATRON selected, so the model must be stated explicitly."""
    env = engine_environment(
        strategy=ImageStrategy.STOCK_FIRST,
        llm_base_url="http://core:8000/internal/presenton/llm/v1",
        llm_gateway_token="tok",
        model="models/gemini-3.8-flash",
    )
    assert env["LLM"] == "custom"
    assert env["CUSTOM_MODEL"] == "models/gemini-3.8-flash"
    assert "GOOGLE_API_KEY" not in env, "the Gemini key must never reach the engine"
    assert "GOOGLE_MODEL" not in env


def test_engine_environment_keeps_search_native_and_adds_no_vendor():
    env = engine_environment(
        strategy=ImageStrategy.STOCK_FIRST, llm_base_url="u", llm_gateway_token="t",
        model="m", web_search=True,
    )
    assert env["WEB_SEARCH_PROVIDER"] == "native"
    for forbidden in ("TAVILY_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "SEARXNG_URL"):
        assert forbidden not in env


def test_engine_environment_disables_parallel_image_generation():
    """Measured during the audit: six concurrent image calls returned 0/6
    successes where the same six in sequence returned 5/6."""
    env = engine_environment(
        strategy=ImageStrategy.GENERATED, llm_base_url="u", llm_gateway_token="t", model="m",
    )
    assert env["ENABLE_PARALLEL_IMAGE_GENERATION"] == "false"


def test_engine_environment_locks_the_engine_ui_out_of_key_changes():
    env = engine_environment(
        strategy=ImageStrategy.STOCK_FIRST, llm_base_url="u", llm_gateway_token="t", model="m",
    )
    assert env["CAN_CHANGE_KEYS"] == "false"


# ── The gateway: Gemini only, key on our side, usage recorded ────────────

class _Gemini(BaseHTTPRequestHandler):
    mode = "ok"
    #: How many calls answer 503 "high demand" before the mode takes over.
    busy_first = 0
    seen_models: list[str] = []
    seen_auth: list[str] = []
    seen_bodies: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen_models.append(body.get("model"))
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        type(self).seen_bodies.append(body)

        if type(self).busy_first > 0:
            # Google's answer during a demand spike, seen in production on 2026-09-30.
            type(self).busy_first -= 1
            raw = json.dumps([{"error": {"code": 503, "status": "UNAVAILABLE",
                                         "message": "This model is currently experiencing high demand."}}]).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if type(self).mode == "stream":
            # The shape Google actually returns for a streamed completion:
            # content deltas, then a final usage-only chunk, then [DONE].
            chunks = [
                {"choices": [{"delta": {"content": "Slide "}}]},
                {"choices": [{"delta": {"content": "one"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1200, "completion_tokens": 300,
                           "total_tokens": 1500}},
            ]
            raw = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            encoded = raw.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        if type(self).mode == "quota":
            payload, code = {"error": {"message": "quota"}}, 429
        elif type(self).mode == "hidden_reasoning":
            # The exact shape measured live: thinking tokens appear in
            # NEITHER completion_tokens NOR completion_tokens_details.
            payload, code = {
                "choices": [{"message": {"content": ""}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 58},
            }, 200
        else:
            payload, code = {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "completion_tokens_details": {"reasoning_tokens": 1250},
                },
            }, 200
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake_gemini(monkeypatch):
    _Gemini.mode = "ok"
    _Gemini.busy_first = 0
    _Gemini.seen_models = []
    _Gemini.seen_auth = []
    _Gemini.seen_bodies = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(
        "src.api.routes.presenton_gateway.GEMINI_OPENAI_BASE", base, raising=False
    )
    yield _Gemini
    srv.shutdown()


def test_gateway_rejects_a_request_without_the_shared_token(client, monkeypatch):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "right-token")
    r = client.post("/internal/presenton/llm/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 401


def test_gateway_is_closed_when_no_token_is_configured(client, monkeypatch):
    """An unauthenticated proxy to a paid model API is the worst default, so
    an unconfigured gateway is absent rather than open."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "")
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"model": "x"}, headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 404


def test_gateway_forces_our_gemini_model_whatever_the_engine_asks_for(
    client, monkeypatch, fake_gemini, db
):
    """"Gemini only" has to be enforced, not merely configured: there is no
    request body that can reach another model through this endpoint."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "server-side-key")
    monkeypatch.setattr(settings, "presentation_model", "models/gemini-3.8-flash")

    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert fake_gemini.seen_models == ["models/gemini-3.8-flash"]


def test_gateway_uses_the_server_side_key_not_the_engines(
    client, monkeypatch, fake_gemini
):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "engine-token")
    monkeypatch.setattr(settings, "gemini_api_key", "server-side-key")
    client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer engine-token"},
    )
    assert fake_gemini.seen_auth == ["Bearer server-side-key"]
    # The engine's own token never travels upstream.
    assert "engine-token" not in fake_gemini.seen_auth[0]


def test_gateway_records_real_usage_including_reasoning_tokens(
    client, monkeypatch, fake_gemini, db
):
    """Reasoning is billed at the OUTPUT rate and is not part of
    completion_tokens. Folding it in at the input rate would understate a
    thinking model's cost roughly fivefold."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    row = db.query(PresentationLlmCall).order_by(
        PresentationLlmCall.created_at.desc()
    ).first()
    assert row is not None
    assert row.input_tokens == 1000
    assert row.output_tokens == 500
    assert row.reasoning_tokens == 1250
    # 1000 in @ $0.75/M + (500 + 1250) out @ $3.75/M
    assert row.cost_usd == pytest.approx(0.00075 + 0.0065625, rel=1e-6)


def test_gateway_reconstructs_reasoning_tokens_hidden_in_the_total(
    client, monkeypatch, fake_gemini, db
):
    """Measured live on 2026-09-11 against Google's OpenAI-compatible endpoint:
    prompt_tokens=10, completion_tokens=0, no reasoning field at all, and
    total_tokens=58. Those 48 tokens are real thinking tokens billed at the
    OUTPUT rate, so trusting the explicit fields under-reports that call by
    roughly 25x.

    The remainder is reconstructed from the total. This test uses that exact
    response shape, because a synthetic one with a tidy reasoning field would
    not have caught the bug.
    """
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "hidden_reasoning"

    client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    row = db.query(PresentationLlmCall).order_by(
        PresentationLlmCall.created_at.desc()
    ).first()
    assert row.input_tokens == 10
    assert row.output_tokens == 0
    assert row.reasoning_tokens == 48, "the unexplained remainder must be billed"
    assert row.cost_usd == pytest.approx(10 / 1e6 * 0.75 + 48 / 1e6 * 3.75, rel=1e-6)


def test_gateway_never_invents_usage_beyond_the_total(
    client, monkeypatch, fake_gemini, db
):
    """Reconstruction only ever adds cost that was otherwise ignored — it must
    not inflate a response whose fields already add up."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    row = db.query(PresentationLlmCall).order_by(
        PresentationLlmCall.created_at.desc()
    ).first()
    # The default fake reports 1000 + 500 + 1250 explicitly and no total;
    # nothing is added on top of that.
    assert row.reasoning_tokens == 1250


def test_gateway_streams_when_the_engine_asks_for_streaming(
    client, monkeypatch, fake_gemini, db
):
    """The engine really does stream — measured live on 2026-09-11 it sends
    stream=true, stream_options and response_format=json_schema.

    An earlier version stripped `stream` to keep usage visible, and the
    failure was silent and expensive: Google answered 200 and billed the
    tokens, the engine's SSE parser accumulated nothing from a non-streamed
    body, and generation died on an empty outline. So the bytes must come
    back as SSE, framed exactly as the upstream sent them.
    """
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr("src.api.routes.presenton_gateway._session_factory",
                        _TestingSessionFactory(db), raising=False)
    fake_gemini.mode = "stream"

    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [], "stream": True},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "data: " in body and body.rstrip().endswith("[DONE]")
    # Frames must survive intact — the engine parses these with its own
    # OpenAI client, so reformatting here is a parse failure there.
    assert "\n\n" in body


def test_gateway_forces_include_usage_on_a_streamed_call(
    client, monkeypatch, fake_gemini, db
):
    """Without include_usage the upstream sends no usage block at all, and
    every deck would finalize against its estimate forever."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "stream"

    client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [], "stream": True},
        headers={"Authorization": "Bearer tok"},
    )
    sent = fake_gemini.seen_bodies[-1]
    assert sent["stream_options"]["include_usage"] is True
    # And the model override still applies on the streaming path.
    assert sent["model"] == settings.presentation_model


def test_gateway_passes_a_quota_refusal_through_as_429(
    client, monkeypatch, fake_gemini
):
    """The engine must see a quota refusal as a quota refusal so the job can
    classify it as permanent rather than retrying into the same wall."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "quota"
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 429


# ── Ownership and download ───────────────────────────────────────────────

def _job_with_artifact(db, user, *, key="presentations/u/j/deck.pptx", expires_in_days=30):
    job = ExecutionJob(user_id=user.id, job_type="presentation", payload={"steps": []},
                       status="completed")
    db.add(job)
    db.commit()
    artifact = ExecutionArtifact(
        job_id=job.id, filename="deck.pptx", relative_path="deck.pptx",
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=len(FAKE_PPTX), storage_backend="local", storage_key=key,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
    )
    db.add(artifact)
    db.commit()
    return job, artifact


async def test_the_owner_can_download_their_deck(db, local_store):
    user = _user(db)
    job, artifact = _job_with_artifact(db, user)
    await local_store.put(key=artifact.storage_key, data=FAKE_PPTX, content_type="application/x")

    target = await artifact_service.download_target(
        db, artifact.id, user.id, store=local_store
    )
    assert target.data == FAKE_PPTX
    assert target.filename == "deck.pptx"


async def test_another_user_cannot_download_it(db, local_store):
    owner = _user(db)
    stranger = _user(db, "max")
    job, artifact = _job_with_artifact(db, owner)
    await local_store.put(key=artifact.storage_key, data=FAKE_PPTX, content_type="application/x")

    with pytest.raises(artifact_service.ArtifactNotFound):
        await artifact_service.download_target(
            db, artifact.id, stranger.id, store=local_store
        )


async def test_possessing_the_storage_key_grants_nothing(db, local_store):
    """Keys are guessable in shape, so a key must never be an authorization
    token. Authorization comes from the job row, every time."""
    owner = _user(db)
    stranger = _user(db, "max")
    job, artifact = _job_with_artifact(db, owner, key="presentations/known/shape/deck.pptx")
    await local_store.put(key=artifact.storage_key, data=FAKE_PPTX, content_type="application/x")

    with pytest.raises(artifact_service.ArtifactNotFound):
        await artifact_service.download_target(
            db, artifact.id, stranger.id, store=local_store
        )


async def test_an_expired_artifact_is_gone_not_missing(db, local_store):
    """The owner is entitled to know it aged out rather than never existed."""
    user = _user(db)
    job, artifact = _job_with_artifact(db, user, expires_in_days=-1)
    with pytest.raises(artifact_service.ArtifactExpired):
        await artifact_service.download_target(db, artifact.id, user.id, store=local_store)


async def test_retention_sweep_removes_the_object_and_the_row(db, local_store):
    user = _user(db)
    job, artifact = _job_with_artifact(db, user, expires_in_days=-1)
    await local_store.put(key=artifact.storage_key, data=FAKE_PPTX, content_type="application/x")

    removed = await artifact_service.sweep_expired(db, store=local_store)
    assert removed == 1
    assert db.query(ExecutionArtifact).filter(
        ExecutionArtifact.id == artifact.id
    ).first() is None


async def test_retention_sweep_leaves_live_artifacts_alone(db, local_store):
    user = _user(db)
    job, artifact = _job_with_artifact(db, user, expires_in_days=30)
    await local_store.put(key=artifact.storage_key, data=FAKE_PPTX, content_type="application/x")

    assert await artifact_service.sweep_expired(db, store=local_store) == 0
    assert db.query(ExecutionArtifact).filter(
        ExecutionArtifact.id == artifact.id
    ).first() is not None


# ── A busy model is waited out, a spent quota is not ─────────────────────

@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr("src.api.routes.presenton_gateway.UPSTREAM_RETRY_DELAYS_S", (0.0, 0.0, 0.0))


def test_a_busy_model_is_retried_and_the_engine_never_sees_it(client, monkeypatch, fake_gemini, no_wait, db):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.busy_first = 2
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert len(fake_gemini.seen_models) == 3


def test_a_busy_model_is_retried_before_a_stream_starts(client, monkeypatch, fake_gemini, no_wait, db):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "stream"
    fake_gemini.busy_first = 1
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": [], "stream": True}, headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert "Slide " in r.text and "one" in r.text and "error" not in r.text
    assert len(fake_gemini.seen_models) == 2


def test_a_model_that_stays_busy_fails_after_the_retries(client, monkeypatch, fake_gemini, no_wait, db):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "stream"
    fake_gemini.busy_first = 99
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": [], "stream": True}, headers={"Authorization": "Bearer tok"},
    )
    assert "error" in r.text
    assert len(fake_gemini.seen_models) == 4


def test_a_spent_quota_is_not_asked_again(client, monkeypatch, fake_gemini, no_wait, db):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "tok")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    fake_gemini.mode = "quota"
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 429
    assert len(fake_gemini.seen_models) == 1
