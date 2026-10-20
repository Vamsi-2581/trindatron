"""Persistent sessions: rotation, concurrency, replay, expiry and revocation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from starlette.responses import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.database import Base
from src.models.refresh_token import RefreshToken
from src.models.user import User
from src.services import session_token_service as st
from src.services.auth_service import decode_token

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
UA = "Mozilla/5.0 TRINDATRON test"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def user(db):
    u = User(email="session@test.com", hashed_password="x", full_name="S", plan="free")
    db.add(u)
    db.commit()
    return u


def test_a_new_session_has_a_short_access_token_and_a_long_lived_refresh_token(db, user):
    s = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    assert decode_token(s.access_token) == user.id
    assert s.access_expires_in == settings.access_token_minutes * 60
    assert s.refresh_expires_at == NOW + timedelta(days=settings.refresh_token_days)
    [row] = db.query(RefreshToken).all()
    assert row.token_hash != s.refresh_token, "only a hash is stored"


def test_refreshing_rotates_the_token(db, user):
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    second = st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(minutes=31))
    assert second.refresh_token != first.refresh_token
    assert decode_token(second.access_token) == user.id
    third = st.rotate_session(db, second.refresh_token, user_agent=UA, now=NOW + timedelta(hours=2))
    assert third.refresh_token not in (first.refresh_token, second.refresh_token)


def test_each_refresh_restarts_the_inactivity_window(db, user):
    first = st.issue_session(db, user, client="desktop", user_agent=UA, now=NOW)
    later = NOW + timedelta(days=settings.refresh_token_days - 1)
    second = st.rotate_session(db, first.refresh_token, user_agent=UA, now=later)
    assert second.refresh_expires_at == later + timedelta(days=settings.refresh_token_days)


def test_two_refreshes_at_the_same_moment_both_succeed(db, user):
    """Two tabs, or a retry after a dropped response, must not sign anyone out."""
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    a = st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=1))
    b = st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=3))
    assert st.rotate_session(db, a.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=5))
    assert st.rotate_session(db, b.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=6))


def test_a_replayed_token_revokes_the_whole_login(db, user):
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    legit = st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(minutes=1))

    with pytest.raises(st.InvalidRefreshToken) as exc:
        st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(hours=1))
    assert exc.value.reason == "reuse_detected"

    with pytest.raises(st.InvalidRefreshToken):
        st.rotate_session(db, legit.refresh_token, user_agent=UA, now=NOW + timedelta(hours=1, seconds=1))


def test_the_concurrency_window_does_not_cover_another_device(db, user):
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=1))
    with pytest.raises(st.InvalidRefreshToken):
        st.rotate_session(db, first.refresh_token, user_agent="curl/8.0", now=NOW + timedelta(seconds=2))


def test_an_expired_refresh_token_is_refused(db, user):
    first = st.issue_session(db, user, client="mobile", user_agent=UA, now=NOW)
    with pytest.raises(st.InvalidRefreshToken) as exc:
        st.rotate_session(db, first.refresh_token, user_agent=UA,
                          now=NOW + timedelta(days=settings.refresh_token_days, seconds=1))
    assert exc.value.reason == "expired"


def test_logout_ends_the_login(db, user):
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    assert st.revoke_session(db, first.refresh_token, now=NOW)
    with pytest.raises(st.InvalidRefreshToken) as exc:
        st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=1))
    assert exc.value.reason == "revoked"


def test_revoking_all_sessions_signs_out_every_device(db, user):
    web = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    desktop = st.issue_session(db, user, client="desktop", user_agent=UA, now=NOW)
    assert st.revoke_all_sessions(db, user.id, now=NOW) == 2
    for token in (web.refresh_token, desktop.refresh_token):
        with pytest.raises(st.InvalidRefreshToken):
            st.rotate_session(db, token, user_agent=UA, now=NOW + timedelta(seconds=1))


def test_a_deactivated_account_cannot_refresh(db, user):
    first = st.issue_session(db, user, client="web", user_agent=UA, now=NOW)
    user.is_active = False
    db.commit()
    with pytest.raises(st.InvalidRefreshToken) as exc:
        st.rotate_session(db, first.refresh_token, user_agent=UA, now=NOW + timedelta(seconds=1))
    assert exc.value.reason == "user_inactive"


@pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
def test_a_missing_or_unknown_token_is_refused(db, user, token):
    with pytest.raises(st.InvalidRefreshToken):
        st.rotate_session(db, token, user_agent=UA, now=NOW)


@pytest.mark.parametrize("tzinfo", [timezone.utc, ZoneInfo("UTC"), timezone(timedelta(hours=-3)), None])
def test_the_refresh_expiry_can_be_written_into_a_cookie_whatever_timezone_the_database_returns(tzinfo):
    # Production login answered 500: Postgres handed back ZoneInfo("UTC"), which Starlette's cookie
    # formatting rejects — it accepts only datetime.timezone.utc ("usegmt option requires a UTC datetime").
    value = datetime(2026, 10, 26, 23, 0, tzinfo=tzinfo)
    aware = st._aware(value)
    assert aware.tzinfo is timezone.utc
    assert aware == (value if tzinfo is not None else value.replace(tzinfo=timezone.utc))
    Response().set_cookie("refresh", "x", expires=aware)
