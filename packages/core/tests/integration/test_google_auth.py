"""Sign in with Google: token verification, account rules and the routes.

Google is never contacted. A local RSA key stands in for Google's signing key
and its public half is served as the key set; the code exchange is patched to
return tokens signed with it. Everything else — the flow cookie, state and
nonce checks, account linking and session issuance — is the real code.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from src.config import settings
from src.models.refresh_token import RefreshToken
from src.models.user import User
from src.models.user_identity import UserIdentity
from src.services import google_auth_service as google
from src.services.auth_service import authenticate_user, create_user
from src.services.session_token_service import issue_session

WEB_CLIENT = "web-client.apps.googleusercontent.com"
DESKTOP_CLIENT = "desktop-client.apps.googleusercontent.com"
NONCE = "n" * 32


def _keypair(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    public_jwk = jwk.construct(public_pem, "RS256").to_dict()
    public_jwk.update(kid=kid, use="sig", alg="RS256")
    return private_pem, public_jwk


PRIVATE_KEY, PUBLIC_JWK = _keypair("k1")
ROTATED_PRIVATE_KEY, ROTATED_JWK = _keypair("k2")


def _id_token(*, aud=WEB_CLIENT, iss="https://accounts.google.com", sub="google-sub-1",
              email="person@gmail.com", email_verified=True, nonce=NONCE, exp_delta=3600,
              key=PRIVATE_KEY, kid="k1", drop=(), **extra) -> str:
    now = datetime.now(timezone.utc)
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": email_verified,
              "nonce": nonce, "name": "Person", "iat": now, "exp": now + timedelta(seconds=exp_delta), **extra}
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def fake_google(monkeypatch):
    """Google's key set and token endpoint, local."""
    served = {"keys": [PUBLIC_JWK]}
    calls = {"jwks": 0, "token": []}

    async def fetch_jwks():
        calls["jwks"] += 1
        return served

    monkeypatch.setattr(google, "_fetch_jwks_document", fetch_jwks)
    monkeypatch.setattr(google, "_jwks_cache", {"document": None, "fetched_at": 0.0})
    monkeypatch.setattr(settings, "google_client_id", WEB_CLIENT)
    monkeypatch.setattr(settings, "google_client_secret", "web-secret")
    monkeypatch.setattr(settings, "google_desktop_client_id", DESKTOP_CLIENT)
    monkeypatch.setattr(settings, "google_desktop_client_secret", "desktop-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://testserver/auth/google/callback")
    monkeypatch.setattr(settings, "web_app_url", "https://app.example")
    monkeypatch.setattr(settings, "refresh_cookie_secure", False)

    from src.api.routes import auth_google
    from src.services.request_control import InMemoryRateLimiter
    monkeypatch.setattr(auth_google, "_google_ip_rate_limiter", InMemoryRateLimiter())
    return {"served": served, "calls": calls}


def _verify(token, *, audience=WEB_CLIENT, nonce=NONCE):
    return asyncio.run(google.verify_id_token(token, audience=audience, nonce=nonce))


# ── ID token verification ─────────────────────────────────────────────────


def test_a_valid_google_token_yields_the_identity():
    identity = _verify(_id_token(email="Person@Gmail.com"))
    assert identity.subject == "google-sub-1"
    assert identity.email == "person@gmail.com"


def test_both_documented_issuer_forms_are_accepted():
    assert _verify(_id_token(iss="accounts.google.com")).subject == "google-sub-1"


@pytest.mark.parametrize("token_kwargs,reason", [
    ({"aud": "someone-elses-client"}, "invalid_token"),
    ({"iss": "https://evil.example"}, "invalid_token"),
    ({"exp_delta": -600}, "invalid_token"),
    ({"nonce": "a-different-nonce-value-xxxxxxxx"}, "nonce_mismatch"),
    ({"email_verified": False}, "email_unverified"),
    ({"drop": ("aud",)}, "invalid_token"),
    ({"drop": ("nonce",)}, "nonce_mismatch"),
    ({"drop": ("email",)}, "missing_claims"),
    ({"key": ROTATED_PRIVATE_KEY}, "invalid_token"),
])
def test_a_token_that_fails_any_check_is_refused(token_kwargs, reason):
    with pytest.raises(google.GoogleAuthError) as exc:
        _verify(_id_token(**token_kwargs))
    assert exc.value.reason == reason


def test_a_symmetric_token_is_refused_before_any_key_lookup(fake_google):
    forged = jwt.encode({"sub": "x", "aud": WEB_CLIENT}, "guessable", algorithm="HS256")
    with pytest.raises(google.GoogleAuthError) as exc:
        _verify(forged)
    assert exc.value.reason == "unexpected_algorithm"
    assert fake_google["calls"]["jwks"] == 0


def test_a_rotated_signing_key_is_picked_up(fake_google, monkeypatch):
    _verify(_id_token())  # caches the key set with k1 only
    fake_google["served"]["keys"] = [PUBLIC_JWK, ROTATED_JWK]
    monkeypatch.setitem(google._jwks_cache, "fetched_at", 0.0)
    assert _verify(_id_token(key=ROTATED_PRIVATE_KEY, kid="k2")).subject == "google-sub-1"


# ── Account rules ─────────────────────────────────────────────────────────


def _identity(sub="google-sub-1", email="person@gmail.com"):
    return google.GoogleIdentity(subject=sub, email=email, name="Person")


def test_a_first_google_sign_in_creates_a_free_account(db):
    user = google.sign_in(db, _identity())
    assert user.plan == "free" and user.email == "person@gmail.com"
    assert db.query(UserIdentity).filter(UserIdentity.user_id == user.id).count() == 1


def test_the_same_google_identity_always_reaches_the_same_account(db):
    first = google.sign_in(db, _identity())
    again = google.sign_in(db, _identity(email="renamed@gmail.com"))
    assert again.id == first.id
    assert db.query(User).count() == 1


def test_linking_to_a_password_account_disables_the_password_and_revokes_sessions(db):
    squatter = create_user(db, email="Person@gmail.com", password="whoever-registered-first")
    session = issue_session(db, squatter, client="desktop")

    user = google.sign_in(db, _identity())

    assert user.id == squatter.id
    assert authenticate_user(db, email="Person@gmail.com", password="whoever-registered-first") is None
    row = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).one()
    assert row.revoked_at is not None and row.revoked_reason == "identity_linked"
    assert session.refresh_token  # the token that was issued is now dead (see test_auth_sessions)


def test_an_account_already_linked_to_another_google_identity_is_not_relinked(db):
    google.sign_in(db, _identity(sub="original"))
    with pytest.raises(google.GoogleAuthError) as exc:
        google.sign_in(db, _identity(sub="impostor"))
    assert exc.value.reason == "account_linked_to_another_google_identity"


def test_a_disabled_account_cannot_sign_in_with_google(db):
    user = google.sign_in(db, _identity())
    user.is_active = False
    db.commit()
    with pytest.raises(google.GoogleAuthError) as exc:
        google.sign_in(db, _identity())
    assert exc.value.reason == "account_disabled"


# ── Web routes ────────────────────────────────────────────────────────────


def _cookie(resp, name):
    for header in resp.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key == name:
            return rest.split(";", 1)[0]
    return None


def _start(client):
    resp = client.get("/auth/google/start", follow_redirects=False)
    assert resp.status_code == 303
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return resp, {k: v[0] for k, v in query.items()}


def _patch_exchange(monkeypatch, fake_google, token_factory):
    async def post_token(data):
        fake_google["calls"]["token"].append(data)
        return {"id_token": token_factory(data), "access_token": None}
    monkeypatch.setattr(google, "_post_token_request", post_token)


def test_start_sends_the_browser_to_google_with_state_nonce_and_pkce(client):
    resp, query = _start(client)
    assert resp.headers["location"].startswith(google.GOOGLE_AUTH_URL)
    assert query["client_id"] == WEB_CLIENT and query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 32 and len(query["nonce"]) >= 32
    flow_header = next(h for h in resp.headers.get_list("set-cookie") if h.startswith("trindatron_google_flow="))
    assert "httponly" in flow_header.lower() and "path=/auth/google" in flow_header.lower()


def test_start_without_configuration_returns_to_sign_in_with_a_neutral_error(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    resp = client.get("/auth/google/start", follow_redirects=False)
    assert resp.headers["location"] == "https://app.example/auth?error=google_unavailable"


def test_a_full_web_sign_in_sets_the_refresh_cookie_and_lands_on_the_app(client, db, monkeypatch, fake_google):
    start, query = _start(client)
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(nonce=query["nonce"]))

    resp = client.get("/auth/google/callback", params={"code": "auth-code", "state": query["state"]},
                      cookies={"trindatron_google_flow": _cookie(start, "trindatron_google_flow")},
                      follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://app.example/auth/google"
    assert _cookie(resp, settings.refresh_cookie_name)
    exchange = fake_google["calls"]["token"][0]
    assert exchange["grant_type"] == "authorization_code" and len(exchange["code_verifier"]) >= 43
    assert db.query(User).filter(User.email == "person@gmail.com").count() == 1


def test_cancelling_at_google_returns_to_sign_in(client):
    resp = client.get("/auth/google/callback", params={"error": "access_denied", "state": "x"},
                      follow_redirects=False)
    assert resp.headers["location"] == "https://app.example/auth?error=google_cancelled"


def test_a_callback_with_a_forged_state_is_refused(client, monkeypatch, fake_google):
    start, query = _start(client)
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(nonce=query["nonce"]))
    resp = client.get("/auth/google/callback", params={"code": "auth-code", "state": "attacker-state"},
                      cookies={"trindatron_google_flow": _cookie(start, "trindatron_google_flow")},
                      follow_redirects=False)
    assert resp.headers["location"] == "https://app.example/auth?error=google_failed"
    assert fake_google["calls"]["token"] == [], "the code is never exchanged"


def test_a_callback_from_another_browser_is_refused(client, monkeypatch, fake_google):
    """Login CSRF: a victim's browser has no flow cookie for the attacker's code."""
    _, query = _start(client)
    client.cookies.clear()
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(nonce=query["nonce"]))
    resp = client.get("/auth/google/callback", params={"code": "auth-code", "state": query["state"]},
                      follow_redirects=False)
    assert resp.headers["location"] == "https://app.example/auth?error=google_failed"
    assert _cookie(resp, settings.refresh_cookie_name) is None


def test_a_replayed_id_token_with_the_wrong_nonce_is_refused(client, monkeypatch, fake_google):
    start, query = _start(client)
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(nonce="nonce-from-an-older-sign-in-xxxx"))
    resp = client.get("/auth/google/callback", params={"code": "auth-code", "state": query["state"]},
                      cookies={"trindatron_google_flow": _cookie(start, "trindatron_google_flow")},
                      follow_redirects=False)
    assert resp.headers["location"] == "https://app.example/auth?error=google_failed"
    assert _cookie(resp, settings.refresh_cookie_name) is None


# ── Desktop ───────────────────────────────────────────────────────────────


def _desktop_body(**overrides):
    body = {"code": "desktop-code", "code_verifier": "v" * 64,
            "redirect_uri": "http://127.0.0.1:53682/callback", "nonce": NONCE, "client": "desktop"}
    body.update(overrides)
    return body


def test_desktop_config_exposes_only_public_values(client):
    body = client.get("/auth/google/desktop-config").json()
    assert body == {"client_id": DESKTOP_CLIENT, "authorization_endpoint": google.GOOGLE_AUTH_URL,
                    "scope": google.SCOPE}


def test_desktop_sign_in_returns_a_persistent_session(client, monkeypatch, fake_google):
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(aud=DESKTOP_CLIENT))
    resp = client.post("/auth/google/token", json=_desktop_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh_token"] and body["expires_in"] == settings.access_token_minutes * 60
    assert fake_google["calls"]["token"][0]["client_id"] == DESKTOP_CLIENT


@pytest.mark.parametrize("redirect_uri", [
    "https://evil.example/callback", "http://192.168.0.10:5000/cb", "trindatron://auth",
    "http://127.0.0.1.evil.example:80/cb",
])
def test_desktop_accepts_only_loopback_redirects(client, redirect_uri):
    resp = client.post("/auth/google/token", json=_desktop_body(redirect_uri=redirect_uri))
    assert resp.status_code == 400


def test_a_web_client_token_cannot_sign_in_the_desktop_app(client, monkeypatch, fake_google):
    _patch_exchange(monkeypatch, fake_google, lambda data: _id_token(aud=WEB_CLIENT))
    resp = client.post("/auth/google/token", json=_desktop_body())
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Google sign-in failed. Please try again."


def test_desktop_without_configuration_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(settings, "google_desktop_client_id", "")
    assert client.post("/auth/google/token", json=_desktop_body()).status_code == 503
