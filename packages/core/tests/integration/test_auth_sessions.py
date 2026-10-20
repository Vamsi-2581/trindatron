"""A TRINDATRON login stays signed in until the user signs out.

Through the real routes: web keeps its refresh token in an httpOnly cookie,
desktop and mobile in the body; every refresh rotates; a replay ends the
login; logout and admin revocation end it; clients that have not adopted
refresh keep working unchanged.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.models.user import User
from src.services.auth_service import decode_token

HEADER = {"X-TRINDATRON-Client": "web"}


def _register(client, email="persist@test.com", client_type="web"):
    body = {"email": email, "password": "securepass123", "full_name": "P"}
    if client_type:
        body["client"] = client_type
    return client.post("/auth/register", json=body)


def _cookie(resp) -> str | None:
    for header in resp.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name == settings.refresh_cookie_name:
            return rest.split(";", 1)[0] or None
    return None


def _set_cookie_attributes(resp) -> str:
    return next(h for h in resp.headers.get_list("set-cookie") if h.startswith(settings.refresh_cookie_name)).lower()


def _refresh_with_cookie(client, cookie, headers=HEADER):
    client.cookies.clear()
    return client.post("/auth/refresh", headers=headers, cookies={settings.refresh_cookie_name: cookie})


def test_web_login_sets_an_httponly_cookie_and_never_returns_the_refresh_token(client):
    resp = _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["refresh_token"] is None
    assert body["expires_in"] == settings.access_token_minutes * 60
    assert decode_token(body["access_token"]) == body["user_id"]
    attributes = _set_cookie_attributes(resp)
    assert "httponly" in attributes and "path=/auth" in attributes and _cookie(resp)


def test_web_refresh_rotates_and_keeps_the_user_signed_in(client):
    first = _register(client)
    refreshed = _refresh_with_cookie(client, _cookie(first))
    assert refreshed.status_code == 200
    assert decode_token(refreshed.json()["access_token"]) == first.json()["user_id"]
    assert _cookie(refreshed) and _cookie(refreshed) != _cookie(first)


def test_a_cookie_refresh_without_the_client_header_is_refused(client):
    first = _register(client)
    resp = _refresh_with_cookie(client, _cookie(first), headers={})
    assert resp.status_code == 403


def test_desktop_and_mobile_receive_the_refresh_token_in_the_body(client):
    for kind in ("desktop", "mobile"):
        resp = _register(client, email=f"{kind}@test.com", client_type=kind)
        token = resp.json()["refresh_token"]
        assert token and _cookie(resp) is None
        refreshed = client.post("/auth/refresh", json={"refresh_token": token})
        assert refreshed.status_code == 200 and refreshed.json()["refresh_token"] != token


def test_login_works_the_same_way(client):
    _register(client, email="login@test.com", client_type=None)
    resp = client.post("/auth/login", json={"email": "login@test.com", "password": "securepass123", "client": "desktop"})
    assert resp.status_code == 200 and resp.json()["refresh_token"]


def test_a_legacy_client_keeps_its_long_lived_token(client):
    resp = _register(client, email="legacy@test.com", client_type=None)
    body = resp.json()
    assert body["refresh_token"] is None and body["expires_in"] is None and _cookie(resp) is None
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 200


def test_a_restarted_app_restores_the_session_from_the_refresh_token(client):
    token = _register(client, email="restart@test.com", client_type="desktop").json()["refresh_token"]
    with TestClient(app) as restarted:
        resp = restarted.post("/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 200
        me = restarted.get("/auth/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"})
        assert me.status_code == 200 and me.json()["email"] == "restart@test.com"


def test_simultaneous_refreshes_do_not_sign_the_user_out(client):
    token = _register(client, email="tabs@test.com", client_type="desktop").json()["refresh_token"]
    a = client.post("/auth/refresh", json={"refresh_token": token})
    b = client.post("/auth/refresh", json={"refresh_token": token})
    assert a.status_code == 200 and b.status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": a.json()["refresh_token"]}).status_code == 200


def test_a_replayed_token_ends_the_login(client, db, monkeypatch):
    monkeypatch.setattr(settings, "refresh_reuse_grace_seconds", 0)
    token = _register(client, email="replay@test.com", client_type="mobile").json()["refresh_token"]
    rotated = client.post("/auth/refresh", json={"refresh_token": token}).json()["refresh_token"]

    replay = client.post("/auth/refresh", json={"refresh_token": token})
    assert replay.status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": rotated}).status_code == 401


def test_logout_revokes_the_login_and_clears_the_cookie(client):
    first = _register(client, email="bye@test.com")
    cookie = _cookie(first)
    client.cookies.clear()
    out = client.post("/auth/logout", headers=HEADER, cookies={settings.refresh_cookie_name: cookie})
    assert out.status_code == 204
    assert "max-age=0" in _set_cookie_attributes(out) or "expires=" in _set_cookie_attributes(out)
    assert _refresh_with_cookie(client, cookie).status_code == 401


def test_a_refused_refresh_clears_the_cookie(client):
    resp = _refresh_with_cookie(client, "forged-token")
    assert resp.status_code == 401
    assert settings.refresh_cookie_name in "".join(resp.headers.get_list("set-cookie"))


def test_admin_revocation_signs_the_user_out_everywhere(client, db):
    admin = _register(client, email="root@test.com", client_type=None).json()
    db.query(User).filter(User.id == admin["user_id"]).one().is_admin = True
    db.commit()
    target = _register(client, email="target@test.com", client_type="desktop").json()

    resp = client.post(f"/admin/users/{target['user_id']}/revoke-sessions",
                       headers={"Authorization": f"Bearer {admin['access_token']}"})
    assert resp.status_code == 200 and resp.json()["revoked_tokens"] == 1
    assert client.post("/auth/refresh", json={"refresh_token": target["refresh_token"]}).status_code == 401


def test_an_expired_access_token_is_a_401_the_client_can_recover_from(client):
    """The access token's own 401 is recoverable: the session lives on in the refresh token."""
    body = _register(client, email="recover@test.com", client_type="desktop").json()
    assert client.get("/auth/me", headers={"Authorization": "Bearer expired.or.garbage"}).status_code == 401
    refreshed = client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}).status_code == 200
