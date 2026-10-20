"""Media must not break the economics that already hold the product.

Video costs come from media/modes.py (runs measured on Modal, 2026-09-25);
images from the reference image engine below. The tests pin the RULES that keep
any price safe:

  * the weekly ceiling is absolute: the media pool is inside it, and the
    chat/agent floor is what the pool leaves;
  * each plan's commercial reference (images and videos a day in its default
    mode) is paid by its media pool, day by day and week by week;
  * every mode a plan offers fits one request's reservation ceiling, and the
    premium mode (xAI) only exists on MAX/TEAM, never as a default;
  * FREE never gets video, and FREE is bounded plan-wide by
    free_plan_global_daily_budget_usd no matter how many accounts exist.
"""
from __future__ import annotations

import pytest

from src.billing.economics import WEEKS_PER_MONTH
from src.config import settings
from src.entitlements import get_plan_entitlement
from src.media import modes, plans
from src.media.settings import MediaSettings

GPU_USD_PER_HOUR = MediaSettings(_env_file=None).media_gpu_usd_per_hour
DAYS_PER_MONTH = 30
# gpt-image-1-mini, low quality, 1024²: US$ 0.0022 an image (the image route the pool was validated with).
REFERENCE_IMAGE_TCU = 0.22
PAID = ["start", "pro", "max", "team"]


def _image_usd(plan: str) -> float:
    e = plans.get_media_entitlement(plan)
    side = min(1024, e.max_image_resolution)
    return plans.estimate_image_usd(width=side, height=side, quality_tier=e.image_quality_tier, gpu_usd_per_hour=GPU_USD_PER_HOUR)


def _default_video_tcu(plan: str) -> float:
    e = plans.get_media_entitlement(plan)
    mode = modes.get(e.default_video_mode)
    return 0.0 if mode is None else plans.usd_to_tcu(mode.reference_usd(mode.default_seconds))


def _reference_day_tcu(plan: str) -> float:
    e = plans.get_media_entitlement(plan)
    return e.image_daily_limit * REFERENCE_IMAGE_TCU + e.video_daily_limit * _default_video_tcu(plan)


def test_the_commercial_reference_is_what_was_approved():
    table = {plan: (e.image_daily_limit, e.video_daily_limit, e.default_video_mode)
             for plan, e in plans.MEDIA_ENTITLEMENTS.items()}
    assert table["free"] == (3, 0, None)
    assert table["start"] == (25, 5, "economy")
    assert table["pro"] == (50, 15, "economy")
    assert table["max"] == (100, 40, "economy")
    assert table["team"] == table["max"]


def test_mode_costs_follow_the_measured_runs():
    """TCU for 5 s: GPU seconds x Modal's per-second price x 1.30, at US$ 0.01 a TCU."""
    tcu = {mode.id: plans.usd_to_tcu(mode.reference_usd(5)) for mode in modes.MODES.values()}
    assert tcu["economy"] == pytest.approx(8.7 * 0.001097 * 1.3 * 100, abs=1e-3)
    assert tcu["economy"] == pytest.approx(1.24, abs=0.01)
    assert tcu["quality"] == pytest.approx(16.2 * 0.001097 * 1.3 * 100, abs=1e-3)
    assert tcu["quality"] == pytest.approx(2.31, abs=0.01)
    assert tcu["maximum"] == pytest.approx(56.8 * 0.001261 * 1.3 * 100, abs=1e-3)
    assert tcu["maximum"] == pytest.approx(9.31, abs=0.01)
    assert tcu["premium"] == pytest.approx(35.0)
    # A shorter clip costs proportionally less.
    assert modes.QUALITY.reference_usd(3) == pytest.approx(modes.QUALITY.reference_usd(5) * 3 / 5)


@pytest.mark.parametrize("plan", ["free", *PAID])
def test_every_offered_creation_fits_one_requests_ceiling(plan):
    e = plans.get_media_entitlement(plan)
    ceiling = get_plan_entitlement(plan).max_tcu_per_request
    assert plans.usd_to_tcu(_image_usd(plan)) <= ceiling
    assert REFERENCE_IMAGE_TCU <= ceiling
    for mode_id in e.video_modes:
        mode = modes.MODES[mode_id]
        assert plans.usd_to_tcu(mode.reference_usd(mode.max_seconds)) <= ceiling, (plan, mode_id)


def test_which_plan_may_use_which_mode():
    offered = {plan: plans.get_media_entitlement(plan).video_modes for plan in plans.MEDIA_ENTITLEMENTS}
    assert offered["free"] == ()
    assert offered["start"] == ("economy", "quality")
    assert offered["pro"] == ("economy", "quality", "maximum")
    assert offered["max"] == offered["team"] == ("economy", "quality", "maximum", "premium")
    for plan in plans.MEDIA_ENTITLEMENTS:
        e = plans.get_media_entitlement(plan)
        assert e.default_video_mode != "premium"  # premium is never what a plan runs by default
        assert all(e.mode_cap(mode) >= 1 for mode in e.video_modes)
        assert e.mode_cap(e.default_video_mode or "") <= e.video_daily_limit


def test_the_caps_per_mode():
    caps = {plan: dict(plans.get_media_entitlement(plan).video_mode_caps) for plan in PAID}
    assert caps["start"] == {"economy": 5, "quality": 2}
    assert caps["pro"] == {"economy": 15, "quality": 8, "maximum": 2}
    assert caps["max"] == caps["team"] == {"economy": 40, "quality": 40, "maximum": 8, "premium": 2}


def test_premium_never_fits_start_or_pro_even_if_it_were_offered():
    premium = plans.usd_to_tcu(modes.PREMIUM.reference_usd(5))
    assert premium > get_plan_entitlement("start").max_tcu_per_request
    assert premium > get_plan_entitlement("pro").max_tcu_per_request
    assert premium <= get_plan_entitlement("max").max_tcu_per_request


def test_free_video_is_zero():
    e = plans.get_media_entitlement("free")
    assert e.video_daily_limit == 0 and e.video_modes == ()


@pytest.mark.parametrize("plan", ["free", *PAID])
def test_the_weekly_ceiling_is_absolute_and_the_floor_is_what_media_leaves(plan):
    weekly = get_plan_entitlement(plan).weekly_tcu_limit
    daily = get_plan_entitlement(plan).daily_tcu_limit
    floor_daily, floor_weekly = plans.protected_floor_tcu(plan)
    assert plans.media_weekly_pool_tcu(plan) + floor_weekly == pytest.approx(weekly)
    assert plans.media_daily_pool_tcu(plan) + floor_daily == pytest.approx(daily)
    assert 0 < plans.media_weekly_pool_tcu(plan) < weekly
    assert plans.sustainable_media_daily_tcu(plan) <= plans.media_weekly_pool_tcu(plan) / 7 + 1e-9


def test_the_protected_floors():
    assert plans.protected_floor_tcu("start") == (9.0, 22.0)
    assert plans.protected_floor_tcu("pro") == pytest.approx((12.1, 25.85))
    assert plans.protected_floor_tcu("max") == (35.0, 95.0)
    assert plans.protected_floor_tcu("free") == (5.0, 10.0)


@pytest.mark.parametrize("plan", PAID)
def test_a_paid_plans_reference_week_is_paid_by_its_media_pool(plan):
    """25/50/100 images and 5/15/40 clips in the default mode, every day for a week, inside the pool."""
    day = _reference_day_tcu(plan)
    assert day <= plans.media_daily_pool_tcu(plan) + 1e-9, (plan, day)
    assert day * 7 <= plans.media_weekly_pool_tcu(plan) + 1e-9, (plan, day * 7, plans.media_weekly_pool_tcu(plan))


def test_the_reference_numbers():
    assert _reference_day_tcu("start") == pytest.approx(25 * 0.22 + 5 * 1.24, abs=0.05)   # ≈ 11.7 a day
    assert _reference_day_tcu("pro") == pytest.approx(50 * 0.22 + 15 * 1.24, abs=0.1)     # ≈ 29.6 a day
    assert _reference_day_tcu("max") == pytest.approx(100 * 0.22 + 40 * 1.24, abs=0.3)    # ≈ 72 a day


def test_why_pro_keeps_eleven_percent_and_not_twelve():
    week = _reference_day_tcu("pro") * 7
    weekly = get_plan_entitlement("pro").weekly_tcu_limit
    assert week > weekly * (1 - 0.12)
    assert week <= weekly * (1 - plans.chat_agent_floor_share("pro"))


@pytest.mark.parametrize("plan", PAID)
def test_the_reference_counts_are_what_the_pool_offers_on_reference_engines(plan):
    e = plans.get_media_entitlement(plan)
    assert plans.affordable_daily_count(plan, e.image_daily_limit, REFERENCE_IMAGE_TCU / 100) == e.image_daily_limit
    assert plans.affordable_daily_count(plan, e.video_daily_limit, _default_video_tcu(plan) / 100) == e.video_daily_limit
    images, videos = plans.joint_daily_counts(plan, e.image_daily_limit, REFERENCE_IMAGE_TCU / 100,
                                              e.video_daily_limit, _default_video_tcu(plan) / 100)
    assert (images, videos) == (e.image_daily_limit, e.video_daily_limit)


def test_an_expensive_engine_lowers_the_counts_to_what_the_pool_pays():
    # A 2-TCU image: START's pool (88 TCU a week, 12.57 a day) pays for 6 a day, not 25.
    assert plans.affordable_daily_count("start", 25, 0.02) == 6
    # One creation above a request's ceiling is never offered.
    assert plans.affordable_daily_count("start", 25, 0.11) == 0
    # Both kinds shrink together when side by side they would not fit.
    images, videos = plans.joint_daily_counts("start", 6, 0.02, 5, 0.0124)
    assert images * 2 + videos * 1.24 <= plans.sustainable_media_daily_tcu("start") + 1e-9
    assert images >= 1 and videos >= 1


def test_free_three_images_a_day_fit_its_pool_on_the_reference_engine():
    assert plans.affordable_daily_count("free", 3, REFERENCE_IMAGE_TCU / 100) == 3
    assert 3 * REFERENCE_IMAGE_TCU * 7 <= plans.media_weekly_pool_tcu("free")


@pytest.mark.parametrize("users", [1, 100, 1_000, 10_000])
def test_free_images_are_bounded_plan_wide_whatever_the_number_of_accounts(users):
    per_user_day = plans.get_media_entitlement("free").image_daily_limit * _image_usd("free")
    unbounded_day = users * per_user_day
    budget = settings.free_plan_global_daily_budget_usd
    assert budget is not None, "the FREE plan-wide budget must stay on"
    bounded_day = min(unbounded_day, budget)
    bounded_month = bounded_day * DAYS_PER_MONTH

    # One free account at its image maximum costs cents a month.
    assert per_user_day * DAYS_PER_MONTH < 0.50
    # No number of free accounts can push FREE past the plan-wide budget.
    assert bounded_month <= budget * DAYS_PER_MONTH
    if users >= 10_000:
        assert unbounded_day > budget  # the guardrail is what binds at scale


def test_measured_cost_follows_engine_seconds():
    assert plans.measured_usd(3600, 0.69) == pytest.approx(0.69)
    assert plans.measured_usd(-5, 0.69) == 0.0


def test_the_estimate_scales_with_pixels_duration_and_quality():
    base = plans.estimate_image_usd(width=1024, height=1024, quality_tier="standard", gpu_usd_per_hour=1.0)
    assert plans.estimate_image_usd(width=2048, height=2048, quality_tier="standard", gpu_usd_per_hour=1.0) == pytest.approx(base * 4)
    assert plans.estimate_image_usd(width=1024, height=1024, quality_tier="premium", gpu_usd_per_hour=1.0) == pytest.approx(base * 1.5)
    short = plans.estimate_video_usd(short_side=720, duration_seconds=2, gpu_usd_per_hour=1.0)
    assert plans.estimate_video_usd(short_side=720, duration_seconds=4, gpu_usd_per_hour=1.0) == pytest.approx(short * 2)
    assert WEEKS_PER_MONTH > 4


def test_an_operator_can_lower_or_raise_free_images_without_a_deploy():
    assert plans.get_media_entitlement("free", free_image_daily_limit=1).image_daily_limit == 1
    assert plans.get_media_entitlement("free", free_image_daily_limit=None).image_daily_limit == 3
    # The override never touches paid plans.
    assert plans.get_media_entitlement("pro", free_image_daily_limit=0).image_daily_limit == 50
