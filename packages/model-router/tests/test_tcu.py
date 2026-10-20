"""Tests for TCU calculation — all server-side, no client influence."""
from __future__ import annotations

import pytest

from trinity_router.tcu import (
    TCU_EXCHANGE_RATE,
    calculate_actual_cost_usd,
    calculate_tcu,
    estimate_cost_usd,
)


class TestCalculateTcu:
    def test_zero_cost_gives_zero_tcu(self):
        assert calculate_tcu(0.0) == 0.0

    def test_one_cent_gives_one_tcu(self):
        assert calculate_tcu(0.01) == pytest.approx(1.0, rel=1e-4)

    def test_exchange_rate_applied(self):
        cost = 0.05
        assert calculate_tcu(cost) == pytest.approx(cost * TCU_EXCHANGE_RATE, rel=1e-6)

    def test_negative_cost_raises(self):
        with pytest.raises(ValueError):
            calculate_tcu(-0.01)

    def test_result_is_rounded(self):
        # Should not have excessive decimal places
        result = calculate_tcu(0.001234567890)
        assert len(str(result).split(".")[-1]) <= 4


class TestCalculateActualCostUsd:
    def test_zero_tokens_zero_cost(self):
        cost = calculate_actual_cost_usd(3.0, 15.0, 0, 0)
        assert cost == 0.0

    def test_one_million_input_tokens(self):
        cost = calculate_actual_cost_usd(
            cost_per_1m_input_usd=3.0,
            cost_per_1m_output_usd=15.0,
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert cost == pytest.approx(3.0, rel=1e-6)

    def test_one_million_output_tokens(self):
        cost = calculate_actual_cost_usd(
            cost_per_1m_input_usd=3.0,
            cost_per_1m_output_usd=15.0,
            input_tokens=0,
            output_tokens=1_000_000,
        )
        assert cost == pytest.approx(15.0, rel=1e-6)

    def test_combined_tokens(self):
        cost = calculate_actual_cost_usd(3.0, 15.0, 100, 50)
        expected = (100 / 1_000_000 * 3.0) + (50 / 1_000_000 * 15.0)
        assert cost == pytest.approx(expected, rel=1e-6)

    def test_negative_input_raises(self):
        with pytest.raises(ValueError):
            calculate_actual_cost_usd(3.0, 15.0, -1, 0)

    def test_negative_output_raises(self):
        with pytest.raises(ValueError):
            calculate_actual_cost_usd(3.0, 15.0, 0, -1)


class TestEstimateCostUsd:
    def test_estimate_is_conservative(self):
        actual = calculate_actual_cost_usd(3.0, 15.0, 2_000, 1_000)
        estimate = estimate_cost_usd(3.0, 15.0, estimated_input_tokens=2_000, max_output_tokens=4_096)
        # Estimate uses max_output_tokens (4096 > actual 1000) so must be larger
        assert estimate >= actual

    def test_negative_inputs_raise(self):
        with pytest.raises(ValueError):
            estimate_cost_usd(3.0, 15.0, estimated_input_tokens=-1)
