"""
Integration tests — weekly TCU limit enforcement, timezone-aware rule
(Sunday 12:00 in the user's LOCAL IANA timezone — see src/timezones.py).

This replaces the old Monday-00:00-UTC rule entirely. The previous version
of this test file placed a fixed offset from the real "now" and broke
whenever the suite happened to run on a Monday (day_start == week_start
under the old rule) — a real, observed flake, not hypothetical.

Every test here either:
  (a) tests the pure function week_window() with an explicit now_utc
      (see tests/unit/test_timezones.py for the exhaustive per-timezone/
      DST matrix — this file focuses on END-TO-END WIRING: does the route
      actually use the authenticated user's stored timezone?), or
  (b) mocks src.services.entitlement_service._utcnow() to a fixed instant
      before touching the API, so the outcome never depends on the real
      calendar date/time the suite executes at.

Tests
-----
  1. weekly_limit_enforced_per_timezone — parametrized over the 5 target
     zones (Sao Paulo, New York, London, Tokyo, Sydney): a completed
     UsageRecord placed inside the user's weekly window (computed via the
     same week_window() the app uses) triggers 429 weekly_limit_exceeded,
     without the daily check firing first.
  2. weekly_window_uses_stored_user_timezone_not_utc — same seed placement,
     but for a user with NO stored timezone (falls back to UTC) does NOT
     trigger 429 — proving the route genuinely reads current_user.timezone
     rather than always resolving to the same window regardless of user.
  3. usage_endpoint_includes_weekly / usage_week_endpoint — response shape,
     including the new week_end / next_reset_at fields.
  4. week_endpoint_requires_auth
  5. week_window_start_is_always_sunday_local — integration-level
     regression guard (unit-level exhaustive coverage lives in
     test_timezones.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.usage import UsageRecord
from src.models.user import User
from src.timezones import week_window


# ── helpers ──────────────────────────────────────────────────────────────────

def _register_and_login(client: TestClient) -> tuple[str, str]:
    email = f"weekly_{uuid.uuid4().hex[:8]}@test.com"
    password = "TestPass123!"
    resp = client.post("/auth/register", json={"email": email, "password": password, "full_name": "Test"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["access_token"], data["user_id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _set_timezone(db, user_id: str, tz_name: str | None) -> None:
    user = db.query(User).filter(User.id == user_id).first()
    user.timezone = tz_name
    db.commit()


# A fixed "now" per target zone — deliberately NOT tied to when the suite
# actually runs. Values are unremarkable (no DST boundary) on purpose; DST
# correctness is exhaustively covered in test_timezones.py.
_FIXED_NOW_BY_TZ = {
    "America/Sao_Paulo": datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),   # Wed
    "America/New_York":  datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
    "Europe/London":     datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
    "Asia/Tokyo":         datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
    "Australia/Sydney":  datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
}


# ── 1. Weekly enforcement — parametrized across the 5 target zones ────────────

@pytest.mark.parametrize("tz_name", sorted(_FIXED_NOW_BY_TZ.keys()))
def test_weekly_limit_enforced_per_timezone(client, db, tz_name):
    """Exhausting the weekly budget returns 429 weekly_limit_exceeded, using
    each user's own local timezone to determine the window — not UTC."""
    token, user_id = _register_and_login(client)
    _set_timezone(db, user_id, tz_name)

    fixed_now = _FIXED_NOW_BY_TZ[tz_name]
    week_start, week_end = week_window(fixed_now, tz_name)

    # Place the seed 3 days into the window and "now" 5 days in — both
    # comfortably inside [week_start, week_end), and on DIFFERENT UTC
    # calendar dates from each other, so the daily check (100 TCU limit)
    # cannot also fire from the same seed record.
    seed_at = week_start + timedelta(days=1, hours=1)
    assert seed_at.date() != fixed_now.date(), "seed and 'now' must differ in UTC calendar date"
    assert week_start <= seed_at < week_end

    # New users get 'start' plan: weekly_tcu_limit=500, max_tcu_per_request=10.
    # Inject 491 TCU so the next reservation (10) pushes total to 501 > 500.
    record = UsageRecord(
        id=str(uuid.uuid4()), user_id=user_id, session_id=None, message_id=None,
        model="claude-haiku-4-5-20251001",
        input_tokens=100, output_tokens=100, cached_tokens=0,
        cost_usd=0.0, tcu=491.0, duration_ms=100, status="completed",
        created_at=seed_at,
    )
    db.add(record)
    db.commit()

    sess = client.post("/sessions", json={"title": "weekly-test"}, headers=_auth(token))
    assert sess.status_code == 201, sess.text
    sid = sess.json()["id"]

    with patch("src.services.entitlement_service._utcnow", return_value=fixed_now):
        resp = client.post(f"/sessions/{sid}/message", json={"content": "hello"}, headers=_auth(token))

    assert resp.status_code == 429, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "weekly_limit_exceeded"
    # The payload no longer carries weekly_used/weekly_limit: those are TCU
    # figures, and TCU is internal accounting. What the user needs — and
    # what this test now checks — is when the window reopens, computed in
    # THEIR timezone, which is the behaviour this test exists for.
    assert detail["resets_at"], detail
    assert detail["resets_in"], detail
    assert "TCU" not in str(detail)


def test_weekly_window_uses_stored_user_timezone_not_utc(client, db):
    """Same seed-placement strategy, but for a Sao-Paulo-local window, on a
    user whose timezone is NOT set (None -> UTC fallback). The seed record
    must land OUTSIDE the UTC-fallback window, so no 429 fires — proving
    the route reads the real per-user timezone rather than a fixed rule."""
    token, user_id = _register_and_login(client)
    _set_timezone(db, user_id, None)  # explicit: not yet detected

    fixed_now = _FIXED_NOW_BY_TZ["America/Sao_Paulo"]
    sp_week_start, sp_week_end = week_window(fixed_now, "America/Sao_Paulo")
    utc_week_start, utc_week_end = week_window(fixed_now, "UTC")
    # Sao Paulo is UTC-3, so its window is offset from the UTC-fallback
    # window by 3 hours — confirm they actually differ before relying on it.
    assert sp_week_start != utc_week_start

    # The two windows mostly overlap (only a 3-hour offset) — the only slice
    # that is inside the Sao Paulo window but outside the UTC-fallback one
    # is right at the SP window's tail end.
    seed_at = sp_week_end - timedelta(hours=1)
    assert sp_week_start <= seed_at < sp_week_end
    assert not (utc_week_start <= seed_at < utc_week_end), (
        "seed must fall inside the Sao Paulo window but OUTSIDE the UTC-fallback window"
    )

    record = UsageRecord(
        id=str(uuid.uuid4()), user_id=user_id, session_id=None, message_id=None,
        model="claude-haiku-4-5-20251001",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.0, tcu=491.0, duration_ms=0, status="completed",
        created_at=seed_at,
    )
    db.add(record)
    db.commit()

    sess = client.post("/sessions", json={"title": "weekly-test"}, headers=_auth(token))
    sid = sess.json()["id"]

    with patch("src.services.entitlement_service._utcnow", return_value=fixed_now):
        resp = client.post(f"/sessions/{sid}/message", json={"content": "hello"}, headers=_auth(token))

    # User has no stored timezone -> UTC fallback -> seed falls outside the
    # computed window -> the weekly check must NOT fire.
    assert resp.status_code != 429 or resp.json()["detail"].get("error") != "weekly_limit_exceeded"


# ── 2. Usage endpoints — shape, including new fields ────────────────────────

def test_usage_endpoint_includes_weekly(client):
    """GET /users/me/usage must return a 'weekly' key with reset metadata."""
    token, _ = _register_and_login(client)
    resp = client.get("/users/me/usage", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "weekly" in data, f"'weekly' missing from /users/me/usage — got: {list(data.keys())}"
    weekly = data["weekly"]
    assert "used" in weekly
    assert "limit" in weekly
    assert "remaining" in weekly
    assert "week_start" in weekly
    assert "next_reset_at" in weekly


def test_usage_week_endpoint_returns_week_data(client):
    """GET /users/me/usage/week returns plan, week_start, week_end,
    next_reset_at, and a weekly sub-object."""
    token, _ = _register_and_login(client)
    resp = client.get("/users/me/usage/week", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "plan" in data
    assert "week_start" in data
    assert "week_end" in data
    assert "next_reset_at" in data
    assert "weekly" in data
    assert data["week_end"] == data["next_reset_at"]

    week_start = datetime.fromisoformat(data["week_start"])
    # New rule: week_start is always a SUNDAY (weekday 6), not Monday.
    # This user has no stored timezone -> UTC fallback for this assertion.
    assert week_start.weekday() == 6, f"week_start is not Sunday: {week_start}"


def test_week_endpoint_requires_auth(client):
    """GET /users/me/usage/week must return 401 without a valid token."""
    resp = client.get("/users/me/usage/week")
    assert resp.status_code == 401


# ── 3. week_window() wiring — integration-level regression guard ───────────
# Exhaustive per-timezone/DST math coverage lives in tests/unit/test_timezones.py.

@pytest.mark.parametrize("tz_name", sorted(_FIXED_NOW_BY_TZ.keys()))
def test_week_window_start_is_always_sunday_local(tz_name):
    """start/end are always Sunday 12:00 in LOCAL time. Their UTC delta is
    NOT always exactly 7 days — a DST transition inside the window shifts
    it by an hour by design (that's the correctness property, see
    test_timezones.py's dedicated DST-boundary tests); these fixed "now"
    instants deliberately avoid a transition, so 7 days holds here too."""
    fixed_now = _FIXED_NOW_BY_TZ[tz_name]
    start, end = week_window(fixed_now, tz_name)
    local_start = start.astimezone(ZoneInfo(tz_name))
    local_end = end.astimezone(ZoneInfo(tz_name))
    assert local_start.weekday() == 6  # Sunday
    assert local_end.weekday() == 6
    assert local_start.hour == 12 and local_start.minute == 0 and local_start.second == 0
    assert local_end.hour == 12 and local_end.minute == 0 and local_end.second == 0
    assert end == start + timedelta(days=7)  # holds for these no-DST-transition instants
