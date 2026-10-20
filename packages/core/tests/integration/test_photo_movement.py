"""Photo → Movement: a photo is animated only in the Maximum mode, on the plans that have it (PRO, MAX, TEAM).

Product decision 2026-09-29. The efficient engines (Economy/Quality, the 5B) ignore a photo (measured 2026-09-25),
so a photo never falls to them: it runs in Maximum on an engine that animates it, or it is refused. Scripted
engines only — no provider, nothing spent.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from src.media import modes, plans
from src.media.orchestrator import VideoEngineChain
from src.media.registry import use_engines
from src.media.routes import HostedVideoRoute
from src.models.media import MediaJob
from src.services import capability_service
from tests.integration.test_video_modes import MAXIMUM_USD_S, PricedVideo, _plan, efficient, store  # noqa: F401


class PhotoOnly(PricedVideo):
    """An engine that animates a photo and does nothing else (Wan2.2-I2V-A14B + Lightning)."""

    async def is_available(self, *, from_image: bool = False) -> bool:
        return from_image


class TextOnly(PricedVideo):
    """An engine that never takes a photo (the 5B, and the T2V A14B)."""

    async def is_available(self, *, from_image: bool = False) -> bool:
        return not from_image


def photo_maximum() -> HostedVideoRoute:
    return HostedVideoRoute(PhotoOnly({480: MAXIMUM_USD_S, 720: MAXIMUM_USD_S}),
                            engine_id="wan22_i2v_a14b_lightning", runner_name="modal", quality=4)


def text_maximum() -> HostedVideoRoute:
    return HostedVideoRoute(TextOnly({480: MAXIMUM_USD_S, 720: MAXIMUM_USD_S}),
                            engine_id="wan22_t2v_a14b", runner_name="modal", quality=4)


def text_efficient() -> HostedVideoRoute:
    route = efficient()
    route.engine = TextOnly(route.engine.usd_per_second)
    return route


def _photo(client, headers) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (832, 480), (200, 160, 40)).save(buffer, format="PNG")
    response = client.post("/media/sources", headers=headers, files={"file": ("tram.png", buffer.getvalue(), "image/png")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _animate(client, headers, source_id: str, **body):
    return client.post("/media/videos", headers=headers,
                       json={"prompt": "o bonde anda devagar", "source_artifact_id": source_id, **body})


# ── Who may animate a photo ──────────────────────────────────────────────────

@pytest.mark.parametrize("plan, included", [("free", False), ("start", False), ("pro", True), ("max", True),
                                            ("team", True)])
def test_photo_movement_belongs_to_the_plans_with_the_maximum_mode(plan, included):
    assert plans.MEDIA_ENTITLEMENTS[plan].image_to_video is included
    assert ("maximum" in plans.MEDIA_ENTITLEMENTS[plan].video_modes) is included


def test_start_is_told_the_plan_does_not_include_it(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "start")
    source = _photo(client, auth_headers)
    with use_engines(video=VideoEngineChain([text_efficient(), photo_maximum()])):
        response = _animate(client, auth_headers, source)
    assert response.status_code == 403 and response.json()["detail"]["error"] == "plan_not_included"
    assert db.query(MediaJob).count() == 0


# ── A photo runs in Maximum, on the engine that animates it ─────────────────

@pytest.mark.parametrize("plan", ["pro", "max"])
def test_a_photo_runs_in_maximum_even_when_the_plan_defaults_to_economy(db, client, auth_headers, registered_user,
                                                                        store, plan):
    _plan(db, registered_user["user_id"], plan)
    source = _photo(client, auth_headers)
    chain = VideoEngineChain([text_efficient(), text_maximum(), photo_maximum()])
    with use_engines(video=chain):
        response = _animate(client, auth_headers, source)
    assert response.status_code == 202, response.text
    job = db.query(MediaJob).filter(MediaJob.id == response.json()["id"]).one()
    assert job.operation == "image_to_video" and job.source_artifact_id == source
    assert job.params["mode"] == "maximum" and job.params["adjustments"] == []
    # Reserved as a photo clip on the photo engine (5 s at the Maximum rate), not priced as text to video.
    assert float(job.estimated_cost_usd) == pytest.approx(5 * MAXIMUM_USD_S)


def test_asking_for_economy_with_a_photo_still_runs_in_maximum(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "pro")
    source = _photo(client, auth_headers)
    with use_engines(video=VideoEngineChain([text_efficient(), photo_maximum()])):
        response = _animate(client, auth_headers, source, mode="economy")
    assert response.status_code == 202, response.text
    assert db.query(MediaJob).one().params["mode"] == "maximum"


def test_without_an_engine_that_animates_photos_the_photo_is_refused_never_sent_to_the_5b(
        db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    source = _photo(client, auth_headers)
    economy = text_efficient()
    with use_engines(video=VideoEngineChain([economy, text_maximum()])):
        response = _animate(client, auth_headers, source)
    assert response.status_code != 202
    assert db.query(MediaJob).count() == 0 and economy.engine.calls == []


# ── What the product says it can do ─────────────────────────────────────────

def _image_to_video(client, headers) -> dict:
    capability_service.reset_cache()
    return client.get("/users/me/capabilities", headers=headers).json()


def test_capabilities_offer_photo_movement_only_where_it_runs(db, client, auth_headers, registered_user, store):
    chain = VideoEngineChain([text_efficient(), text_maximum(), photo_maximum()])
    with use_engines(video=chain):
        _plan(db, registered_user["user_id"], "start")
        start = _image_to_video(client, auth_headers)
        _plan(db, registered_user["user_id"], "pro")
        pro = _image_to_video(client, auth_headers)
    assert start["capabilities"]["image_to_video"]["available"] is False
    assert start["matrix"]["image_to_video"]["status"] == "plan"
    assert pro["capabilities"]["image_to_video"]["available"] is True
    assert pro["matrix"]["image_to_video"]["status"] == "available"


def test_the_photo_engine_is_a_maximum_engine():
    assert modes.engine_class(photo_maximum()) == modes.MAXIMUM_CLASS
    assert modes.restrict(VideoEngineChain([text_efficient(), photo_maximum()]), modes.ECONOMY).engines[0] \
        .engine_id == "wan22_ti2v_5b"
