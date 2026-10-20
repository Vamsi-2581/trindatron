"""Sign in with Google from the native apps: one account base, same token rules.

Reuses the local stand-in for Google's signing key from test_google_auth.py —
Google is never contacted.
"""
from __future__ import annotations

import pytest

from src.models.refresh_token import RefreshToken
from src.models.user import User
from src.services import google_mobile_settings as mobile_settings
from src.services.google_mobile_settings import GoogleMobileSettings
from tests.integration.test_google_auth import NONCE, WEB_CLIENT, _id_token, fake_google  # noqa: F401 — fixture

ANDROID_CLIENT = "android-client.apps.googleusercontent.com"
IOS_CLIENT = "ios-client.apps.googleusercontent.com"


@pytest.fixture(autouse=True)
def mobile_clients(monkeypatch):
    configured = GoogleMobileSettings(_env_file=None, google_android_client_id=ANDROID_CLIENT,
                                      google_ios_client_id=IOS_CLIENT)
    monkeypatch.setattr(mobile_settings, "google_mobile_settings", lambda: configured)
    return configured


def _sign_in(client, token, nonce=NONCE):
    return client.post("/auth/google/id-token", json={"id_token": token, "nonce": nonce, "client": "mobile"})


def test_mobile_config_exposes_only_public_client_ids(client):
    assert client.get("/auth/google/mobile-config").json() == {
        "android_client_id": ANDROID_CLIENT, "ios_client_id": IOS_CLIENT, "web_client_id": None, "scope": "openid email profile",
    }


def test_an_android_token_signs_in_with_a_persistent_mobile_session(client, db):
    response = _sign_in(client, _id_token(aud=ANDROID_CLIENT))
    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"] and body["expires_in"] > 0
    user = db.query(User).filter(User.email == "person@gmail.com").one()
    assert user.plan == "free"
    assert db.query(RefreshToken).filter(RefreshToken.user_id == user.id).one().client == "mobile"
    # The same Google identity from iOS reaches the same account.
    assert _sign_in(client, _id_token(aud=IOS_CLIENT)).json()["user_id"] == user.id


@pytest.mark.parametrize("token_kwargs", [
    {"aud": WEB_CLIENT},                                  # issued to another app
    {"aud": ANDROID_CLIENT, "nonce": "a-different-nonce-value-xxxxxxxx"},
    {"aud": ANDROID_CLIENT, "email_verified": False},
    {"aud": ANDROID_CLIENT, "iss": "https://evil.example"},
    {"aud": ANDROID_CLIENT, "exp_delta": -3600},
])
def test_a_token_that_fails_any_check_is_refused_with_a_neutral_message(client, token_kwargs):
    response = _sign_in(client, _id_token(**token_kwargs))
    assert response.status_code == 401
    assert response.json()["detail"] == "Google sign-in failed. Please try again."


def test_a_malformed_token_is_refused(client):
    assert _sign_in(client, "not-a-jwt-but-long-enough-to-validate").status_code == 401


def test_without_mobile_client_ids_native_google_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(mobile_settings, "google_mobile_settings", lambda: GoogleMobileSettings(_env_file=None))
    assert client.get("/auth/google/mobile-config").status_code == 503
    assert _sign_in(client, _id_token(aud=ANDROID_CLIENT)).status_code == 503
