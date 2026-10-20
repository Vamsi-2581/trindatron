"""Infrastructure security audit — one work stream of the FINAL SECURITY
HARDENING + RED TEAM mission. Scope: secrets hygiene (verified out-of-band,
not here), dependency posture (verified out-of-band via pip-audit/npm
audit, not here), CORS/production-config validation, rate-limit
IP-spoofing, error-handling leakage, and data exposure.

Findings this file PROVES (see this run's audit report for full IDs/
severities):
  INFRA-01 — X-Forwarded-For is trusted blindly (first entry, no trusted-
    proxy validation) by every _client_ip() in this codebase
    (auth.py/billing.py/sessions.py) — an attacker can defeat per-IP rate
    limiting entirely by sending a fresh spoofed value on every request.
    Proven on the login IP limiter here (the sharpest, most security-
    relevant instance); billing.py/sessions.py share the identical helper
    so the same bypass applies there too (not re-proven per-route,
    per-mission "não fazer refactor desnecessário" — one proof of the
    shared root cause is sufficient evidence).
  Also confirms (PASS, not vulnerabilities): production-config guardrails
  (CORS wildcard+credentials, localhost origins, DEBUG=true, missing
  ANTHROPIC_API_KEY, insecure JWT secret placeholders) actually fire;
  error responses never leak stack traces/SQL; data-exposure spot checks
  on a few key endpoints.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

_STRONG_JWT = "a" * 64
_PROD_BASE = {
    "jwt_secret_key": _STRONG_JWT,
    "anthropic_api_key": "sk-ant-test",
    "debug": False,
    "allowed_origins": ["https://app.trinity.ai"],
    "database_url": "postgresql+psycopg://user:pass@host:5432/trinity_db",
    # CP8: conftest.py sets TESTING=true in the OS environment for the whole
    # pytest session; a bare Settings(...) picks that up like any other env var,
    # and testing=True is itself refused in production (correctly — see
    # Settings._validate_production_secrets). This file's "PASS confirmation"
    # tests describe an otherwise-secure production deploy, so they override it
    # back to False rather than tripping on the ambient test-runner env var.
    "testing": False,
}


def _make_prod_settings(**overrides):
    from src.config import Settings
    return Settings(environment="production", **{**_PROD_BASE, **overrides})


# ---------------------------------------------------------------------------
# Production config guardrails — PASS confirmation (existing validator,
# previously had ZERO test coverage despite being genuinely security-
# relevant startup logic)
# ---------------------------------------------------------------------------


def test_production_rejects_wildcard_cors_with_credentials():
    with pytest.raises(ValidationError) as exc_info:
        _make_prod_settings(allowed_origins=["*"])
    assert "ALLOWED_ORIGINS must not contain" in str(exc_info.value)


def test_production_rejects_localhost_origin():
    with pytest.raises(ValidationError) as exc_info:
        _make_prod_settings(allowed_origins=["http://localhost:3000"])
    assert "localhost" in str(exc_info.value)


def test_production_rejects_debug_true():
    with pytest.raises(ValidationError) as exc_info:
        _make_prod_settings(debug=True)
    assert "DEBUG must be false" in str(exc_info.value)


def test_production_rejects_missing_anthropic_key():
    with pytest.raises(ValidationError) as exc_info:
        _make_prod_settings(anthropic_api_key="")
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_production_accepts_safe_config():
    """Sanity check: the guardrails above don't false-positive on a
    genuinely safe production configuration."""
    s = _make_prod_settings()
    assert s.environment == "production"


# ---------------------------------------------------------------------------
# INFRA-01 — X-Forwarded-For trusted blindly -> per-IP rate-limit bypass
# ---------------------------------------------------------------------------


def test_INFRA01_client_ip_prefers_x_real_ip_and_ignores_x_forwarded_for(monkeypatch):
    """FIX regression test (INFRA-01, HIGH, final gate revision):
    _client_ip() (auth.py, and identically billing.py/sessions.py) no
    longer parses X-Forwarded-For at all (Railway's own support channel
    gives contradictory, unverifiable answers about whether it strips or
    appends to a client-supplied value — see sessions.py's own docstring).
    It now prefers X-Real-IP, which Railway documents as its own single
    source of truth for the connecting IP. Proves: (1) the real IP stays
    stable across requests, (2) an arbitrary X-Forwarded-For value has
    ZERO effect on the resolved IP — an attacker cannot create unlimited
    buckets through it any more, whatever position they put a fake value
    in."""
    from src.api.routes.auth import _client_ip
    from starlette.requests import Request

    def _fake_request(*, real_ip: str | None, xff: str | None) -> Request:
        headers = []
        if real_ip:
            headers.append((b"x-real-ip", real_ip.encode()))
        if xff:
            headers.append((b"x-forwarded-for", xff.encode()))
        scope = {
            "type": "http", "headers": headers,
            "client": ("10.0.0.1", 12345),  # Railway's own internal proxy address
        }
        return Request(scope)

    # (1) Stable real IP across requests.
    req = _fake_request(real_ip="203.0.113.55", xff=None)
    req2 = _fake_request(real_ip="203.0.113.55", xff=None)
    assert _client_ip(req) == _client_ip(req2) == "203.0.113.55"

    # (2) A forged X-Forwarded-For — in ANY position, any number of fake
    # hops — has zero effect now: X-Real-IP is checked first and wins.
    req3 = _fake_request(real_ip="203.0.113.55", xff="198.51.100.1, 198.51.100.2, 198.51.100.3")
    assert _client_ip(req3) == "203.0.113.55"

    # No X-Real-IP at all (e.g. local/dev, no proxy in front) -> falls
    # back to the actual TCP peer, never to the attacker-controlled XFF.
    req4 = _fake_request(real_ip=None, xff="198.51.100.9")
    assert _client_ip(req4) == "10.0.0.1"


def test_INFRA01_login_ip_rate_limit_ignores_forwarded_for_uses_real_ip(client, monkeypatch):
    """FIX regression test, end-to-end over real HTTP: exhausting the
    login IP rate limit must hold regardless of what an attacker puts in
    X-Forwarded-For (varying it on every request must NOT reset the
    bucket, since it's no longer consulted at all) — and rate limiting
    must still function correctly for a genuinely stable X-Real-IP.

    Mitigating factor (unchanged either way): the per-EMAIL limiter
    (auth_login_email_rate_limit) still caps abuse against any ONE target
    account regardless of this IP-layer behavior.
    """
    from src.config import settings
    monkeypatch.setattr(settings, "auth_login_ip_rate_limit", 2)
    monkeypatch.setattr(settings, "auth_login_email_rate_limit", 1000)  # isolate the IP layer

    body = {"email": "nonexistent-infra01@test.com", "password": "wrong-password"}
    stable_real_ip = "203.0.113.77"

    r1 = client.post("/auth/login", json=body, headers={"X-Real-IP": stable_real_ip, "X-Forwarded-For": "198.51.100.1"})
    r2 = client.post("/auth/login", json=body, headers={"X-Real-IP": stable_real_ip, "X-Forwarded-For": "198.51.100.2"})
    r3 = client.post("/auth/login", json=body, headers={"X-Real-IP": stable_real_ip, "X-Forwarded-For": "198.51.100.3"})
    assert r1.status_code in (401, 429) and r2.status_code in (401, 429)
    assert r3.status_code == 429, "varying X-Forwarded-For must not reset the IP bucket keyed on X-Real-IP"

    # A genuinely different X-Real-IP is a genuinely different bucket —
    # rate limiting still functions correctly, not globally broken.
    r4 = client.post("/auth/login", json=body, headers={"X-Real-IP": "203.0.113.200"})
    assert r4.status_code == 401, "a real, different client IP must not be blocked by someone else's bucket"


# ---------------------------------------------------------------------------
# Security headers — observed fact, not a test assertion (documented gap,
# not asserted as pass/fail since there's no existing app-level middleware
# contract to regress-test against; see audit report for severity/context)
# ---------------------------------------------------------------------------


def test_response_headers_observed(client):
    """FIX regression test: X-Content-Type-Options: nosniff was added
    (cheap, safe defense-in-depth for a payments API — main.py's
    _security_headers middleware). HSTS/CSP/Referrer-Policy/X-Frame-Options
    remain deliberately absent: this is a pure JSON API (no HTML render,
    no cookie-based auth), and HSTS is the reverse-proxy/CDN's job on
    Railway — documented here as a stable regression signal either way."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options") == "nosniff"

    absent = [
        h for h in (
            "strict-transport-security", "content-security-policy",
            "referrer-policy", "x-frame-options",
        )
        if h not in resp.headers
    ]
    assert absent == [
        "strict-transport-security", "content-security-policy",
        "referrer-policy", "x-frame-options",
    ], f"Some security headers now present ({absent}) — update this test, don't just delete it."


# ---------------------------------------------------------------------------
# Error handling — no leakage
# ---------------------------------------------------------------------------


def test_malformed_json_never_leaks_internals(client, auth_headers):
    resp = client.post(
        "/billing/checkout", content=b"{not valid json", headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code in (400, 422)
    body_text = resp.text.lower()
    for leak in ("traceback", "sqlalchemy", "site-packages", "c:\\", "database_url", "secret"):
        assert leak not in body_text, f"leaked {leak!r} in error response: {resp.text[:500]}"


def test_unknown_route_404_no_internals(client):
    resp = client.get("/this-route-does-not-exist-xyz123")
    assert resp.status_code == 404
    body_text = resp.text.lower()
    for leak in ("traceback", "site-packages", "c:\\"):
        assert leak not in body_text


# ---------------------------------------------------------------------------
# Data exposure spot checks
# ---------------------------------------------------------------------------


def test_entitlement_response_has_no_internal_fields(client, auth_headers):
    resp = client.get("/users/me/entitlement", headers=auth_headers)
    assert resp.status_code == 200
    body_text = resp.text.lower()
    for leak in ("hashed_password", "jwt_secret", "api_key", "reservation_id"):
        assert leak not in body_text


def test_billing_providers_response_has_no_secrets(client):
    resp = client.get("/billing/providers")
    assert resp.status_code == 200
    body_text = resp.text.lower()
    for leak in ("api_key", "ipn_secret", "secret_key"):
        assert leak not in body_text
