"""GET /admin/economics — where provider money goes, for operators only.

Spend by plan and provider is internal accounting, like /health/metrics:
never product surface, never visible to a regular account.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.models.usage import UsageRecord
from src.models.user import User


def _register(client, db, email, *, admin=False, plan=None):
    data = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Economics",
    }).json()
    user = db.query(User).filter(User.id == data["user_id"]).one()
    user.is_admin = admin
    if plan:
        user.plan = plan
    db.commit()
    return user, {"Authorization": f"Bearer {data['access_token']}"}


def test_economics_requires_authentication(client):
    assert client.get("/admin/economics").status_code == 401


def test_economics_rejects_a_regular_user(client, db):
    _, headers = _register(client, db, "regular-econ@example.com")
    assert client.get("/admin/economics", headers=headers).status_code == 403


def test_economics_reports_spend_by_plan_and_provider(client, db):
    _, headers = _register(client, db, "admin-econ@example.com", admin=True)
    payer, _ = _register(client, db, "payer-econ@example.com", plan="max")
    db.add(UsageRecord(user_id=payer.id, model="gpt-4o-mini", cost_usd=0.25, tcu=25.0,
                       status="completed", created_at=datetime.now(timezone.utc)))
    db.commit()

    resp = client.get("/admin/economics", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["spend"]["day"]["by_plan_usd"]["max"] == 0.25
    assert body["spend"]["day"]["by_provider_usd"]["openai"] == 0.25
    assert body["accounts_by_plan"]["max"] == 1
    assert body["free_plan_daily_budget_usd"] is not None
    assert body["alerts"] == []
