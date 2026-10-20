"""
Integration tests — GET /users/me/entitlement (Fase F criterion #3).

Tests:
  1. entitlement_requires_auth          — 401 without token
  2. entitlement_returns_expected_shape — correct top-level keys + nested sections
  3. start_plan_flags                   — start: CODE=True, WORK=False
  4. free_plan_flags                    — free: agent=False, CODE=False, WORK=False
  5. start_plan_resource_limits         — max_projects=10, max_agents=5
  6. entitlement_to_dict_max_plan       — max: unlimited quotas, all features enabled
"""
from __future__ import annotations

import pytest

from src.entitlements import PLAN_ENTITLEMENTS, entitlement_to_dict, get_plan_entitlement


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── endpoint tests ────────────────────────────────────────────────────────────

def test_entitlement_requires_auth(client):
    resp = client.get("/users/me/entitlement")
    assert resp.status_code == 401


def test_entitlement_returns_expected_shape(client, registered_user):
    token = registered_user["token"]
    resp = client.get("/users/me/entitlement", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "plan" in data
    assert "quota" in data
    assert "features" in data
    assert "limits" in data

    quota = data["quota"]
    assert "daily_tcu_limit" in quota
    assert "weekly_tcu_limit" in quota
    assert "monthly_tcu_limit" in quota
    assert "max_tcu_per_request" in quota

    features = data["features"]
    assert "supports_agent_mode" in features
    assert "supports_trinity_code" in features
    assert "supports_trinity_work" in features
    assert "supports_image_generation" in features

    limits = data["limits"]
    assert "max_projects" in limits
    assert "max_agents" in limits


def test_start_plan_has_code_but_not_work(client, registered_user):
    """A START subscriber: CODE=True, WORK=False. (A new signup is FREE — see
    test_free_plan_protection.py; `registered_user` is a START customer.)"""
    token = registered_user["token"]
    resp = client.get("/users/me/entitlement", headers=_auth(token))
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert features["supports_trinity_code"] is True
    assert features["supports_trinity_work"] is False
    assert features["supports_agent_mode"] is True


def test_start_plan_resource_limits(client, registered_user):
    token = registered_user["token"]
    resp = client.get("/users/me/entitlement", headers=_auth(token))
    assert resp.status_code == 200
    limits = resp.json()["limits"]
    assert limits["max_projects"] == 10
    assert limits["max_agents"] == 5


# ── unit tests (entitlement dataclass / dict) ─────────────────────────────────

def test_free_plan_no_agent_no_code_no_work():
    e = get_plan_entitlement("free")
    assert e.supports_agent_mode is False
    assert e.supports_trinity_code is False
    assert e.supports_trinity_work is False
    assert e.max_projects == 3
    assert e.max_agents == 0


def test_max_plan_unlocks_every_feature_but_is_not_unlimited():
    """Renamed from "all features unlimited". Features are unrestricted on
    MAX; usage is not. It used to carry no daily or weekly ceiling at all,
    which was an unbounded provider-cost exposure on a fixed-price plan."""
    e = get_plan_entitlement("max")
    assert e.supports_agent_mode is True
    assert e.supports_trinity_code is True
    assert e.supports_trinity_work is True
    assert e.max_projects is None
    assert e.max_agents is None
    # Usage is bounded on three complementary windows.
    assert e.session_tcu_limit is not None
    assert e.daily_tcu_limit is not None
    assert e.weekly_tcu_limit is not None


def test_entitlement_to_dict_structure():
    e = get_plan_entitlement("pro")
    d = entitlement_to_dict(e)
    assert d["plan"] == "pro"
    assert d["features"]["supports_trinity_work"] is True
    assert d["quota"]["weekly_tcu_limit"] == e.weekly_tcu_limit
    assert d["quota"]["weekly_tcu_limit"] is not None
    assert d["limits"]["max_projects"] == 50


def test_all_plans_have_entitlement_defined():
    for plan in ("free", "start", "pro", "max", "team"):
        e = get_plan_entitlement(plan)
        assert e.plan == plan, f"plan field mismatch for {plan}"
        d = entitlement_to_dict(e)
        assert d["plan"] == plan
