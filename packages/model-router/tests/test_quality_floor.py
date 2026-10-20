"""RoutingRequest.min_quality_class: a caller-set floor that can only raise the plan's own, never past its ceiling."""
from __future__ import annotations

import pytest

from trinity_router import RoutingRequest, TaskType
from trinity_router.limits import get_plan_limits
from trinity_router.providers import QUALITY_CLASS_ORDER
from trinity_router.router import ModelRouter, _effective_limits


def _request(plan: str, floor: str | None, **kwargs) -> RoutingRequest:
    return RoutingRequest(messages=[{"role": "user", "content": "Resuma os resultados da pesquisa."}],
                          task_type=TaskType.CHAT, user_plan=plan, session_id="s", min_quality_class=floor, **kwargs)


def test_without_a_floor_the_plan_limits_are_unchanged():
    assert _effective_limits(_request("max", None)) == get_plan_limits("max")


def test_a_floor_raises_the_plans_own():
    assert get_plan_limits("max").min_quality_class == "low"
    assert _effective_limits(_request("max", "standard")).min_quality_class == "standard"


def test_a_floor_never_lowers_the_plans_own():
    assert _effective_limits(_request("pro", "ultra_low")).min_quality_class == get_plan_limits("pro").min_quality_class


@pytest.mark.parametrize("plan", ["start", "pro"])
def test_a_floor_above_the_plans_ceiling_is_clamped_not_refused(plan):
    limits = _effective_limits(_request(plan, "premium"))
    assert limits.min_quality_class == limits.max_quality_class == get_plan_limits(plan).max_quality_class


def test_an_unknown_floor_is_ignored():
    assert _effective_limits(_request("max", "excellent")) == get_plan_limits("max")


def test_the_floor_and_a_budget_override_both_apply():
    limits = _effective_limits(_request("max", "standard", max_cost_usd_override=0.05))
    assert limits.min_quality_class == "standard" and limits.max_estimated_cost_per_request_usd == 0.05


def test_every_model_in_the_chain_meets_the_floor():
    """The floor reaches the real chain decision: nothing below 'standard' is left to answer."""
    request = _request("max", "standard")
    decision = ModelRouter()._build_chain_decision(TaskType.CHAT, "max", _effective_limits(request), "research",
                                                  messages=request.messages)
    rank = QUALITY_CLASS_ORDER.index
    assert decision.chain, "MAX must still have a model at 'standard' or above"
    assert all(rank(model.quality_class) >= rank("standard") for _, model in decision.chain)
    assert "gpt-4o-mini" not in [key for key, _ in decision.chain]
