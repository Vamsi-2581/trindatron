"""Account security: email confirmation, password recovery and change, lockout,
the security log, and the Resend connector."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.config import settings
from src.connectors.email import EmailConnector, EmailConnectorError
from src.models.user import User
from src.services import email_service
from src.services.email_service import ResendEmailConnector

PASSWORD = "password123"


class RecordingConnector(EmailConnector):
    def __init__(self) -> None:
        self.sent: list[dict] = []

    @property
    def connector_name(self) -> str:
        return "recording"

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> str:
        self.sent.append({"to": to, "subject": subject, "body": body, "html": kwargs.get("html")})
        return str(len(self.sent))


@pytest.fixture
def outbox(monkeypatch):
    connector = RecordingConnector()
    email_service.set_connector(connector)
    monkeypatch.setattr(settings, "web_app_url", "https://trindatron.com")
    monkeypatch.setattr(settings, "auth_login_email_rate_limit", 1_000)
    monkeypatch.setattr(settings, "auth_login_ip_rate_limit", 1_000)
    yield connector.sent
    email_service.set_connector(None)


def _token(message: dict, path: str) -> str:
    match = re.search(rf"https://trindatron\.com{path}#token=([\w-]+)", message["body"])
    assert match, message["body"]
    return match.group(1)


def _register(client, email: str = "owner@test.com") -> dict:
    resp = client.post("/auth/register", json={"email": email, "password": PASSWORD, "client": "web"})
    assert resp.status_code == 201
    return resp.json()


def _login(client, email: str = "owner@test.com", password: str = PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def _me(client, token: str):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})


# ── Email confirmation ────────────────────────────────────────────────────────


def test_signup_sends_a_confirmation_link_that_confirms_once(client, outbox):
    session = _register(client)
    assert _me(client, session["access_token"]).json()["email_verified"] is False
    assert [m["to"] for m in outbox] == ["owner@test.com"]

    token = _token(outbox[0], "/auth/verify")
    assert client.post("/auth/email/verify", json={"token": token}).status_code == 200
    assert _me(client, session["access_token"]).json()["email_verified"] is True
    assert client.post("/auth/email/verify", json={"token": token}).status_code == 400


def test_resend_replaces_the_previous_confirmation_link(client, outbox):
    session = _register(client)
    first = _token(outbox[0], "/auth/verify")
    resp = client.post("/auth/email/resend", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert resp.status_code == 202
    second = _token(outbox[1], "/auth/verify")
    assert client.post("/auth/email/verify", json={"token": first}).status_code == 400
    assert client.post("/auth/email/verify", json={"token": second}).status_code == 200


def test_a_google_account_starts_confirmed(db):
    from src.services import google_auth_service as google

    user = google.sign_in(db, google.GoogleIdentity(subject="g-1", email="g@test.com", name="G"))
    assert user.email_verified is True


# ── Password recovery ─────────────────────────────────────────────────────────


def test_forgot_answers_the_same_for_an_unknown_address_and_sends_nothing(client, outbox):
    _register(client)
    outbox.clear()
    unknown = client.post("/auth/password/forgot", json={"email": "nobody@test.com"})
    known = client.post("/auth/password/forgot", json={"email": "OWNER@test.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert [m["to"] for m in outbox] == ["owner@test.com"]


def test_reset_sets_the_password_and_ends_every_session(client, db, outbox):
    session = _register(client)
    old_access = session["access_token"]
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    token = _token(outbox[-1], "/auth/reset")

    resp = client.post("/auth/password/reset", json={"token": token, "password": "brand-new-pass"})
    assert resp.status_code == 200
    assert _me(client, old_access).status_code == 401
    assert client.post("/auth/refresh", headers={"x-trindatron-client": "web"}).status_code == 401
    assert _login(client).status_code == 401
    fresh = _login(client, password="brand-new-pass")
    assert fresh.status_code == 200
    assert _me(client, fresh.json()["access_token"]).status_code == 200
    assert outbox[-1]["subject"] == "Sua senha do TRINDATRON foi alterada"

    # Opening the link proved the address, and the link works once.
    assert db.query(User).filter(User.email == "owner@test.com").one().email_verified_at is not None
    again = client.post("/auth/password/reset", json={"token": token, "password": "another-pass-1"})
    assert again.status_code == 400


def test_an_expired_or_replaced_reset_link_is_refused(client, outbox, monkeypatch):
    _register(client)
    monkeypatch.setattr(settings, "password_reset_minutes", 0)
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    expired = _token(outbox[-1], "/auth/reset")
    assert client.post("/auth/password/reset", json={"token": expired, "password": "brand-new-pass"}).status_code == 400

    monkeypatch.setattr(settings, "password_reset_minutes", 30)
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    older = _token(outbox[-1], "/auth/reset")
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    newer = _token(outbox[-1], "/auth/reset")
    assert client.post("/auth/password/reset", json={"token": older, "password": "brand-new-pass"}).status_code == 400
    assert client.post("/auth/password/reset", json={"token": newer, "password": "brand-new-pass"}).status_code == 200


def test_reset_enforces_the_password_rules(client, outbox):
    _register(client)
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    token = _token(outbox[-1], "/auth/reset")
    assert client.post("/auth/password/reset", json={"token": token, "password": "short"}).status_code == 422


def test_forgot_stops_sending_to_one_address_without_saying_so(client, outbox, monkeypatch):
    _register(client)
    outbox.clear()
    monkeypatch.setattr(settings, "auth_email_address_rate_limit", 2)
    answers = [client.post("/auth/password/forgot", json={"email": "owner@test.com"}) for _ in range(4)]
    assert {a.status_code for a in answers} == {202}
    assert len(outbox) == 2


def test_the_reset_link_keeps_the_token_out_of_the_url_sent_to_servers(client, outbox):
    _register(client)
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    assert "https://trindatron.com/auth/reset#token=" in outbox[-1]["body"]
    assert "?token=" not in outbox[-1]["body"]


# ── Password change ───────────────────────────────────────────────────────────


def test_change_password_needs_the_current_one_and_signs_out_everywhere(client, outbox):
    session = _register(client)
    auth = {"Authorization": f"Bearer {session['access_token']}"}
    wrong = client.post("/auth/password/change", headers=auth,
                        json={"current_password": "not-it-123", "new_password": "brand-new-pass"})
    assert wrong.status_code == 400

    ok = client.post("/auth/password/change", headers=auth,
                     json={"current_password": PASSWORD, "new_password": "brand-new-pass"})
    assert ok.status_code == 204
    assert _me(client, session["access_token"]).status_code == 401
    assert _login(client, password="brand-new-pass").status_code == 200
    assert outbox[-1]["subject"] == "Sua senha do TRINDATRON foi alterada"


# ── Lockout ───────────────────────────────────────────────────────────────────


def test_repeated_wrong_passwords_lock_password_sign_in_and_warn_the_owner(client, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    _register(client)
    outbox.clear()
    codes = [_login(client, password="wrong-pass-1").status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    assert [m["subject"] for m in outbox] == ["Tentativas de acesso à sua conta TRINDATRON"]

    # Even the right password is refused while locked: a guesser learns nothing.
    assert _login(client).status_code == 429

    # The owner is never locked out: a reset clears the lock.
    client.post("/auth/password/forgot", json={"email": "owner@test.com"})
    token = _token(outbox[-1], "/auth/reset")
    client.post("/auth/password/reset", json={"token": token, "password": "brand-new-pass"})
    assert _login(client, password="brand-new-pass").status_code == 200


def test_the_lock_ends_on_its_own(client, db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auth_lockout_threshold", 2)
    _register(client)
    for _ in range(2):
        _login(client, password="wrong-pass-1")
    user = db.query(User).filter(User.email == "owner@test.com").one()
    user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert _login(client).status_code == 200


def test_wrong_passwords_far_apart_do_not_add_up(client, db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auth_lockout_threshold", 2)
    _register(client)
    _login(client, password="wrong-pass-1")
    user = db.query(User).filter(User.email == "owner@test.com").one()
    user.last_failed_login_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    assert _login(client, password="wrong-pass-1").status_code == 401
    assert _login(client).status_code == 200


def test_an_unknown_address_is_never_reported_as_locked(client, outbox, monkeypatch):
    monkeypatch.setattr(settings, "auth_lockout_threshold", 2)
    codes = {_login(client, email="ghost@test.com", password="wrong-pass-1").status_code for _ in range(5)}
    assert codes == {401}


# ── Security log ──────────────────────────────────────────────────────────────


def test_the_security_log_shows_the_owner_their_own_events_only(client, outbox):
    session = _register(client)
    _login(client, password="wrong-pass-1")
    _login(client)
    other = _register(client, "other@test.com")

    mine = client.get("/auth/security-events", headers={"Authorization": f"Bearer {session['access_token']}"}).json()
    kinds = [event["kind"] for event in mine]
    assert kinds[0] == "login_success" and "login_failed" in kinds and "account_created" in kinds
    assert all(event["label"] for event in mine)
    theirs = client.get("/auth/security-events", headers={"Authorization": f"Bearer {other['access_token']}"}).json()
    assert [event["kind"] for event in theirs] == ["account_created"]
    assert "token" not in json.dumps(mine)


# ── Delivery ──────────────────────────────────────────────────────────────────


def test_resend_receives_the_message_with_the_key_as_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg_1"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            connector = ResendEmailConnector("re_test", "TRINDATRON <nao-responda@trindatron.com>", client=http)
            return await connector.send(to="a@test.com", subject="Oi", body="texto", html="<p>texto</p>")

    assert asyncio.run(run()) == "msg_1"
    assert seen["auth"] == "Bearer re_test"
    assert seen["body"] == {"from": "TRINDATRON <nao-responda@trindatron.com>", "to": ["a@test.com"],
                            "subject": "Oi", "text": "texto", "html": "<p>texto</p>"}


def test_a_refused_message_raises_for_the_connector_but_never_for_the_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "domain not verified"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            connector = ResendEmailConnector("re_test", "x@trindatron.com", client=http)
            with pytest.raises(EmailConnectorError):
                await connector.send(to="a@test.com", subject="Oi", body="texto")
            email_service.set_connector(connector)
            try:
                await email_service.deliver(email_service.email_verify_email("a@test.com", "https://x/y", 48))
            finally:
                email_service.set_connector(None)

    asyncio.run(run())


def test_without_a_key_nothing_is_sent_and_nothing_breaks(client, monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "")
    email_service.set_connector(None)
    assert email_service.configured() is False
    assert client.post("/auth/register", json={"email": "quiet@test.com", "password": PASSWORD}).status_code == 201
    assert client.post("/auth/password/forgot", json={"email": "quiet@test.com"}).status_code == 202
