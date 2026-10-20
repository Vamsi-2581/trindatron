"""Admin view of the usage windows, and team assignment.

Operators need to answer "why was this user refused" and "who shares this
pool" without reading the database by hand. Both routes are admin-only for
the same reason /health/metrics is: these are internal accounting figures,
not product surface.
"""
from __future__ import annotations

from datetime import timedelta

from src.entitlements import get_plan_entitlement
from src.models.user import User
from src.services.entitlement_service import check_and_reserve


def _admin(client, db, email="usage-admin@example.com"):
    data = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Admin",
    }).json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.is_admin = True
    db.commit()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["user_id"]


def _user(client, db, plan="max", email="usage-target@example.com"):
    data = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Target",
    }).json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.plan = plan
    db.commit()
    return data["user_id"], {"Authorization": f"Bearer {data['access_token']}"}


# ── Authorization ───────────────────────────────────────────────────────────


def test_usage_windows_requires_authentication(client, db):
    target, _ = _user(client, db)
    assert client.get(f"/admin/users/{target}/usage-windows").status_code == 401


def test_usage_windows_rejects_a_regular_user(client, db):
    target, headers = _user(client, db)
    assert client.get(f"/admin/users/{target}/usage-windows", headers=headers).status_code == 403


def test_team_assignment_rejects_a_regular_user(client, db):
    target, headers = _user(client, db)
    resp = client.put(f"/admin/users/{target}/team", json={"team_id": "t1"}, headers=headers)
    assert resp.status_code == 403


# ── What an operator sees ───────────────────────────────────────────────────


def test_admin_sees_every_window_with_its_reset_time(client, db):
    headers, _ = _admin(client, db)
    target, _ = _user(client, db, "max")

    body = client.get(f"/admin/users/{target}/usage-windows", headers=headers).json()

    assert body["plan"] == "max"
    for window in ("session", "daily", "weekly"):
        assert window in body
        assert "used_tcu" in body[window]
        assert "limit_tcu" in body[window]
        assert "remaining_tcu" in body[window]
        assert "percent_used" in body[window]
    assert body["daily"]["limit_tcu"] == get_plan_entitlement("max").daily_tcu_limit
    assert body["daily"]["resets_at"], "an operator needs to know when it reopens"
    assert body["per_request_tcu_limit"] == get_plan_entitlement("max").max_tcu_per_request


def test_percent_used_tracks_real_consumption(client, db):
    headers, _ = _admin(client, db)
    target, target_headers = _user(client, db, "max")
    session_id = client.post("/sessions", json={}, headers=target_headers).json()["id"]

    before = client.get(f"/admin/users/{target}/usage-windows", headers=headers).json()
    assert before["daily"]["used_tcu"] == 0

    check_and_reserve(db, user_id=target, session_id=session_id, plan="max")

    after = client.get(f"/admin/users/{target}/usage-windows", headers=headers).json()
    assert after["daily"]["used_tcu"] > 0
    assert after["daily"]["percent_used"] > 0
    assert after["session"]["resets_at"], "the 5-hour window opened on first reservation"


def test_an_individual_plan_reports_no_team(client, db):
    headers, _ = _admin(client, db)
    target, _ = _user(client, db, "max")
    body = client.get(f"/admin/users/{target}/usage-windows", headers=headers).json()
    assert body["team"] is None


# ── Team assignment and the shared pool ─────────────────────────────────────


def test_admin_can_put_a_user_into_a_team_and_take_them_out(client, db):
    headers, _ = _admin(client, db)
    target, _ = _user(client, db, "team")

    assigned = client.put(
        f"/admin/users/{target}/team", json={"team_id": "team-alpha"}, headers=headers,
    )
    assert assigned.status_code == 200
    assert assigned.json()["team_id"] == "team-alpha"
    assert db.query(User).filter(User.id == target).one().team_id == "team-alpha"

    cleared = client.put(f"/admin/users/{target}/team", json={"team_id": None}, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["team_id"] is None


def test_a_team_cannot_grow_past_its_member_cap(client, db, monkeypatch):
    import dataclasses

    from src.api.routes import admin as admin_routes

    team = get_plan_entitlement("team")
    monkeypatch.setattr(admin_routes, "get_plan_entitlement",
                        lambda plan: dataclasses.replace(team, team_max_members=2))
    headers, _ = _admin(client, db)
    a, _ = _user(client, db, "team", "cap-a@example.com")
    b, _ = _user(client, db, "team", "cap-b@example.com")
    c, _ = _user(client, db, "team", "cap-c@example.com")
    for uid in (a, b):
        assert client.put(f"/admin/users/{uid}/team", json={"team_id": "team-cap"}, headers=headers).status_code == 200

    refused = client.put(f"/admin/users/{c}/team", json={"team_id": "team-cap"}, headers=headers)
    assert refused.status_code == 409
    assert db.query(User).filter(User.id == c).one().team_id is None

    # Re-assigning an existing member, or taking one out, is never blocked.
    assert client.put(f"/admin/users/{a}/team", json={"team_id": "team-cap"}, headers=headers).status_code == 200
    assert client.put(f"/admin/users/{b}/team", json={"team_id": None}, headers=headers).status_code == 200
    assert client.put(f"/admin/users/{c}/team", json={"team_id": "team-cap"}, headers=headers).status_code == 200


def test_the_real_team_cap_is_set():
    assert get_plan_entitlement("team").team_max_members == 150


def test_team_window_reports_the_shared_pool_and_member_count(client, db):
    headers, _ = _admin(client, db)
    a, _ = _user(client, db, "team", "team-a@example.com")
    b, _ = _user(client, db, "team", "team-b@example.com")
    for uid in (a, b):
        client.put(f"/admin/users/{uid}/team", json={"team_id": "team-alpha"}, headers=headers)

    body = client.get(f"/admin/users/{a}/usage-windows", headers=headers).json()
    assert body["team"] is not None
    assert body["team"]["team_id"] == "team-alpha"
    assert body["team"]["members"] == 2
    assert body["team"]["pool"]["limit_tcu"] == get_plan_entitlement("team").team_weekly_pool_tcu_limit
    assert body["team"]["pool"]["resets_at"]


def test_unknown_user_is_a_404_not_a_crash(client, db):
    headers, _ = _admin(client, db)
    assert client.get("/admin/users/does-not-exist/usage-windows", headers=headers).status_code == 404
    resp = client.put("/admin/users/does-not-exist/team", json={"team_id": "x"}, headers=headers)
    assert resp.status_code == 404
