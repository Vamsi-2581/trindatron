"""Video modes, the media pool and the router, through the real product paths.

Engines are scripted routes priced like the measured ones (media/modes.py):
economy ≈ 1.24 TCU, quality ≈ 2.31, maximum ≈ 9.31 and premium 35 for 5 s.
Plans, windows, the media pool, reservations, jobs, settlement and chat are real.

Pinned here:
  * each plan runs its default mode, and a mode above the plan is lowered, saying so;
  * when a request does not fit, the router shortens it, then lowers the resolution,
    then uses a cheaper engine — and refuses rather than run above the budget;
  * media never spends the chat/agent floor, and chat still works when the pool is empty;
  * premium (xAI) runs only on MAX, only when asked for and confirmed, and is never a fallback;
  * a job settles at its real cost.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.media.engines import EngineFailed, MediaOutput, VideoEngine
from src.media.health import health
from src.media.orchestrator import VideoEngineChain
from src.media.registry import use_engines
from src.media.routes import HostedVideoRoute
from src.media.settings import media_settings
from src.models.media import MediaJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, entitlement_service, media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession
from tests.week_helpers import earlier_this_week_not_today

# US$ per clip second, as measured (media/modes.py) — 480p and 720p on the 5B, 720p on the A14B, xAI 720p.
ECONOMY_USD_S, QUALITY_USD_S, MAXIMUM_USD_S, PREMIUM_USD_S = 0.00248, 0.00462, 0.01862, 0.07


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"0" * 64


class PricedVideo(VideoEngine):
    """A route's engine that prices like the real one and records what it was asked to make."""

    def __init__(self, usd_per_second: dict[int, float], *, short_side: int = 720, fail: bool = False,
                 real_cost_factor: float = 1.0) -> None:
        self.usd_per_second, self.short_side, self.fail = usd_per_second, short_side, fail
        self.real_cost_factor = real_cost_factor
        self.calls: list = []

    async def is_available(self, *, from_image: bool = False) -> bool:
        return True

    def normalize(self, request):
        short = min(self.short_side, min(request.width, request.height))
        scale = short / min(request.width, request.height)
        return replace(request, width=int(request.width * scale) // 16 * 16, height=int(request.height * scale) // 16 * 16,
                       duration_seconds=min(6, request.duration_seconds))

    def _rate(self, request) -> float:
        short = min(request.width, request.height)
        return self.usd_per_second[480] if short <= 480 else self.usd_per_second[720]

    def estimate_usd(self, request):
        return self._rate(request) * request.duration_seconds

    async def generate(self, request):
        self.calls.append(request)
        if self.fail:
            raise EngineFailed("no gpu", retryable=False)
        return MediaOutput(data=_mp4(), content_type="video/mp4", width=request.width, height=request.height,
                           duration_seconds=float(request.duration_seconds), engine_seconds=10.0,
                           cost_usd=self.estimate_usd(request) * self.real_cost_factor)


def efficient(**kwargs) -> HostedVideoRoute:
    return HostedVideoRoute(PricedVideo({480: ECONOMY_USD_S, 720: QUALITY_USD_S}, **kwargs),
                            engine_id="wan22_ti2v_5b", runner_name="modal", quality=3)


def maximum(usd_per_second: float = MAXIMUM_USD_S, **kwargs) -> HostedVideoRoute:
    return HostedVideoRoute(PricedVideo({480: usd_per_second, 720: usd_per_second}, **kwargs),
                            engine_id="wan22_a14b", runner_name="modal", quality=4)


def premium(**kwargs) -> HostedVideoRoute:
    return HostedVideoRoute(PricedVideo({480: 0.05, 720: PREMIUM_USD_S}, **kwargs),
                            engine_id="grok_imagine_video", runner_name="xai", quality=4)


@pytest.fixture
def store(tmp_path, monkeypatch):
    media_settings.cache_clear()
    health.reset()
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    capability_service.reset_cache()
    yield local
    capability_service.reset_cache()
    health.reset()
    media_settings.cache_clear()


def _plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


def _user(db, registered_user) -> User:
    return db.query(User).filter(User.id == registered_user["user_id"]).one()


def _video(client, headers, **body):
    return client.post("/media/videos", headers=headers, json={"prompt": "ondas nas pedras", **body})


def _job(db, response) -> MediaJob:
    assert response.status_code == 202, response.text
    return db.query(MediaJob).filter(MediaJob.id == response.json()["id"]).one()


def _earlier_today():
    """A second before now, never before today: when a test's "already spent today" happened.

    The request that follows starts the 5-hour session at its own "now", and on Windows the clock
    moves in ~1 ms steps, so a record stamped "now" could share that instant and count inside the
    session too (seen on the self-hosted runner, never on Linux).
    """
    day_start, _ = entitlement_service._day_window(None)
    return max(day_start, datetime.now(timezone.utc) - timedelta(seconds=1))


def _spend_chat(db, user_id: str, tcu: float) -> None:
    """TCU already spent today by chat (a completed usage record, not media)."""
    db.add(UsageRecord(user_id=user_id, model="chat", cost_usd=Decimal("0"), tcu=Decimal(str(tcu)),
                       status="completed", created_at=_earlier_today()))
    db.commit()


def _spend_media(db, user_id: str, tcu: float, *, mode: str | None = None) -> None:
    """A finished creation today: counted by the media pool (and, like the real one, by the windows)."""
    when = _earlier_today()
    db.add(MediaJob(user_id=user_id, kind="video", operation="generate", status="completed", prompt="x",
                    params={"mode": mode} if mode else {}, estimated_cost_usd=Decimal(str(tcu / 100)),
                    cost_usd=Decimal(str(tcu / 100)), created_at=when, completed_at=when))
    db.add(UsageRecord(user_id=user_id, model="media:video", cost_usd=Decimal(str(tcu / 100)),
                       tcu=Decimal(str(tcu)), status="completed", created_at=when))
    db.commit()


# ── Default modes and plan ceilings ─────────────────────────────────────────

def test_start_runs_economy_480p_by_default(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    with use_engines(video=VideoEngineChain([efficient(), maximum(), premium()])):
        job = _job(db, _video(client, auth_headers))
    assert job.params["mode"] == "economy" and job.params["adjustments"] == []
    assert min(job.params["width"], job.params["height"]) == 480 and job.params["duration_seconds"] == 5
    assert float(job.estimated_cost_usd) == pytest.approx(5 * ECONOMY_USD_S)


def test_a_clip_is_5_s_by_default_and_up_to_6_s_when_asked(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        default = _job(db, _video(client, auth_headers))
        db.query(MediaJob).filter(MediaJob.id == default.id).update({"status": "completed"})
        db.commit()
        longest = _job(db, _video(client, auth_headers, duration_seconds=6))
        db.query(MediaJob).filter(MediaJob.id == longest.id).update({"status": "completed"})
        db.commit()
        too_long = _video(client, auth_headers, duration_seconds=7)
    with use_engines(video=VideoEngineChain([efficient(), maximum()]), image=None):
        entitlement = media_service.entitlement_for(db, _user(db, registered_user))
    assert default.params["duration_seconds"] == 5 and default.params["adjustments"] == []
    assert longest.params["duration_seconds"] == 6 and longest.params["adjustments"] == []
    # A 6 s clip reserves its own cost; the daily count stays sized on 5 s.
    assert float(longest.estimated_cost_usd) == pytest.approx(6 * ECONOMY_USD_S)
    assert too_long.status_code == 422
    assert entitlement.max_video_duration_seconds == 6 and entitlement.video_daily_limit == 15


def test_max_runs_economy_480p_by_default(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), maximum(), premium()])):
        response = _video(client, auth_headers)
    job = _job(db, response)
    assert job.params["mode"] == "economy" and min(job.params["width"], job.params["height"]) == 480
    assert response.json()["mode"] == "economy" and response.json()["adjustments"] == []


@pytest.mark.parametrize("request_text, mode, short_side", [
    ("Crie um vídeo de ondas quebrando nas rochas", "economy", 480),
    ("Crie um vídeo de ondas quebrando nas rochas em alta qualidade", "quality", 720),
    ("Crie um vídeo em Full HD de ondas quebrando nas rochas", "quality", 720),
])
def test_in_the_conversation_max_gets_480p_unless_it_asks_for_quality(db, client, auth_headers, registered_user,
                                                                       store, request_text, mode, short_side):
    from tests.integration.test_chat_media import _ask

    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), maximum(), premium()])):
        _, events = _ask(client, auth_headers, request_text)
    job_id = next(event["artifact"]["id"] for event in events if event["type"] == "artifact")
    job = db.query(MediaJob).filter(MediaJob.id == job_id).one()
    assert job.params["mode"] == mode and min(job.params["width"], job.params["height"]) == short_side


async def test_pro_can_ask_for_maximum_and_it_runs_only_on_the_maximum_engine(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    fast, best, xai = efficient(), maximum(), premium()
    with use_engines(video=VideoEngineChain([fast, best, xai])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
        assert job.params["mode"] == "maximum"
        assert float(job.estimated_cost_usd) == pytest.approx(5 * MAXIMUM_USD_S)  # 9.31 TCU, inside PRO's 11
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    db.refresh(job)
    assert job.status == "completed"
    assert len(best.engine.calls) == 1 and fast.engine.calls == [] and xai.engine.calls == []


def test_a_mode_above_the_plan_becomes_the_best_the_plan_offers_and_says_so(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
    assert job.params["mode"] == "quality" and job.params["requested_mode"] == "maximum"
    assert job.params["adjustments"] == ["plan"]


def test_free_has_no_video_in_any_mode(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "free")
    with use_engines(video=VideoEngineChain([efficient(), maximum(), premium()])):
        for mode in (None, "economy", "quality", "maximum", "premium"):
            response = _video(client, auth_headers, **({"mode": mode} if mode else {}))
            assert response.status_code == 403 and response.json()["detail"]["error"] == "plan_not_included"


def test_the_per_request_ceiling_is_never_exceeded(db, client, auth_headers, registered_user, store):
    """A maximum engine priced like a hosted API (40 TCU for 5 s) never runs on PRO (11 TCU a request)."""
    _plan(db, registered_user["user_id"], "pro")
    with use_engines(video=VideoEngineChain([efficient(), maximum(usd_per_second=0.08)])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
    # The hosted maximum is not offered to PRO at all: the ask is lowered to the best mode PRO can pay for.
    assert job.params["mode"] == "quality"
    assert float(job.estimated_cost_usd) * 100 <= 11


# ── The router: duration, then resolution, then engine; never above the budget ─

def test_the_router_shortens_the_clip_first(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    _spend_chat(db, registered_user["user_id"], 102)  # 8 TCU left today: 4 s of maximum (7.45) fits, 5 s does not
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
    assert job.params["mode"] == "maximum" and job.params["duration_seconds"] == 4
    assert job.params["adjustments"] == ["duration"]


def test_the_router_lowers_the_resolution_before_changing_engine(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    _spend_chat(db, registered_user["user_id"], 350 - 1.3)  # 1.3 TCU left today: 720p never fits, 480p 5 s does
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        job = _job(db, _video(client, auth_headers, mode="quality"))
    assert job.params["mode"] == "economy" and job.params["duration_seconds"] == 5
    assert job.params["adjustments"] == ["resolution"]


def test_the_router_uses_a_cheaper_engine_last(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    _spend_chat(db, registered_user["user_id"], 105)  # 5 TCU left: no maximum clip of 3 s or more fits (5.59)
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
    assert job.params["mode"] == "quality" and job.params["duration_seconds"] == 5
    assert job.params["adjustments"] == ["engine"]
    assert float(job.estimated_cost_usd) * 100 <= 5


def test_nothing_runs_when_nothing_fits(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    _spend_chat(db, registered_user["user_id"], 45 - 0.5)  # 0.5 TCU left: even 3 s of economy (0.74) does not fit
    with use_engines(video=VideoEngineChain([efficient()])):
        response = _video(client, auth_headers)
    assert response.status_code == 429 and response.json()["detail"]["error"] == "budget_exhausted"
    assert db.query(MediaJob).count() == 0


def test_a_mode_whose_daily_cap_is_used_falls_to_the_next_mode(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    for _ in range(2):  # PRO: two maximum clips a day
        _spend_media(db, registered_user["user_id"], 9.31, mode="maximum")
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        job = _job(db, _video(client, auth_headers, mode="maximum"))
    assert job.params["mode"] == "quality" and job.params["adjustments"] == ["engine"]


# ── One media pool, and the chat/agent floor ────────────────────────────────

def test_the_pool_counts_every_kind_and_leaves_the_floor_to_chat(db, client, auth_headers, registered_user, store):
    user_id = registered_user["user_id"]
    _plan(db, user_id, "start")
    _spend_media(db, user_id, 36)  # START's daily pool: 45 less the 20 % floor
    user = _user(db, registered_user)
    pool = media_service.media_pool(db, user)
    assert pool.daily_limit == 36.0 and pool.remaining == 0.0

    with use_engines(video=VideoEngineChain([efficient()])):
        response = _video(client, auth_headers)
    assert response.status_code == 429 and response.json()["detail"]["error"] == "budget_exhausted"

    # The floor is still there for the conversation.
    reservation = entitlement_service.check_and_reserve(db, user_id, None, "start", user.timezone, estimated_tcu=1.0)
    assert entitlement_service.get_reservation_budget_tcu(db, reservation) == pytest.approx(1.0)


def test_images_draw_from_the_same_pool(db, client, auth_headers, registered_user, store):
    from tests.integration.test_chat_media import ScriptedImage

    user_id = registered_user["user_id"]
    _plan(db, user_id, "start")
    _spend_media(db, user_id, 36)
    with use_engines(image=ScriptedImage()):
        response = client.post("/media/images", headers=auth_headers, json={"prompt": "um farol"})
    assert response.status_code == 429 and response.json()["detail"]["error"] == "budget_exhausted"


def test_the_weekly_pool_binds_across_days(db, client, auth_headers, registered_user, store):
    user_id = registered_user["user_id"]
    _plan(db, user_id, "start")
    user = _user(db, registered_user)
    earlier = earlier_this_week_not_today(user.timezone)
    # 87.5 TCU of creations earlier this week (not today): the weekly pool (88) has 0.5 left.
    db.add(UsageRecord(user_id=user_id, model="media:image", cost_usd=Decimal("0.875"), tcu=Decimal("87.5"),
                       status="completed", created_at=earlier))
    db.commit()
    assert media_service.media_pool(db, user).remaining == pytest.approx(0.5)
    with use_engines(video=VideoEngineChain([efficient()])):
        response = _video(client, auth_headers)
    assert response.status_code == 429 and response.json()["detail"]["error"] == "budget_exhausted"


# ── Premium (xAI): MAX only, explicit, confirmed, never a fallback ──────────

def test_premium_asks_for_confirmation_with_its_cost(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        response = _video(client, auth_headers, mode="premium")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "confirmation_required" and detail["mode"] == "premium"
    assert detail["estimated_tcu"] == pytest.approx(35.0) and detail["duration_seconds"] == 5
    assert db.query(MediaJob).count() == 0


def test_premium_runs_when_confirmed(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        job = _job(db, _video(client, auth_headers, mode="premium", confirm_premium=True))
    assert job.params["mode"] == "premium" and float(job.estimated_cost_usd) == pytest.approx(0.35)


def test_with_only_a_premium_engine_max_is_asked_to_confirm_and_start_gets_nothing(db, client, auth_headers,
                                                                                     registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([premium()])):
        response = _video(client, auth_headers)  # no mode: premium is never chosen silently
        assert response.status_code == 409 and response.json()["detail"]["error"] == "confirmation_required"
        _plan(db, registered_user["user_id"], "start")
        response = _video(client, auth_headers, mode="premium", confirm_premium=True)
    assert response.status_code == 403 and response.json()["detail"]["error"] == "plan_not_included"


def test_a_plan_never_promises_more_clips_than_its_mode_caps(db, registered_user, store):
    """MAX with only a premium engine: 2 clips a day (the premium cap), not the 3 its pool could pay for."""
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([premium()]), image=None):
        entitlement = media_service.entitlement_for(db, _user(db, registered_user))
    assert entitlement.video_modes == ("premium",) and entitlement.default_video_mode is None
    assert entitlement.video_daily_limit == entitlement.mode_cap("premium") == 2


async def test_premium_is_never_a_fallback(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    broken, xai = efficient(fail=True), premium()
    with use_engines(video=VideoEngineChain([broken, xai])):
        job = _job(db, _video(client, auth_headers))
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    db.refresh(job)
    assert job.status == "failed"
    assert len(broken.engine.calls) == 1 and xai.engine.calls == []
    # Nothing was charged: the job's reservation was released.
    assert db.query(UsageRecord).filter(UsageRecord.model == "media:video", UsageRecord.status != "failed").count() == 0
    assert float(db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).one().tcu) == 0.0


def test_a_confirmed_premium_that_does_not_fit_is_lowered_not_forced(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    _spend_chat(db, registered_user["user_id"], 350 - 10)  # 10 TCU left: premium (21 TCU for 3 s) never fits
    with use_engines(video=VideoEngineChain([efficient(), maximum(), premium()])):
        job = _job(db, _video(client, auth_headers, mode="premium", confirm_premium=True))
    assert job.params["mode"] == "maximum" and job.params["adjustments"] == ["engine"]


# ── Settlement ──────────────────────────────────────────────────────────────

async def test_a_job_settles_at_its_real_cost(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    route = efficient(real_cost_factor=0.5)
    with use_engines(video=VideoEngineChain([route])):
        job = _job(db, _video(client, auth_headers))
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    db.refresh(job)
    assert job.status == "completed"
    assert float(job.cost_usd) == pytest.approx(float(job.estimated_cost_usd) * 0.5)
    usage = db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).one()
    assert usage.status == "completed" and float(usage.tcu) == pytest.approx(float(job.cost_usd) * 100, abs=1e-3)


# ── In the conversation ─────────────────────────────────────────────────────

async def _never(request):
    raise AssertionError("a creation request must never reach the conversation model")
    yield  # pragma: no cover


def _ask(client, headers, content: str, session_id: str | None = None) -> str:
    session_id = session_id or client.post("/sessions", json={}, headers=headers).json()["id"]
    with patch("src.services.chat_service._router.stream", _never), patch("src.services.agent_service._router.stream", _never):
        response = client.post(f"/sessions/{session_id}/message", json={"content": content}, headers=headers)
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    return next(event["answer"] for event in events if event["type"] == "done")


def test_chat_names_the_mode_and_says_what_changed(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        reply = _ask(client, auth_headers, "Crie um vídeo em máxima qualidade de um farol")
    assert reply.startswith("Estou criando seu vídeo")
    assert "Seu plano não inclui o modo pedido; usei o modo Qualidade (720p)." in reply
    job = db.query(MediaJob).one()
    assert job.params["mode"] == "quality"


def test_chat_asks_before_premium_and_runs_it_once_confirmed(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        reply = _ask(client, auth_headers, "Crie um vídeo premium de um farol")
        assert "35 TCU" in reply and "Responda “sim” para confirmar" in reply
        assert db.query(MediaJob).count() == 0
        reply = _ask(client, auth_headers, "Crie um vídeo premium de um farol, confirmo")
    assert reply.startswith("Estou criando seu vídeo")
    assert db.query(MediaJob).one().params["mode"] == "premium"


# ── Video that has to be heard (owner's rule, 2026-09-27) ────────────────────

AUDIO_REQUEST = ("Crie um vídeo de uma festa, com som: um homem gritando 'VIVA A VIDA, TRINDATRON!', "
                 "a multidão comemorando e uma garrafa de champanhe estourando")


def test_max_is_offered_premium_for_a_video_with_sound_and_a_yes_makes_it(db, client, auth_headers,
                                                                          registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        reply = _ask(client, auth_headers, AUDIO_REQUEST, session_id=session_id)
        assert reply.startswith("Esse vídeo tem som") and "35 TCU" in reply and "Responda “sim”" in reply
        assert db.query(MediaJob).count() == 0  # nothing runs, and nothing is billed, before the yes

        reply = _ask(client, auth_headers, "sim", session_id=session_id)

    assert reply.startswith("Estou criando seu vídeo")
    job = db.query(MediaJob).one()
    assert job.params["mode"] == "premium" and "VIVA A VIDA" in job.prompt  # the offered request, not "sim"


def test_a_yes_with_no_offer_before_it_is_just_conversation(db, client, auth_headers, registered_user, store):
    from src.services import chat_media

    _plan(db, registered_user["user_id"], "max")
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    assert chat_media.accepted_offer(db, registered_user["user_id"], session_id, "sim") is None
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        _ask(client, auth_headers, "Crie um vídeo de um farol", session_id=session_id)  # made at once, no offer
    assert chat_media.accepted_offer(db, registered_user["user_id"], session_id, "sim") is None


def test_a_yes_in_someone_elses_conversation_confirms_nothing(db, client, auth_headers, registered_user, store):
    from src.services import chat_media

    _plan(db, registered_user["user_id"], "max")
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        _ask(client, auth_headers, AUDIO_REQUEST, session_id=session_id)
    assert chat_media.accepted_offer(db, registered_user["user_id"], session_id, "sim") is not None
    assert chat_media.accepted_offer(db, "someone-else", session_id, "sim") is None


def test_start_gets_the_video_without_sound_and_is_told_where_sound_is(db, client, auth_headers,
                                                                      registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        reply = _ask(client, auth_headers, AUDIO_REQUEST)
    assert reply.startswith("Estou criando seu vídeo")
    assert reply.endswith("Este vídeo sai sem som: vídeo com som está disponível no plano MAX.")
    assert db.query(MediaJob).one().params["mode"] == "economy"


def test_a_scene_described_without_crie_is_made_not_refused(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    scene = ("Wide cinematic shot at golden hour on Lago do Manso, a small wooden boat glides across calm water, "
             "gentle camera dolly following the boat, photorealistic")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        reply = _ask(client, auth_headers, scene)
    assert reply.startswith("Estou criando seu vídeo") or reply.startswith("I'm creating your video")
    assert db.query(MediaJob).one().params["mode"] == "economy"  # no sound asked: 480p by default


def test_chat_explains_a_shorter_clip(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    _spend_chat(db, registered_user["user_id"], 102)
    with use_engines(video=VideoEngineChain([efficient(), maximum()])):
        reply = _ask(client, auth_headers, "Crie um vídeo em máxima qualidade de um farol")
    assert reply.endswith("Para caber na sua capacidade atual, encurtei o vídeo para 4 s.")


# ── The scene the engine receives (media/prompt_writer.py) ───────────────────

SCENE = "Wide cinematic shot at golden hour on Lago do Manso: a wooden boat glides across calm water. Photorealistic."


def _writer(monkeypatch, seen: list, cost: float = 0.0005):
    from src.media import prompt_writer

    async def rewrite(request, *, kind, plan, with_sound=False, router=None):
        seen.append({"request": request, "kind": kind, "plan": plan, "with_sound": with_sound})
        return prompt_writer.Rewrite(prompt=SCENE, cost_usd=cost, model="cheap-writer")

    monkeypatch.setattr(prompt_writer, "rewrite", rewrite)


async def test_the_engine_receives_the_written_scene_and_the_job_pays_for_writing_it(
        db, client, auth_headers, registered_user, store, monkeypatch):
    seen: list = []
    _writer(monkeypatch, seen)
    _plan(db, registered_user["user_id"], "max")
    route = efficient()
    with use_engines(video=VideoEngineChain([route])):
        job = _job(db, _video(client, auth_headers, prompt="crie um video de um barco no manso mt no po do sol"))
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    db.refresh(job)

    assert job.status == "completed"
    assert route.engine.calls[0].prompt == SCENE
    assert job.prompt == "crie um video de um barco no manso mt no po do sol"  # the person's words are kept
    assert seen == [{"request": job.prompt, "kind": "video", "plan": "max", "with_sound": False}]
    engine_usd = route.engine.estimate_usd(route.engine.calls[0])
    assert float(job.cost_usd) == pytest.approx(engine_usd + 0.0005)
    usage = db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).one()
    assert float(usage.cost_usd) == pytest.approx(engine_usd + 0.0005)


async def test_premium_with_sound_asks_the_writer_to_keep_the_sound(db, client, auth_headers, registered_user,
                                                                    store, monkeypatch):
    seen: list = []
    _writer(monkeypatch, seen)
    _plan(db, registered_user["user_id"], "max")
    with use_engines(video=VideoEngineChain([efficient(), premium()])):
        job = _job(db, _video(client, auth_headers, prompt=AUDIO_REQUEST, mode="premium", confirm_premium=True))
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    assert seen[0]["with_sound"] is True


async def test_when_the_writer_fails_the_engine_gets_the_persons_words(db, client, auth_headers, registered_user,
                                                                        store, monkeypatch):
    from src.media import prompt_writer

    async def fails(*args, **kwargs):
        return None

    monkeypatch.setattr(prompt_writer, "rewrite", fails)
    _plan(db, registered_user["user_id"], "max")
    route = efficient()
    with use_engines(video=VideoEngineChain([route])):
        job = _job(db, _video(client, auth_headers, prompt="ondas nas pedras"))
        await media_service.run_job(job.id, session_factory=_TestingSession, store=store)
    db.refresh(job)
    assert job.status == "completed" and route.engine.calls[0].prompt == "ondas nas pedras"
    assert "engine_prompt" not in (job.params or {})


async def test_an_edit_of_an_attached_image_keeps_the_persons_instruction(db, registered_user, monkeypatch):
    seen: list = []
    _writer(monkeypatch, seen)
    job = MediaJob(user_id=registered_user["user_id"], kind="image", operation="edit", prompt="deixe o céu roxo",
                   params={})
    assert await media_service._engine_prompt(db, job, has_source=True) == "deixe o céu roxo"
    assert seen == []
