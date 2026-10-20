"""With every hosted engine this Core can run, every plan stays inside its price.

The engines price themselves from the published rates in MediaSettings
(media/hosted.py, media/vision.py). These tests do not call any provider; they
pin the rules that turn those prices into what each plan is offered:

  * one creation always fits one request's reservation ceiling;
  * images and videos are counted against one media pool (the windows less the
    chat/agent floor), and together they fit it every day of a week;
  * each video mode is served only by its own class of route: the hosted A14B
    serves "maximum", a per-clip premium API serves only "premium";
  * image readings keep their own share of what the plan can spend a day;
  * FREE never gets video: it has no video mode.
"""
from __future__ import annotations

import pytest

from src.media import modes, plans
from src.media.hosted import FalImageEngine, OpenAIImageEngine
from src.media.registry import build_image_route, build_video_route
from src.media.settings import MediaSettings, media_settings
from src.media.vision import GeminiVisionEngine
from src.services import media_service

PLANS = ["free", "start", "pro", "max", "team"]
IMAGE_ENGINES = {"z-image-on-fal": FalImageEngine(), "images-api": OpenAIImageEngine()}
VIDEO_ROUTES = {"wan-a14b-on-fal": "wan22_a14b@fal", "veo": "veo_3_1_lite@gemini", "xai": "grok_imagine_video@xai"}


@pytest.fixture(autouse=True)
def published_prices(monkeypatch):
    """Price with the defaults (the published rates), whatever the local .env says."""
    monkeypatch.setattr("src.media.hosted.media_settings", lambda: MediaSettings(_env_file=None))
    monkeypatch.setattr("src.media.vision.media_settings", lambda: MediaSettings(_env_file=None))
    monkeypatch.setattr("src.media.xai.media_settings", lambda: MediaSettings(_env_file=None), raising=False)
    media_settings.cache_clear()
    yield
    media_settings.cache_clear()


def _route(name: str):
    return build_video_route(VIDEO_ROUTES[name])


def _pool(plan: str) -> float:
    return plans.sustainable_media_daily_tcu(plan)


@pytest.mark.parametrize("engine", IMAGE_ENGINES)
@pytest.mark.parametrize("plan", PLANS)
def test_images_are_offered_only_as_far_as_the_media_pool_pays_for_them(plan, engine):
    base = plans.get_media_entitlement(plan)
    unit = media_service.image_unit_usd(base, IMAGE_ENGINES[engine])
    count = plans.affordable_daily_count(plan, base.image_daily_limit, unit)

    assert plans.fits_request_ceiling(plan, unit)
    assert 1 <= count <= base.image_daily_limit
    assert count * plans.usd_to_tcu(unit) <= _pool(plan) + 1e-6


@pytest.mark.parametrize("engine", VIDEO_ROUTES)
@pytest.mark.parametrize("plan", PLANS)
def test_each_mode_is_offered_only_as_far_as_the_plan_pays_for_it(plan, engine):
    base = plans.get_media_entitlement(plan)
    for mode_id in base.video_modes:
        mode = modes.MODES[mode_id]
        offer = media_service.video_offer(plan, base, _route(engine), mode)
        if offer is None:
            continue
        duration, unit = offer
        count = plans.affordable_daily_count(plan, base.mode_cap(mode_id), unit)
        assert 1 <= duration <= mode.max_seconds
        assert plans.fits_request_ceiling(plan, unit)
        assert 1 <= count <= base.mode_cap(mode_id)
        assert count * plans.usd_to_tcu(unit) <= _pool(plan) + 1e-6


@pytest.mark.parametrize("plan", PLANS)
def test_a_premium_api_serves_only_the_premium_mode(plan):
    base = plans.get_media_entitlement(plan)
    for name in ("veo", "xai"):
        for mode in (modes.ECONOMY, modes.QUALITY, modes.MAXIMUM):
            assert media_service.video_offer(plan, base, _route(name), mode) is None
        offer = media_service.video_offer(plan, base, _route(name), modes.PREMIUM)
        assert (offer is not None) == (plan in ("max", "team"))


def test_xai_premium_on_max_costs_35_tcu_for_5_seconds_and_fits_its_ceiling():
    duration, usd = media_service.video_offer("max", plans.get_media_entitlement("max"), _route("xai"), modes.PREMIUM)
    assert duration == 5 and plans.usd_to_tcu(usd) == pytest.approx(35.0)
    assert plans.fits_request_ceiling("max", usd)


def test_the_hosted_a14b_serves_only_maximum_and_only_where_it_fits_a_request():
    route = _route("wan-a14b-on-fal")
    for plan in PLANS:
        base = plans.get_media_entitlement(plan)
        assert media_service.video_offer(plan, base, route, modes.ECONOMY) is None
        assert media_service.video_offer(plan, base, route, modes.QUALITY) is None
    # At the hosted per-second price 720p does not fit PRO's 11 TCU request ceiling; it does fit MAX.
    assert media_service.video_offer("pro", plans.get_media_entitlement("pro"), route, modes.MAXIMUM) is None
    assert media_service.video_offer("max", plans.get_media_entitlement("max"), route, modes.MAXIMUM) is not None


@pytest.mark.parametrize("engine", VIDEO_ROUTES)
def test_free_never_gets_video_on_any_engine(engine):
    base = plans.get_media_entitlement("free")
    assert media_service.video_offer("free", base, _route(engine)) is None
    for mode in modes.MODES.values():
        assert media_service.video_offer("free", base, _route(engine), mode) is None


@pytest.mark.parametrize("plan", PLANS)
def test_image_readings_fit_every_plan_and_their_share(plan):
    unit = GeminiVisionEngine().estimate_usd()
    limit = plans.vision_daily_limit(plan, unit)
    assert plans.fits_request_ceiling(plan, unit)
    assert limit >= 1
    assert limit * plans.usd_to_tcu(unit) <= plans.CREATION_SHARE_OF_DAILY_WINDOW * plans.sustainable_daily_tcu(plan) + 1e-6


@pytest.mark.parametrize("image_name", IMAGE_ENGINES)
@pytest.mark.parametrize("video_name", VIDEO_ROUTES)
@pytest.mark.parametrize("plan", PLANS)
def test_a_week_of_maximum_creation_stays_inside_the_media_pool(plan, image_name, video_name):
    base = plans.get_media_entitlement(plan)
    image_unit = media_service.image_unit_usd(base, IMAGE_ENGINES[image_name])
    images = plans.affordable_daily_count(plan, base.image_daily_limit, image_unit)
    offers = {mode_id: media_service.video_offer(plan, base, _route(video_name), modes.MODES[mode_id])
              for mode_id in base.video_modes}
    offers = {mode_id: offer for mode_id, offer in offers.items() if offer is not None}
    videos, video_unit = 0, 0.0
    if offers:
        sizing = base.default_video_mode if base.default_video_mode in offers else next(iter(offers))
        video_unit = offers[sizing][1]
        videos = plans.affordable_daily_count(plan, base.video_daily_limit, video_unit)
    images, videos = plans.joint_daily_counts(plan, images, image_unit, videos, video_unit)
    daily_tcu = images * plans.usd_to_tcu(image_unit) + videos * plans.usd_to_tcu(video_unit)

    assert daily_tcu * 7 <= plans.media_weekly_pool_tcu(plan) + 1e-6, f"{plan}: {daily_tcu * 7:.2f} TCU a week"
    assert daily_tcu <= plans.media_daily_pool_tcu(plan) + 1e-6


@pytest.mark.parametrize("plan, target", [("free", 3), ("start", 25), ("pro", 50), ("max", 100), ("team", 100)])
def test_the_economical_image_route_reaches_every_plans_commercial_target(plan, target):
    """gpt-image-1-mini at the standard tier (low): ≈ 0.35 TCU in its dearest format, 0.24 square."""
    base = plans.get_media_entitlement(plan)
    route = build_image_route("gpt_image_1_mini@openai")
    unit = media_service.image_unit_usd(base, route)
    assert base.image_quality_tier == "standard"  # the route is grade 3: a "premium" tier would exclude it
    assert plans.usd_to_tcu(unit) == pytest.approx(0.35, abs=0.01)
    assert plans.affordable_daily_count(plan, base.image_daily_limit, unit) == target
    if plan in ("max", "team"):
        # Today MAX's only video is premium (35 TCU, 2 a day): both still fit the pool side by side.
        assert plans.joint_daily_counts(plan, target, unit, base.mode_cap("premium"), 0.35) == (target, 2)
