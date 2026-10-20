"""Security review of the presentation capability.

The capability adds three things an attacker cares about: a service this
process calls over private networking, a proxy to a paid model API, and files
served to browsers. Each gets its own section below.

The SSRF case is the one that was actually found during implementation rather
than anticipated: the engine's generate response hands back a `path` that core
then fetches, and resolving it with urljoin lets an absolute URL replace the
host entirely. Core reaches the engine from inside the private network, which
makes this process a far better-placed attacker than the internet is.
"""
from __future__ import annotations

import pytest

from src.config import settings
from src.presentation.presenton import PresentonProvider
from src.presentation.provider import PresentationError


def _provider(base="http://engine.internal:5001"):
    return PresentonProvider(base_url=base, username="u", password="p")


# ── SSRF: an engine-supplied path must not choose the host ───────────────

@pytest.mark.parametrize("hostile", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://evil.example.com/exfiltrate",
    "http://127.0.0.1:5432/",
    "http://engine.internal.evil.com/x",
])
def test_an_artifact_path_cannot_redirect_the_fetch_to_another_host(hostile):
    """A path is data returned by a service, not an instruction about where to
    connect. Anything resolving off-origin is refused rather than fetched."""
    with pytest.raises(PresentationError):
        _provider()._same_origin_url(hostile)


@pytest.mark.parametrize("ok", [
    "/static/deck-1.pptx",
    "static/deck-1.pptx",
    "/api/v1/ppt/files/deck%20one.pptx",
])
def test_ordinary_relative_paths_still_resolve(ok):
    resolved = _provider()._same_origin_url(ok)
    assert resolved.startswith("http://engine.internal:5001/")


def test_a_protocol_relative_path_is_neutralised_rather_than_followed():
    """`//evil.example.com/x` is the classic protocol-relative SSRF payload:
    urljoin would resolve it to a completely different host.

    Two independent defences stop it, and this pins both. The leading slashes
    are stripped before resolution, which demotes it to an ordinary path
    segment on the engine's own host — a harmless 404 rather than a request
    to an attacker's server. And if that strip were ever removed, the
    same-origin check below is what would catch it.
    """
    resolved = _provider()._same_origin_url("//evil.example.com/x")
    assert resolved == "http://engine.internal:5001/evil.example.com/x"


def test_the_origin_check_alone_catches_a_protocol_relative_url():
    """The second layer, exercised directly: whatever the strip does or stops
    doing, an off-origin resolution is refused."""
    from urllib.parse import urljoin

    # What urljoin would produce if the strip were gone.
    would_be = urljoin("http://engine.internal:5001/", "//evil.example.com/x")
    assert would_be == "http://evil.example.com/x"
    with pytest.raises(PresentationError):
        _provider()._same_origin_url("http://evil.example.com/x")


def test_a_path_on_the_same_host_but_a_different_port_is_refused():
    """Same host is not the same service — another port on that host is a
    different process, and on a private network usually a more interesting
    one."""
    with pytest.raises(PresentationError):
        _provider()._same_origin_url("http://engine.internal:5432/")


# ── The gateway is not a public surface ──────────────────────────────────

def test_the_gateway_is_absent_from_the_public_schema(client):
    """An internal proxy to a paid model API must not be discoverable."""
    schema = client.get("/openapi.json").json()
    assert not [p for p in schema.get("paths", {}) if "internal" in p]


def test_the_gateway_refuses_a_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "correct-horse")
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_a_configured_token_with_an_accent_refuses_instead_of_crashing(client, monkeypatch):
    """Seen in production: a hand-typed token with a "ç" made the comparison raise, a 500 instead of a 401."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "correct-horse-coração")
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Bearer correct-horse-coracao"},
    )
    assert r.status_code == 401


def test_the_gateway_refuses_a_non_bearer_scheme(client, monkeypatch):
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "correct-horse")
    r = client.post(
        "/internal/presenton/llm/v1/chat/completions",
        json={"messages": []}, headers={"Authorization": "Basic correct-horse"},
    )
    assert r.status_code == 401


# ── Secrets never travel outward ─────────────────────────────────────────

def test_the_engine_environment_never_carries_the_gemini_key():
    """The engine is a third-party container. It gets a token we can rotate,
    never Google's credential."""
    from src.presentation.presenton import engine_environment
    from src.presentation.provider import ImageStrategy

    env = engine_environment(
        strategy=ImageStrategy.STOCK_FIRST,
        llm_base_url="http://core:8000/internal/presenton/llm/v1",
        llm_gateway_token="rotatable-token",
        model="models/gemini-3.8-flash",
    )
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    assert "GOOGLE_API_KEY" not in joined
    assert "GEMINI_API_KEY" not in joined


def test_a_signed_url_never_contains_the_secret_access_key():
    import asyncio

    from src.storage.r2 import R2ObjectStore

    secret = "s3cr3t-r2-key-material"
    store = R2ObjectStore(
        endpoint="https://acct.r2.cloudflarestorage.com", bucket="b",
        access_key_id="AKID", secret_access_key=secret,
    )
    url = asyncio.run(store.signed_url(key="presentations/u/j/deck.pptx", expires_in=60))
    assert secret not in url


def test_a_signed_url_always_expires():
    """There is no code path that produces a non-expiring URL — the expiry is
    a mandatory argument and part of the signature."""
    import asyncio
    import inspect

    from src.storage.r2 import R2ObjectStore

    sig = inspect.signature(R2ObjectStore.signed_url)
    assert sig.parameters["expires_in"].default is inspect.Parameter.empty

    store = R2ObjectStore(
        endpoint="https://a.r2.cloudflarestorage.com", bucket="b",
        access_key_id="AKID", secret_access_key="k",
    )
    url = asyncio.run(store.signed_url(key="k.pptx", expires_in=60))
    assert "X-Amz-Expires=60" in url


# ── Storage keys are not authorization ───────────────────────────────────

def test_local_store_cannot_be_walked_out_of_its_root(tmp_path):
    import asyncio

    from src.storage.local import LocalObjectStore
    from src.storage.object_store import ObjectStoreError

    store = LocalObjectStore(root=str(tmp_path / "objects"))
    for hostile in ("../escape.txt", "../../escape.txt", "a/../../escape.txt"):
        with pytest.raises(ObjectStoreError):
            asyncio.run(store.put(key=hostile, data=b"x", content_type="text/plain"))


# ── Download authorization ───────────────────────────────────────────────

def test_downloading_an_unknown_artifact_is_404_not_403(client, auth_headers):
    """403 would confirm the artifact exists. 404 for both cases leaks
    nothing — the same rule the job routes already follow."""
    r = client.get(
        "/execution/artifacts/00000000-0000-0000-0000-000000000000/download",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_downloading_requires_authentication(client):
    r = client.get("/execution/artifacts/whatever/download")
    assert r.status_code in (401, 403)


def test_creating_a_presentation_requires_authentication(client):
    r = client.post("/presentations", json={"brief": "a deck about anything at all"})
    assert r.status_code in (401, 403)
