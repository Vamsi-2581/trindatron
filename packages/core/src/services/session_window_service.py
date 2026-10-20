"""
5-hour rolling entitlement session window.

Product rule (verbatim from spec)
----------------------------------
- The session does NOT start at login and does NOT start when the app
  opens. It starts at the moment of the FIRST operation that actually
  reserves TCU after no session is currently active.
- It lasts EXACTLY 5 hours from that timestamp — an absolute clock, never
  extended by later activity ("no renewal per request", "no idle-based
  expiry"). A request at 18:00 inside a session that started at 14:37:22
  does NOT push session_expires_at to 23:00.
- The next TCU-consuming operation after expiry (now >= expires_at)
  automatically starts a new 5-hour session.
- A failed/cancelled reservation must not prolong or restart the session —
  this module is only ever called from check_and_reserve's pre-reservation
  path, never from cancel_reservation or finalize_usage. Those two never
  touch EntitlementSession at all.
- The user's timezone does NOT affect the 5-hour duration — this window is
  timezone-agnostic, unlike the weekly (Sunday 12:00 local) rule.
- Separate dimension from daily/weekly TCU entitlement and from credits/
  billing (src/billing/) — EntitlementSession has no FK to UsageRecord or
  any financial table, and carries no TCU limit of its own in this phase.

Concurrency
-----------
resolve_or_start_session() must be called from within the SAME transaction
check_and_reserve already holds the PostgreSQL advisory lock for (keyed
tcu_{user_id}) — that lock is what serializes two concurrent "no active
session" requests on PostgreSQL, so only one of them actually inserts.
The UniqueConstraint on EntitlementSession.user_id + the IntegrityError
fallback below is the safety net for dialects without that lock (SQLite in
tests, and defense-in-depth in production): if two inserts still race, the
loser detects the conflict and reads back the winner's row instead of
creating a second session.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.models.entitlement_session import EntitlementSession

SESSION_DURATION = timedelta(hours=5)


def resolve_or_start_session(db: DBSession, user_id: str, now: datetime) -> EntitlementSession:
    """Return the user's active EntitlementSession, starting a new one if
    none exists or the existing one has expired (now >= expires_at).

    now must be the same timezone-aware UTC instant check_and_reserve is
    reasoning about for this request — passed in, never re-derived here,
    so the whole reservation flow is internally consistent about "now".

    Does NOT commit — the caller (check_and_reserve) owns the transaction
    boundary and commits once, alongside the UsageRecord reservation.
    """
    existing = db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).first()

    if existing is not None and now < existing.session_expires_at:
        return existing

    if existing is not None:
        # Expired — a NEW session starts now. This is a genuine new start,
        # not a renewal: session_started_at moves to `now`.
        existing.session_started_at = now
        existing.session_expires_at = now + SESSION_DURATION
        db.flush()
        return existing

    session = EntitlementSession(
        user_id=user_id,
        session_started_at=now,
        session_expires_at=now + SESSION_DURATION,
    )
    db.add(session)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a concurrent request that inserted first — only
        # reachable on dialects without the advisory lock (SQLite).
        db.rollback()
        winner = db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).first()
        if winner is not None:
            return winner
        raise  # genuinely unexpected — re-raise rather than mask it
    return session


def get_session_status(db: DBSession, user_id: str, now: datetime) -> dict:
    """Read-only status for display — NEVER creates or mutates a session.

    Used by GET /users/me/session. If the user has no session yet (never
    performed a TCU-consuming operation), active is False and the
    timestamps are None — this is expected and not an error state.
    """
    existing = db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).first()
    if existing is None:
        return {"active": False, "session_started_at": None, "session_expires_at": None}

    active = now < existing.session_expires_at
    return {
        "active": active,
        "session_started_at": existing.session_started_at.isoformat(),
        "session_expires_at": existing.session_expires_at.isoformat(),
    }
