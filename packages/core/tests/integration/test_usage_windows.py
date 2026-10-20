"""Usage windows: 5-hour session, daily, weekly, shared team pool.

MAX and TEAM used to carry no daily or weekly ceiling at all — an unbounded
provider-cost exposure on a fixed-price plan. Every plan is now bounded on
complementary windows, sized from what its price can actually pay for
(src/billing/economics.py, and tests/unit/test_plan_economics.py for the
derivation) so ordinary use never meets them and automation does.

What these tests are really protecting:
  - every window actually binds, and the shortest one binds first
  - the 5-hour window renews by itself, with no cron and no scheduled job
  - counters survive a process restart, because they are derived from rows
  - an agent loop, a failover or a retry cannot walk around any of it
  - a team shares one budget, and one member cannot strand the rest
  - the user is told WHEN access returns, and never sees "TCU"
  - the lower plans carry the windows that were actually decided for them
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.entitlements import get_plan_entitlement
from src.models.entitlement_session import EntitlementSession
from src.models.usage import UsageRecord
from src.models.user import User
from src.services.entitlement_service import (
    _day_window,
    _week_window,
    check_agent_iteration_budget,
    check_and_reserve,
    finalize_usage,
)


@pytest.fixture
def plan_user(client, db):
    """Create a user on `plan` and return (headers, user_id, session_id).

    Same factory shape as test_entitlement.py's own fixture — duplicated
    rather than imported because pytest fixtures do not cross module
    boundaries without a conftest, and hoisting it into conftest.py would
    widen its blast radius to every suite for the sake of two files.
    """
    counter = {"n": 0}

    def _make(plan: str = "max"):
        counter["n"] += 1
        resp = client.post("/auth/register", json={
            "email": f"windows{counter['n']}@test.com",
            "password": "TestPass123!",
            "full_name": "Window User",
        })
        assert resp.status_code == 201
        token, user_id = resp.json()["access_token"], resp.json()["user_id"]
        headers = {"Authorization": f"Bearer {token}"}

        user = db.query(User).filter(User.id == user_id).first()
        user.plan = plan
        db.commit()

        session_id = client.post("/sessions", json={"title": "t"}, headers=headers).json()["id"]
        return headers, user_id, session_id

    return _make


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed_usage(db, user_id: str, tcu: float, *, when: datetime | None = None) -> UsageRecord:
    """A completed usage row — the same shape finalize_usage writes."""
    rec = UsageRecord(
        user_id=user_id, session_id=None, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.0, tcu=tcu, duration_ms=0, status="completed",
        created_at=when or _utcnow(),
    )
    db.add(rec)
    db.commit()
    return rec


def _session_of(db, user_id: str) -> EntitlementSession:
    return db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).one()


# ── The 5-hour session window ───────────────────────────────────────────────


def test_max_inside_the_session_budget_is_allowed(db, plan_user):
    """Spend inside the window, with room left for the next request.

    The window is opened FIRST and the usage seeded into it, rather than
    seeded before any window exists. Seeding first left it ambiguous
    whether the row landed inside the session at all — the session starts
    at whatever instant check_and_reserve() opens it, and on a clock with
    ~16ms resolution that can be the same instant the row was written. The
    test used to pass either way only because the budget was wide enough
    (800 TCU) that the difference did not change the answer; at 115 it
    does, so the ambiguity became a flake.

    The amount is derived so it stays correct when the plan is re-windowed:
    the reservation costs max_tcu_per_request up front, so "comfortably
    inside" means leaving at least that much headroom.
    """
    _, user_id, session_id = plan_user("max")
    e = get_plan_entitlement("max")

    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    _seed_usage(db, user_id, e.session_tcu_limit - e.max_tcu_per_request * 2)

    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")


def test_max_hitting_the_session_budget_is_refused(db, plan_user):
    _, user_id, session_id = plan_user("max")
    limit = get_plan_entitlement("max").session_tcu_limit

    # Open the window, then fill it.
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    _seed_usage(db, user_id, limit)

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "session_limit_exceeded"


def test_the_session_window_renews_by_itself_after_five_hours(db, plan_user):
    """No cron, no scheduled job: the next request after expiry opens a new
    window, and usage from the old one no longer counts against it."""
    _, user_id, session_id = plan_user("max")
    limit = get_plan_entitlement("max").session_tcu_limit

    # An old window, already expired, that was spent to its ceiling.
    old_start = _utcnow() - timedelta(hours=6)
    window = _session_of(db, user_id) if db.query(EntitlementSession).filter(
        EntitlementSession.user_id == user_id).count() else None
    if window is None:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
        window = _session_of(db, user_id)
    window.session_started_at = old_start
    window.session_expires_at = old_start + timedelta(hours=5)
    db.commit()

    # Usage dated INSIDE that old window — not "now", which would sit on the
    # boundary of the window about to open and make this test race itself.
    _seed_usage(db, user_id, limit, when=old_start + timedelta(minutes=10))

    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max"), (
        "an expired 5-hour window must roll over on the next request"
    )

    # And the rollover is real: the new window starts now, not then.
    refreshed = _session_of(db, user_id)
    assert refreshed.session_started_at > old_start + timedelta(hours=5)


def test_usage_from_an_expired_window_does_not_follow_the_user(db, plan_user):
    """The other half of renewal: spend from a closed window must not be
    charged against the new one."""
    _, user_id, session_id = plan_user("max")
    limit = get_plan_entitlement("max").session_tcu_limit

    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    old_start = _utcnow() - timedelta(hours=9)
    window = _session_of(db, user_id)
    window.session_started_at = old_start
    window.session_expires_at = old_start + timedelta(hours=5)
    db.commit()
    # 1.25x the session budget: comfortably over the window under test, and
    # — together with the reservation already pending above — still under
    # MAX's daily ceiling, otherwise this would pass or fail on the daily
    # window instead, which is a different test.
    _seed_usage(db, user_id, limit * 1.25, when=old_start + timedelta(hours=1))

    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")


def test_the_refusal_says_when_access_returns(db, plan_user):
    _, user_id, session_id = plan_user("max")
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    _seed_usage(db, user_id, get_plan_entitlement("max").session_tcu_limit)

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")

    detail = exc.value.detail
    assert "resets_at" in detail and detail["resets_at"]
    assert detail["resets_in"], "the user needs a human-readable wait, not just a timestamp"
    assert "renovado" in detail["message"]


def test_no_refusal_ever_mentions_the_internal_unit(db, plan_user):
    """TCU is this platform's accounting unit. It stays internal."""
    _, user_id, session_id = plan_user("max")
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    _seed_usage(db, user_id, get_plan_entitlement("max").session_tcu_limit)

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")

    rendered = str(exc.value.detail)
    assert "TCU" not in rendered, rendered
    assert "tcu" not in rendered.lower().replace("tcu_limit", ""), rendered


# ── Daily and weekly ────────────────────────────────────────────────────────


def test_max_daily_window_binds(db, plan_user):
    _, user_id, session_id = plan_user("max")
    e = get_plan_entitlement("max")
    day_start, _ = _day_window()

    # Spread across the day so no single 5-hour window holds it all —
    # this isolates the daily ceiling from the session one.
    _seed_usage(db, user_id, e.daily_tcu_limit, when=day_start + timedelta(hours=1))

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    assert exc.value.detail["error"] in ("daily_limit_exceeded", "session_limit_exceeded")


def test_max_weekly_window_binds(db, plan_user):
    _, user_id, session_id = plan_user("max")
    e = get_plan_entitlement("max")
    week_start, _ = _week_window(None)

    # Earlier in the week, outside today and outside any live session.
    _seed_usage(db, user_id, e.weekly_tcu_limit, when=week_start + timedelta(minutes=5))

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    assert exc.value.status_code == 429


# ── Persistence ─────────────────────────────────────────────────────────────


def test_counters_survive_a_restart(db, plan_user):
    """Nothing about the limit lives in process memory: usage is summed from
    UsageRecord rows and the window from entitlement_sessions, so a Railway
    restart cannot hand anyone a fresh budget."""
    _, user_id, session_id = plan_user("max")
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    _seed_usage(db, user_id, get_plan_entitlement("max").session_tcu_limit)

    # Simulate a restart: drop every identity map / cached object.
    db.expire_all()
    db.close()

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    assert exc.value.status_code == 429


def test_a_cancelled_reservation_does_not_consume_budget(db, plan_user):
    """Failed work must not eat someone's window — the sum excludes
    status='failed', which is what cancel_reservation writes."""
    _, user_id, session_id = plan_user("max")
    rec = _seed_usage(db, user_id, get_plan_entitlement("max").session_tcu_limit)
    rec.status = "failed"
    rec.tcu = 0
    db.commit()

    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")


# ── Agent loops, failover and retries cannot escape ─────────────────────────


def test_agent_loop_is_stopped_by_the_session_window(db, plan_user):
    """The workload most able to spend a budget between two checks."""
    _, user_id, session_id = plan_user("max")
    reservation = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    limit = get_plan_entitlement("max").session_tcu_limit

    assert check_agent_iteration_budget(db, reservation, user_id, "max", 50.0) is True
    assert check_agent_iteration_budget(db, reservation, user_id, "max", limit) is False


def test_one_logical_request_reserves_once_however_many_providers_it_tries(db, plan_user):
    """Failover and Quality Recovery happen inside a single reservation, so
    retrying a second provider must not double-count against the window."""
    _, user_id, session_id = plan_user("max")

    before = db.query(UsageRecord).filter(UsageRecord.user_id == user_id).count()
    reservation = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    # The router may have gone through several candidates; the request
    # finalises once, with the real total.
    finalize_usage(
        db, reservation, message_id=None, model="groq-llama4-scout",
        input_tokens=100, output_tokens=50, cached_tokens=0,
        cost_usd=0.01, tcu=1.0, duration_ms=10,
    )
    after = db.query(UsageRecord).filter(UsageRecord.user_id == user_id).count()
    assert after - before == 1, "a failover must not create a second usage row"


# ── TEAM: the shared pool ───────────────────────────────────────────────────


def _make_team(db, plan_user, members: int, team_id: str = "team-alpha"):
    out = []
    for _ in range(members):
        headers, user_id, session_id = plan_user("team")
        user = db.query(User).filter(User.id == user_id).one()
        user.team_id = team_id
        out.append((user_id, session_id))
    db.commit()
    return out


def test_team_members_share_one_budget(db, plan_user):
    """Usage by one member counts against the other's pool — that is what
    makes it a pool rather than two individual plans."""
    (a_id, a_sess), (b_id, b_sess) = _make_team(db, plan_user, 2)
    pool = get_plan_entitlement("team").team_weekly_pool_tcu_limit
    week_start, _ = _week_window(None)

    _seed_usage(db, a_id, pool, when=week_start + timedelta(minutes=5))

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=b_id, session_id=b_sess, plan="team")
    assert exc.value.detail["error"] == "team_pool_exceeded"
    assert "equipe" in exc.value.detail["message"]


def test_a_team_member_is_not_blocked_while_the_pool_has_room(db, plan_user):
    (a_id, a_sess), (b_id, b_sess) = _make_team(db, plan_user, 2)
    week_start, _ = _week_window(None)
    _seed_usage(db, a_id, 100.0, when=week_start + timedelta(minutes=5))

    assert check_and_reserve(db, user_id=b_id, session_id=b_sess, plan="team")


def test_one_member_cannot_drain_the_whole_pool(db, plan_user):
    """The per-member weekly limit is the protection inside the pool."""
    e = get_plan_entitlement("team")
    assert e.weekly_tcu_limit < e.team_weekly_pool_tcu_limit

    (a_id, a_sess), (_b_id, _b_sess) = _make_team(db, plan_user, 2)
    week_start, _ = _week_window(None)
    _seed_usage(db, a_id, e.weekly_tcu_limit, when=week_start + timedelta(minutes=5))

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=a_id, session_id=a_sess, plan="team")
    # Their own weekly limit stops them well before the pool is empty.
    assert exc.value.status_code == 429


def test_a_team_account_with_no_team_behaves_as_an_individual(db, plan_user):
    """A TEAM subscription with a single seat has team_id NULL. The pool is
    then just that person, which must not break anything."""
    _, user_id, session_id = plan_user("team")
    assert db.query(User).filter(User.id == user_id).one().team_id is None
    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="team")


def test_usage_from_another_team_does_not_count(db, plan_user):
    (a_id, _), = _make_team(db, plan_user, 1, team_id="team-alpha")
    (b_id, b_sess), = _make_team(db, plan_user, 1, team_id="team-beta")
    pool = get_plan_entitlement("team").team_weekly_pool_tcu_limit
    week_start, _ = _week_window(None)

    _seed_usage(db, a_id, pool, when=week_start + timedelta(minutes=5))

    assert check_and_reserve(db, user_id=b_id, session_id=b_sess, plan="team"), (
        "pools are per team; another team's spend must be invisible"
    )


def test_team_agent_loop_is_bound_by_the_pool(db, plan_user):
    (a_id, a_sess), (b_id, b_sess) = _make_team(db, plan_user, 2)
    pool = get_plan_entitlement("team").team_weekly_pool_tcu_limit
    week_start, _ = _week_window(None)

    reservation = check_and_reserve(db, user_id=b_id, session_id=b_sess, plan="team")
    _seed_usage(db, a_id, pool, when=week_start + timedelta(minutes=5))

    assert check_agent_iteration_budget(db, reservation, b_id, "team", 1.0) is False


# ── The windows the lower plans actually carry ──────────────────────────────
#
# This block used to assert that FREE/START/PRO were untouched by the
# mission that introduced session budgets for MAX and TEAM. The 2026-09-11
# economics pass repriced and re-windowed every plan, and gave a session
# budget to every PAID plan — so what is worth pinning now is not "these did
# not change" but "these are the numbers that were decided". The derivation
# lives in tests/unit/test_plan_economics.py.


@pytest.mark.parametrize("plan,daily,weekly,session", [
    # FREE weekly 50 -> 20 on 2026-09-13: sized to a person evaluating the
    # product, with a plan-wide daily budget bounding every account together.
    ("free", 10.0, 20.0, None),
    ("start", 45.0, 110.0, 30.0),
    ("pro", 110.0, 235.0, 60.0),
])
def test_lower_plan_windows_are_the_ones_that_were_decided(plan, daily, weekly, session):
    e = get_plan_entitlement(plan)
    assert e.daily_tcu_limit == daily
    assert e.weekly_tcu_limit == weekly
    assert e.session_tcu_limit == session, (
        "FREE has no session budget; every paid plan does, so a limit is "
        "always absorbed by a window that renews rather than a daily wall"
    )
    assert e.team_weekly_pool_tcu_limit is None


def test_lower_plans_still_reserve_normally(db, plan_user):
    for plan in ("free", "start", "pro"):
        _, user_id, session_id = plan_user(plan)
        assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan=plan)
