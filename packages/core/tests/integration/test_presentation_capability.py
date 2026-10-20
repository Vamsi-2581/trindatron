"""The presentation capability, from classification to artifact.

The provider is exercised against a real HTTP server rather than a mock,
because the two things most likely to break are the parts a mock would
paper over: the session-cookie handshake (the published docs say Basic Auth;
the running engine rejects it with 401) and fetching the artifact back with
that same session.

What is NOT tested here is a real Gemini generation. The free tier was
measured at 20 requests per DAY — `quotaValue: "20"`, metric `PerDay` — and
one deck needs more calls than that, so an end-to-end run against Google
cannot pass on an unbilled key. That is a prerequisite, not a flaky test,
and it is recorded as such rather than hidden behind a skip that reads like
an environment quirk.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.capability.capability_router import ExecutorKind, route
from src.capability.classifier import Capability, classify
from src.capability.pipelines import UNIMPLEMENTED_CAPABILITIES
from src.presentation.cost import estimate, max_slides_within
from src.presentation.presenton import PresentonProvider, image_provider_env
from src.presentation.provider import (
    ExportFormat,
    ImageStrategy,
    PresentationError,
    PresentationQuotaError,
    PresentationRequest,
)
from src.services import presentation_service as ps

# A real PPTX is a zip; "PK" is what makes it openable.
FAKE_PPTX = b"PK\x03\x04" + b"\x00" * 512


# ── A stand-in engine that behaves like the real one ─────────────────────

class _Engine(BaseHTTPRequestHandler):
    mode = "ok"          # ok | quota | broken | nosession
    seen: list[str] = []

    def log_message(self, *a):
        pass

    def _json(self, code, payload, cookie=False):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", "presenton_session=s3ss10n; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).seen.append("GET " + self.path)
        if self.path.startswith("/api/v1/auth/status"):
            return self._json(200, {"configured": True, "authenticated": False})
        if self.path.startswith("/static/"):
            # The artifact download must carry the session cookie.
            if "presenton_session" not in self.headers.get("Cookie", ""):
                return self._json(401, {"detail": "Unauthorized"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FAKE_PPTX)))
            self.end_headers()
            self.wfile.write(FAKE_PPTX)
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self):
        type(self).seen.append("POST " + self.path)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path.startswith("/api/v1/auth/login"):
            if type(self).mode == "nosession":
                return self._json(200, {"authenticated": True})   # no cookie
            return self._json(200, {"authenticated": True}, cookie=True)
        if self.path.startswith("/api/v1/ppt/presentation/generate"):
            if "presenton_session" not in self.headers.get("Cookie", ""):
                return self._json(401, {"detail": "Unauthorized"})
            if type(self).mode == "quota":
                return self._json(429, {"detail": "rate limited"})
            if type(self).mode == "broken":
                return self._json(500, {"detail": "boom"})
            return self._json(200, {
                "presentation_id": "deck-1",
                "path": "/static/deck-1.pptx",
                "edit_path": "/p/deck-1",
            })
        self._json(404, {"detail": "not found"})


@pytest.fixture
def engine():
    _Engine.mode = "ok"
    _Engine.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Engine
    srv.shutdown()


def _provider(base):
    return PresentonProvider(base_url=base, username="u", password="p", timeout_s=30)


# ── Discovery: the user says "create a presentation" ─────────────────────

@pytest.mark.parametrize("text", [
    "Create a 10-slide presentation about the Brazilian solar market",
    "make me a slide deck on Q3 results",
    "build a powerpoint for the board",
    "crie uma apresentação de 15 slides sobre energia solar",
    "monte um deck sobre o mercado solar",
    "haz una presentación de 12 diapositivas sobre energía solar",
])
def test_a_deck_request_is_recognised(text):
    assert classify(text) is Capability.PRESENTATION


@pytest.mark.parametrize("text", [
    "generate an image of a solar panel",
    "what is solar energy",
    "read the file and summarise it",
])
def test_other_requests_are_not_mistaken_for_decks(text):
    assert classify(text) is not Capability.PRESENTATION


def test_presentations_route_to_their_own_executor():
    r = route(Capability.PRESENTATION)
    assert r.executor is ExecutorKind.PRESENTATION
    assert r.implemented is True
    assert Capability.PRESENTATION not in UNIMPLEMENTED_CAPABILITIES


# ── Availability: never offer what cannot run ────────────────────────────

def test_an_unconfigured_engine_reports_unavailable():
    p = PresentonProvider(base_url="", username="", password="")
    result = asyncio.run(p.availability())
    assert result.available is False
    assert "not configured" in result.reason


def test_an_unreachable_engine_reports_unavailable_without_raising():
    p = PresentonProvider(base_url="http://127.0.0.1:9", username="u", password="p",
                          health_timeout_s=0.5)
    result = asyncio.run(p.availability())
    assert result.available is False
    assert result.reason


def test_a_healthy_engine_reports_available(engine):
    base, _ = engine
    assert asyncio.run(_provider(base).availability()).available is True


# ── Generation over the real wire ────────────────────────────────────────

def test_a_deck_is_generated_and_the_artifact_comes_back(engine):
    base, cls = engine
    req = PresentationRequest(content="Solar energy in Brazil", n_slides=6)
    result = asyncio.run(_provider(base).generate(req))

    assert result.provider == "presenton"
    assert result.external_id == "deck-1"
    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.filename.endswith(".pptx")
    assert art.data[:2] == b"PK", "artifact is not a real zip/pptx"
    assert art.size_bytes == len(FAKE_PPTX)
    assert "presentationml" in art.media_type
    # Sign-in happened before generation, and the artifact was fetched.
    assert any("auth/login" in s for s in cls.seen)
    assert any("/static/deck-1.pptx" in s for s in cls.seen)


def test_pdf_is_requested_and_typed_correctly(engine):
    base, _ = engine
    req = PresentationRequest(content="x", n_slides=3, export_as=ExportFormat.PDF)
    art = asyncio.run(_provider(base).generate(req)).artifacts[0]
    assert art.filename.endswith(".pdf")
    assert art.media_type == "application/pdf"


def test_a_quota_refusal_is_its_own_error_and_is_not_retried(engine):
    """Measured: the free Gemini tier caps at 20 requests per day. Retrying
    a quota refusal spends the remainder faster and lengthens the outage, so
    it has to be distinguishable from an ordinary failure."""
    base, cls = engine
    cls.mode = "quota"
    with pytest.raises(PresentationQuotaError):
        asyncio.run(_provider(base).generate(PresentationRequest(content="x")))
    # Exactly one generate attempt — no retry loop inside the provider.
    assert sum(1 for s in cls.seen if "generate" in s) == 1


def test_an_engine_error_surfaces_as_a_failure(engine):
    base, cls = engine
    cls.mode = "broken"
    with pytest.raises(PresentationError):
        asyncio.run(_provider(base).generate(PresentationRequest(content="x")))


def test_a_login_without_a_session_is_refused(engine):
    base, cls = engine
    cls.mode = "nosession"
    with pytest.raises(PresentationError):
        asyncio.run(_provider(base).generate(PresentationRequest(content="x")))


def test_credentials_never_appear_in_an_error(engine):
    """An error message is rendered in the UI and written to the activity
    trail; a password must not ride along."""
    base, cls = engine
    cls.mode = "broken"
    p = PresentonProvider(base_url=base, username="admin", password="sup3r-s3cret-pw")
    try:
        asyncio.run(p.generate(PresentationRequest(content="x")))
    except PresentationError as exc:
        assert "sup3r-s3cret-pw" not in str(exc)
        assert "admin" not in str(exc)
    else:
        pytest.fail("expected a failure")


# ── Cost: the estimate is what protects the window ───────────────────────

def test_a_deck_costs_far_more_than_one_request_reservation():
    """The reason a preflight estimate exists at all. A 10-slide deck is
    ~12 TCU; the flat per-request reservation is 4 on START and 8 on PRO."""
    from src.entitlements import get_plan_entitlement

    deck = estimate(n_slides=10, image_strategy=ImageStrategy.STOCK_FIRST).tcu
    for plan in ("start", "pro"):
        assert deck > get_plan_entitlement(plan).max_tcu_per_request


def test_the_estimate_grows_with_the_deck():
    small = estimate(n_slides=5, image_strategy=ImageStrategy.STOCK_FIRST).tcu
    large = estimate(n_slides=20, image_strategy=ImageStrategy.STOCK_FIRST).tcu
    assert large > small * 2


def test_generated_images_cost_an_order_of_magnitude_more():
    """The finding that decides the image strategy: pictures, not text,
    dominate a deck's cost."""
    stock = estimate(n_slides=10, image_strategy=ImageStrategy.STOCK_FIRST).tcu
    gen = estimate(n_slides=10, image_strategy=ImageStrategy.GENERATED).tcu
    assert gen > stock * 10


def test_reasoning_tokens_are_priced_in():
    """Gemini 3.8 Flash bills invisible reasoning as output — a live call
    billed 70 tokens for a 9-token prompt and an empty completion. An
    estimate that ignored it would under-reserve every deck."""
    from src.presentation import cost

    assert cost.REASONING_MULTIPLIER > 1.0


@pytest.mark.parametrize("plan,expected", [
    ("free", ImageStrategy.STOCK_FIRST),
    ("start", ImageStrategy.STOCK_FIRST),
    ("pro", ImageStrategy.STOCK_FIRST),
    ("max", ImageStrategy.GENERATED),
    ("team", ImageStrategy.GENERATED),
])
def test_generated_images_only_where_the_window_can_absorb_them(plan, expected):
    assert ps.image_strategy_for_plan(plan) is expected


def test_every_paid_plan_can_still_make_a_real_deck():
    """Stock images are what keep the capability available below MAX: a full
    START window pays for a deck of useful length."""
    from src.entitlements import get_plan_entitlement

    for plan in ("start", "pro", "max"):
        window = get_plan_entitlement(plan).session_tcu_limit
        slides = ps.largest_affordable_deck(plan=plan, remaining_tcu=window)
        assert slides >= 10, f"{plan} can only afford {slides} slides per window"


def test_free_does_not_get_presentations():
    """Not a commercial choice so much as an arithmetic one: FREE's whole day
    is 10 TCU and the smallest useful deck is about 8."""
    assert ps.plan_allows_presentations("free") is False
    for plan in ("start", "pro", "max", "team"):
        assert ps.plan_allows_presentations(plan) is True


def test_the_remaining_budget_bounds_the_deck():
    small = ps.largest_affordable_deck(plan="pro", remaining_tcu=5)
    large = ps.largest_affordable_deck(plan="pro", remaining_tcu=60)
    assert small < large
    assert max_slides_within(0.0, image_strategy=ImageStrategy.STOCK_FIRST) == 0


# ── Engine-side configuration ────────────────────────────────────────────

def test_the_cheap_image_strategy_does_not_enable_generation():
    env = image_provider_env(ImageStrategy.STOCK_FIRST)
    assert env["DISABLE_IMAGE_GENERATION"] == "true"
    assert env["IMAGE_PROVIDER"] == "pexels"


def test_the_expensive_strategy_is_explicit_about_it():
    env = image_provider_env(ImageStrategy.GENERATED)
    assert env["DISABLE_IMAGE_GENERATION"] == "false"
    assert env["IMAGE_PROVIDER"] == "gemini_flash"


def test_gemini_remains_the_configured_intelligence():
    """Gemini is mandated for this capability. Nothing here may quietly
    introduce a second LLM as a fallback."""
    from src.config import settings

    assert "gemini" in settings.presentation_model.lower()
