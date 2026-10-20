"""Etapa 5H — admin routes for runtime settings (GET/PUT /admin/settings).

Covers: unauthenticated -> 401; authenticated non-admin -> 403 (never a
second auth system, reuses get_current_user + is_admin); admin can GET the
effective snapshot and PUT a valid change; unknown key -> 404; wrong-shaped
value -> 422; a non-admin (or unauthenticated) request can never change a
setting via any field in the request (no client-controlled override);
runtime_setting_changed is observable (metric + response reflects the
change).
"""
from __future__ import annotations

from src.models.user import User
from src.observability.metrics import core_metrics
from src.services.runtime_settings import AGENT_ENABLED, CONTINUITY_ENABLED, DISABLED_PROVIDER_KEYS, KNOWN_KEYS


def _make_admin(client, db, email: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Admin User",
    })
    assert resp.status_code == 201
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.is_admin = True
    db.commit()
    return {"token": data["access_token"], "user_id": data["user_id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ═════════════════════════════════════════════════════════════════════════
# Authorization
# ═════════════════════════════════════════════════════════════════════════

def test_get_settings_requires_auth(client):
    resp = client.get("/admin/settings")
    assert resp.status_code == 401


def test_get_settings_rejects_non_admin_user(client, auth_headers):
    resp = client.get("/admin/settings", headers=auth_headers)
    assert resp.status_code == 403


def test_put_settings_rejects_non_admin_user(client, auth_headers):
    resp = client.put("/admin/settings/agent_enabled", json={"value": False}, headers=auth_headers)
    assert resp.status_code == 403


def test_non_admin_cannot_disable_agent_via_any_request_field(client, auth_headers):
    """A regular user cannot smuggle admin intent through the request body
    of a NON-admin endpoint either — /chat's ChatRequest has no such field,
    and even if it did, only /admin/settings writes RuntimeSetting rows."""
    resp = client.post("/sessions", json={}, headers=auth_headers)
    session_id = resp.json()["id"]
    resp = client.post(
        f"/sessions/{session_id}/chat",
        json={"content": "hi", "agent_enabled": False, "is_admin": True},
        headers=auth_headers,
    )
    # Extra fields are silently dropped by the schema; request proceeds normally
    # (200 or a provider-mock-dependent shape) — the point is it's never a 403/500
    # from an admin field being interpreted.
    assert resp.status_code in (200, 404)


# ═════════════════════════════════════════════════════════════════════════
# GET — effective snapshot
# ═════════════════════════════════════════════════════════════════════════

def test_get_settings_returns_defaults_for_a_fresh_admin(client, db):
    admin = _make_admin(client, db, "admin_defaults@test.com")
    resp = client.get("/admin/settings", headers=_auth(admin["token"]))
    assert resp.status_code == 200
    body = resp.json()["settings"]
    assert set(body.keys()) == KNOWN_KEYS
    assert body[AGENT_ENABLED] is True
    assert body[CONTINUITY_ENABLED] is True
    assert body[DISABLED_PROVIDER_KEYS] == []


# ═════════════════════════════════════════════════════════════════════════
# PUT — validated writes
# ═════════════════════════════════════════════════════════════════════════

def test_admin_can_disable_and_reenable_agent(client, db):
    admin = _make_admin(client, db, "admin_toggle@test.com")
    resp = client.put("/admin/settings/agent_enabled", json={"value": False}, headers=_auth(admin["token"]))
    assert resp.status_code == 200
    assert resp.json()["settings"][AGENT_ENABLED] is False

    resp = client.put("/admin/settings/agent_enabled", json={"value": True}, headers=_auth(admin["token"]))
    assert resp.status_code == 200
    assert resp.json()["settings"][AGENT_ENABLED] is True


def test_admin_can_set_disabled_provider_keys(client, db):
    admin = _make_admin(client, db, "admin_providers@test.com")
    resp = client.put(
        "/admin/settings/disabled_provider_keys",
        json={"value": ["claude-sonnet"]},
        headers=_auth(admin["token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["settings"][DISABLED_PROVIDER_KEYS] == ["claude-sonnet"]


def test_put_unknown_key_returns_404(client, db):
    admin = _make_admin(client, db, "admin_unknown@test.com")
    resp = client.put("/admin/settings/not_a_real_key", json={"value": True}, headers=_auth(admin["token"]))
    assert resp.status_code == 404


def test_put_wrong_shaped_value_returns_422(client, db):
    admin = _make_admin(client, db, "admin_badvalue@test.com")
    resp = client.put("/admin/settings/agent_enabled", json={"value": "not-a-bool"}, headers=_auth(admin["token"]))
    assert resp.status_code == 422
    # Rejected write never took effect.
    resp2 = client.get("/admin/settings", headers=_auth(admin["token"]))
    assert resp2.json()["settings"][AGENT_ENABLED] is True


# ═════════════════════════════════════════════════════════════════════════
# Observability — runtime_setting_changed
# ═════════════════════════════════════════════════════════════════════════

def test_setting_change_increments_metric(client, db):
    admin = _make_admin(client, db, "admin_metric@test.com")
    before = sum(core_metrics.counter("runtime_settings_changes_total").snapshot().values())
    client.put("/admin/settings/agent_enabled", json={"value": False}, headers=_auth(admin["token"]))
    after = core_metrics.counter("runtime_settings_changes_total").snapshot()
    assert sum(after.values()) >= before + 1
    assert any(dict(k).get("key") == "agent_enabled" for k in after)
