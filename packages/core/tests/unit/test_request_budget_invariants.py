"""Every model call is held to a budget, and that budget can pay for the plan.

Chat, agent, CODE, WORK and the durable execution providers hold each call
to its reservation (RoutingRequest.max_cost_usd_override). That only works if

* every call site really passes it — the execution providers did not, until
  the 2026-09-13 pre-launch audit, so a job step could spend the router's
  whole cap against a reservation clamped far below it; and
* the reservation can pay for the models the plan is sold with — MAX reserved
  30 TCU ($0.30) while one Claude Opus call at MAX's output cap is estimated at
  $0.42, so PREMIUM could never be served in chat or agent mode, with or
  without provider credit. PRO's Sonnet fallback ($0.086) did not fit 8 TCU.

Raising a plan's quality ceiling, its output cap or a model's price without
raising the reservation fails here, before customers find it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.entitlements import PLAN_ENTITLEMENTS
from trinity_router.limits import PLAN_LIMITS
from trinity_router.providers import PROVIDER_CATALOG, QUALITY_CLASS_ORDER
from trinity_router.tcu import estimate_cost_usd

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("plan", sorted(PLAN_ENTITLEMENTS))
def test_a_reservation_pays_for_every_model_the_plan_is_entitled_to(plan):
    limits = PLAN_LIMITS[plan]
    budget_usd = PLAN_ENTITLEMENTS[plan].max_tcu_per_request / 100.0
    ceiling = QUALITY_CLASS_ORDER.index(limits.max_quality_class)
    for key, cfg in PROVIDER_CATALOG.items():
        if QUALITY_CLASS_ORDER.index(cfg.quality_class) > ceiling:
            continue
        estimate = estimate_cost_usd(
            cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd,
            max_output_tokens=limits.max_output_tokens_per_request,
        )
        assert estimate <= budget_usd, (
            f"{plan}: one {key} call is estimated at ${estimate:.4f} but the reservation "
            f"holds ${budget_usd:.2f} — the call would be skipped every time"
        )


@pytest.mark.parametrize("plan", sorted(PLAN_ENTITLEMENTS))
def test_a_reservation_never_authorises_more_than_the_router_cap(plan):
    assert PLAN_ENTITLEMENTS[plan].max_tcu_per_request <= PLAN_LIMITS[plan].max_estimated_cost_per_request_usd * 100 + 1e-9


def _routing_requests_without_budget() -> list[str]:
    missing = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name != "RoutingRequest":
                continue
            if not any(k.arg == "max_cost_usd_override" for k in node.keywords):
                missing.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return missing


def test_every_model_call_in_core_is_held_to_its_reservation():
    assert _routing_requests_without_budget() == []
