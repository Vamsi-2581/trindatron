"""Tests for per-plan limits enforcement."""
from __future__ import annotations

import pytest

from trinity_router.limits import (
    BudgetExceededError,
    PLAN_LIMITS,
    enforce_request_limits,
    get_plan_limits,
)


class TestGetPlanLimits:
    def test_known_plans_returned(self):
        for plan in ("start", "pro", "max"):
            limits = get_plan_limits(plan)
            assert limits.plan == plan

    def test_unknown_plan_falls_back_to_start(self):
        limits = get_plan_limits("enterprise_premium_ultra")
        assert limits.plan == "start"

    def test_case_insensitive(self):
        assert get_plan_limits("PRO").plan == "pro"
        assert get_plan_limits("MAX").plan == "max"


class TestEnforceRequestLimits:
    def test_tokens_capped_to_plan_limit(self):
        limits = get_plan_limits("start")
        # Request 100k — must be capped to plan limit
        effective = enforce_request_limits(limits, requested_max_tokens=100_000, estimated_cost_usd=0.001)
        assert effective == limits.max_output_tokens_per_request

    def test_requested_tokens_below_limit_respected(self):
        limits = get_plan_limits("start")
        low_request = limits.max_output_tokens_per_request // 2
        effective = enforce_request_limits(limits, requested_max_tokens=low_request, estimated_cost_usd=0.001)
        assert effective == low_request

    def test_none_max_tokens_uses_plan_limit(self):
        limits = get_plan_limits("start")
        effective = enforce_request_limits(limits, requested_max_tokens=None, estimated_cost_usd=0.001)
        assert effective == limits.max_output_tokens_per_request

    def test_cost_exceeding_limit_raises(self):
        limits = get_plan_limits("start")
        over_budget = limits.max_estimated_cost_per_request_usd + 0.01
        with pytest.raises(BudgetExceededError) as exc_info:
            enforce_request_limits(limits, requested_max_tokens=None, estimated_cost_usd=over_budget)
        assert exc_info.value.limit_type == "cost_per_request"
        assert exc_info.value.user_plan == "start"

    def test_cost_at_limit_is_allowed(self):
        limits = get_plan_limits("start")
        enforce_request_limits(
            limits,
            requested_max_tokens=None,
            estimated_cost_usd=limits.max_estimated_cost_per_request_usd,
        )  # must not raise

    def test_pro_has_higher_limits_than_start(self):
        start = get_plan_limits("start")
        pro = get_plan_limits("pro")
        assert pro.max_output_tokens_per_request > start.max_output_tokens_per_request
        assert pro.max_estimated_cost_per_request_usd > start.max_estimated_cost_per_request_usd

    def test_max_has_higher_limits_than_pro(self):
        pro = get_plan_limits("pro")
        max_ = get_plan_limits("max")
        assert max_.max_output_tokens_per_request > pro.max_output_tokens_per_request


class TestBudgetExceededError:
    def test_message_is_generic(self):
        err = BudgetExceededError("cost_per_request", "start")
        # Should mention limit type and plan, but not dollar amounts
        assert "cost_per_request" in str(err)
        assert "start" in str(err)
        assert "$" not in str(err)

    def test_attributes_accessible(self):
        err = BudgetExceededError("tokens_per_request", "pro")
        assert err.limit_type == "tokens_per_request"
        assert err.user_plan == "pro"
