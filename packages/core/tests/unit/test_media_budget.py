"""CP36: media/economics.py (premium media operations' own budget) in isolation — no DB, no
HTTP. The full create_operation_job path (plan gating + budget + reservation + settlement,
through the real API) is covered by tests/integration/test_media_operations.py's
"Media Economics" section. tests/unit/test_media_economics.py (pre-existing, CP-earlier) covers
the unrelated GENERATE-path economics in media/plans.py — this file is deliberately separate.
"""
from __future__ import annotations

from src.media import economics, plans


def test_start_and_pro_are_not_allowed_character_animate_at_all():
    assert economics.operation_allowed("character_animate", "start") is False
    assert economics.operation_allowed("character_animate", "pro") is False


def test_max_and_team_are_allowed_character_animate():
    assert economics.operation_allowed("character_animate", "max") is True
    assert economics.operation_allowed("character_animate", "team") is True


def test_free_is_not_allowed_character_animate():
    assert economics.operation_allowed("character_animate", "free") is False


def test_an_operation_outside_the_premium_set_is_allowed_on_every_plan():
    """background_remove/vectorize are not gated by this module at all — every plan passes,
    exactly as before economics.py existed."""
    for plan in ("free", "start", "pro", "max", "team", "anything-unknown"):
        assert economics.operation_allowed("background_remove", plan) is True
        assert economics.fits_media_request_ceiling("background_remove", plan, 999.0) is True
        assert economics.fits_media_daily_ceiling("background_remove", plan, 999.0, 999.0) is True


def test_genjutsu_3s_480p_fits_max_request_ceiling_but_not_pro_or_start():
    cost = 3 * 0.318  # $0.954, the CP35/CP36 dry-run case
    assert economics.fits_media_request_ceiling("character_animate", "max", cost) is True
    assert economics.fits_media_request_ceiling("character_animate", "team", cost) is True
    # PRO/START never reach a cost check in practice (operation_allowed already refuses them),
    # but the ceiling function itself is honest about the number too: their MediaBudget has a
    # zero request ceiling.
    assert economics.fits_media_request_ceiling("character_animate", "pro", cost) is False
    assert economics.fits_media_request_ceiling("character_animate", "start", cost) is False


def test_genjutsu_5s_720p_exceeds_the_max_request_ceiling():
    cost = 5 * 0.681  # $3.405 — the platform's own existing 5s max-plan video duration, at 720p
    assert economics.fits_media_request_ceiling("character_animate", "max", cost) is False


def test_media_request_ceiling_is_independent_of_the_real_fits_request_ceiling():
    """The whole point of CP36: a cost that the REAL, unmodified plans.fits_request_ceiling
    would refuse for every plan (max_tcu_per_request: 11 TCU PRO, 60 TCU MAX) is admitted by
    the SEPARATE premium-media ceiling for MAX — proving media economics does not call, alter,
    or route through fits_request_ceiling at all, for the operation it governs."""
    cost = 3 * 0.318  # 95.4 TCU
    assert plans.fits_request_ceiling("max", cost) is False  # the real, untouched ceiling refuses it
    assert plans.fits_request_ceiling("pro", cost) is False
    assert economics.fits_media_request_ceiling("character_animate", "max", cost) is True  # the media one allows it


def test_media_daily_ceiling_admits_a_first_request_then_refuses_a_second_that_would_exceed_it():
    cost = 3 * 0.318  # 95.4 TCU; MAX's daily_ceiling_tcu is 250.0
    assert economics.fits_media_daily_ceiling("character_animate", "max", 0.0, cost) is True
    # Already spent 95.4 today; another 95.4 would total 190.8 — still under 250.
    assert economics.fits_media_daily_ceiling("character_animate", "max", cost, cost) is True
    # A third of the same size would total 286.2 — over 250.
    assert economics.fits_media_daily_ceiling("character_animate", "max", cost * 2, cost) is False


def test_media_daily_ceiling_of_none_never_refuses():
    budget = economics.MediaBudget("custom", frozenset({"character_animate"}), 100.0, None)
    economics.MEDIA_BUDGETS["custom-test-plan"] = budget
    try:
        assert economics.fits_media_daily_ceiling("character_animate", "custom-test-plan", 1_000_000.0, 1.0) is True
    finally:
        del economics.MEDIA_BUDGETS["custom-test-plan"]


def test_budget_for_unknown_plan_falls_back_to_free_and_denies():
    budget = economics.budget_for("this-plan-does-not-exist")
    assert budget.allowed_operations == frozenset()
    assert economics.operation_allowed("character_animate", "this-plan-does-not-exist") is False
