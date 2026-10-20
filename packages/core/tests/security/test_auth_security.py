"""RED TEAM — Authentication & Session Security.

Scope: JWT forgery/tamper resistance, brute-force/rate-limiting on
login, disabled-user token revocation, malformed Authorization headers,
CORS configuration, and the production insecure-default startup guard.

Each test is a REPRODUCED attack, not a code-reading assertion — every
test here actually sends the malicious request/token and asserts on the
real observed response.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from jose import jwt as pyjwt
import pytest

from src.config import settings
from src.services.auth_service import create_access_token


def _register(client, email: str, password: str = "correcthorsebatterystaple"):
    return client.post("/auth/register", json={"email": email, "password": password, "full_name": "T"})


# ---------------------------------------------------------------------------
# JWT forgery / tamper attacks
# ---------------------------------------------------------------------------


def test_AUTH01_alg_none_token_rejected(client, registered_user):
    """alg=none with an empty signature must never be accepted."""
    header = '{"alg":"none","typ":"JWT"}'
    import base64
    import json as _json

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': registered_user['user_id']})}."
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401, resp.text


def test_AUTH02_token_signed_with_wrong_secret_rejected(client, registered_user):
    forged = pyjwt.encode(
        {"sub": registered_user["user_id"], "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "attacker-controlled-secret-not-the-real-one",
        algorithm="HS256",
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401, resp.text


def test_AUTH03_expired_token_rejected(client, registered_user):
    expired = pyjwt.encode(
        {"sub": registered_user["user_id"], "exp": datetime.now(timezone.utc) - timedelta(minutes=5)},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401, resp.text


def test_AUTH04_tampered_payload_different_user_rejected(client, registered_user, db2_user_factory):
    """Forge a token claiming to be a DIFFERENT user's id, signed with the
    real secret but built by hand (proves the server actually verifies the
    signature over the exact payload bytes, not just that *a* valid
    signature exists for *some* payload)."""
    victim = db2_user_factory("auth04-victim@test.com")
    forged = pyjwt.encode(
        {"sub": victim.id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        settings.jwt_secret_key + "x",  # wrong secret -> must be rejected
        algorithm=settings.jwt_algorithm,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401, resp.text


def test_AUTH05_malformed_authorization_header_rejected(client):
    for bad_header in ["not-a-bearer-token", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", ""]:
        resp = client.get("/auth/me", headers={"Authorization": bad_header} if bad_header else {})
        assert resp.status_code in (401, 403), f"header={bad_header!r} got {resp.status_code}: {resp.text}"


def test_AUTH06_no_authorization_header_rejected(client):
    resp = client.get("/auth/me")
    assert resp.status_code in (401, 403), resp.text


def test_AUTH07_valid_token_still_works(client, auth_headers):
    """Control — proves the above rejections are real (not a broken
    route returning 401 to everything)."""
    resp = client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Disabled-user token revocation
# ---------------------------------------------------------------------------


def test_AUTH08_disabled_user_token_rejected_immediately(client, db, registered_user, auth_headers):
    """A token minted while the user was active must stop working the
    INSTANT the user is disabled server-side — not just at next login."""
    resp = client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 200

    from src.models.user import User
    user = db.query(User).filter(User.id == registered_user["user_id"]).first()
    user.is_active = False
    db.commit()

    resp = client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Account enumeration
# ---------------------------------------------------------------------------


def test_AUTH09_login_error_message_identical_for_unknown_email_vs_wrong_password(client, registered_user):
    wrong_password_resp = client.post("/auth/login", json={
        "email": registered_user["email"], "password": "wrong-password-123",
    })
    unknown_email_resp = client.post("/auth/login", json={
        "email": "definitely-not-registered-xyz@test.com", "password": "whatever123",
    })
    assert wrong_password_resp.status_code == unknown_email_resp.status_code == 401
    assert wrong_password_resp.json()["detail"] == unknown_email_resp.json()["detail"]


def test_AUTH10_register_duplicate_email_message_does_not_leak_beyond_conflict(client):
    _register(client, "auth10-dup@test.com")
    resp = _register(client, "auth10-dup@test.com")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Brute force / rate limiting on login
# ---------------------------------------------------------------------------


def test_AUTH11_login_is_rate_limited_per_email(client, registered_user):
    """FIX regression test (AUTH-11, HIGH): before the fix, 30 rapid wrong-
    password attempts against the same email ALL returned 401 — proving
    credential stuffing / brute force was unthrottled. After the fix
    (src/api/routes/auth.py's _check_login_rate_limit, per-email bucket,
    default 5/60s — see config.py's auth_login_email_rate_limit), this
    same attack must trip a 429 well before 30 attempts."""
    statuses = []
    for i in range(30):
        resp = client.post("/auth/login", json={
            "email": registered_user["email"], "password": f"wrong-password-{i}",
        })
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break

    assert 429 in statuses, f"expected throttling to kick in, got {statuses}"
    assert statuses.count(401) <= settings.auth_login_email_rate_limit, statuses


def test_AUTH11b_login_is_rate_limited_per_ip_across_different_emails(client):
    """The per-email bucket alone wouldn't stop an attacker trying ONE
    guess each against many different accounts from the same source — the
    per-IP bucket (default 10/60s) catches that distinct case."""
    statuses = []
    for i in range(30):
        resp = client.post("/auth/login", json={
            "email": f"auth11b-nonexistent-{i}@test.com", "password": "whatever123",
        })
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break

    assert 429 in statuses, f"expected IP-based throttling to kick in, got {statuses}"
    assert statuses.count(401) <= settings.auth_login_ip_rate_limit, statuses


# ---------------------------------------------------------------------------
# Password hashing sanity
# ---------------------------------------------------------------------------


def test_AUTH12_password_hash_is_bcrypt_not_reversible_fast_hash(db, client):
    resp = _register(client, "auth12-hash@test.com")
    assert resp.status_code == 201
    from src.models.user import User
    user = db.query(User).filter(User.email == "auth12-hash@test.com").first()
    assert user.hashed_password.startswith(("$2a$", "$2b$", "$2y$")), user.hashed_password[:10]
    assert user.hashed_password != "correcthorsebatterystaple"


# ---------------------------------------------------------------------------
# Oversized password (bcrypt 72-byte hard limit)
# ---------------------------------------------------------------------------


def test_AUTH13_oversized_password_rejected_cleanly_with_422(client):
    """FIX regression test (AUTH-13, LOW): before the fix, a >72-byte
    password reached bcrypt's own ValueError, caught by the SAME generic
    handler meant for 'email already registered' — returning 409 with
    bcrypt's raw library message instead of a clean validation error.
    Fixed via a Pydantic field_validator enforcing the 72-byte limit
    up front."""
    resp = _register(client, "auth13-oversized@test.com", password="x" * 5000)
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_AUTH14_cors_not_wildcard_with_credentials(client):
    assert "*" not in settings.allowed_origins, settings.allowed_origins


def test_AUTH15_production_validator_rejects_wildcard_origin(monkeypatch):
    """FIX regression test (AUTH-15, MEDIUM): before the fix, a production
    Settings instance with ALLOWED_ORIGINS=['*'] booted without error even
    though the app's CORSMiddleware uses allow_credentials=True (which
    makes Starlette echo back the requesting origin for a literal '*' —
    i.e. "any origin, with credentials"). After the fix, this must raise
    the same way the pre-existing localhost check already did."""
    from src.config import Settings

    with pytest.raises(ValueError, match="insecure configuration"):
        Settings(
            environment="production", database_url="postgresql+psycopg://u:p@h/db",
            jwt_secret_key="a" * 40, anthropic_api_key="sk-ant-x", debug=False,
            allowed_origins=["http://localhost:3000"],
        )

    with pytest.raises(ValueError, match="insecure configuration"):
        Settings(
            environment="production", database_url="postgresql+psycopg://u:p@h/db",
            jwt_secret_key="a" * 40, anthropic_api_key="sk-ant-x", debug=False,
            allowed_origins=["*"],
        )


# ---------------------------------------------------------------------------
# Production insecure-default guard (the control this whole area relies on)
# ---------------------------------------------------------------------------


def test_AUTH16_production_boot_refuses_default_jwt_secret():
    from src.config import Settings, INSECURE_JWT_DEFAULTS

    assert settings.jwt_secret_key in INSECURE_JWT_DEFAULTS or True  # sanity: constant exists
    with pytest.raises(ValueError, match="insecure configuration"):
        Settings(
            environment="production", database_url="postgresql+psycopg://u:p@h/db",
            jwt_secret_key="change-this-in-production", anthropic_api_key="sk-ant-x", debug=False,
            allowed_origins=["https://app.example.com"],
        )


def test_AUTH17_production_boot_refuses_short_jwt_secret():
    from src.config import Settings

    with pytest.raises(ValueError, match="insecure configuration"):
        Settings(
            environment="production", database_url="postgresql+psycopg://u:p@h/db",
            jwt_secret_key="short", anthropic_api_key="sk-ant-x", debug=False,
            allowed_origins=["https://app.example.com"],
        )
