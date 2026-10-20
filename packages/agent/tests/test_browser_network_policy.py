"""Unit tests for trinity_agent.browser.network_policy — no real browser needed.

DNS resolution is monkeypatched (via socket.getaddrinfo) for determinism —
these tests must not depend on real network/DNS availability in CI.
"""
from __future__ import annotations

import socket

import pytest

from trinity_agent.browser.network_policy import (
    BlockedURLError,
    resolve_and_validate_host,
    validate_request_url,
    validate_url_scheme,
)


def _fake_getaddrinfo(ip: str):
    """Build a socket.getaddrinfo()-shaped return value for a single IPv4/IPv6 address."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 80, 0, 0) if family == socket.AF_INET6 else (ip, 80)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


# ── validate_url_scheme — synchronous, no network ──────────────────────────────

def test_scheme_https_allowed():
    validate_url_scheme("https://example.com/path")  # must not raise


def test_scheme_http_allowed():
    validate_url_scheme("http://example.com/path")  # must not raise


def test_scheme_file_blocked():
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        validate_url_scheme("file:///etc/passwd")


def test_scheme_javascript_blocked():
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        validate_url_scheme("javascript:alert(1)")


def test_scheme_data_blocked():
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        validate_url_scheme("data:text/html,<script>alert(1)</script>")


def test_scheme_about_blocked():
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        validate_url_scheme("about:blank")


def test_scheme_ftp_blocked():
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        validate_url_scheme("ftp://example.com/file")


def test_url_with_embedded_credentials_blocked():
    with pytest.raises(BlockedURLError, match="credentials"):
        validate_url_scheme("https://user:password@example.com/")


def test_url_with_only_username_blocked():
    with pytest.raises(BlockedURLError, match="credentials"):
        validate_url_scheme("https://user@example.com/")


def test_url_without_hostname_blocked():
    with pytest.raises(BlockedURLError, match="no hostname"):
        validate_url_scheme("https:///path-only")


# ── resolve_and_validate_host — DNS-mocked ──────────────────────────────────────

async def test_public_ipv4_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("93.184.216.34"))
    await resolve_and_validate_host("example.com", 80)  # must not raise


async def test_loopback_ipv4_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("127.0.0.1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("localhost", 80)


async def test_loopback_ipv6_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("::1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("localhost6", 80)


@pytest.mark.parametrize("ip", ["10.0.0.1", "172.16.0.1", "192.168.1.1"])
async def test_rfc1918_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo(ip))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("internal.example", 80)


async def test_link_local_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("169.254.1.1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("link-local.example", 80)


async def test_cloud_metadata_endpoint_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("169.254.169.254"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("metadata.example", 80)


async def test_ipv6_unique_local_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("fc00::1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("ula.example", 80)


async def test_ipv6_link_local_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("fe80::1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("v6-link-local.example", 80)


async def test_ipv4_mapped_ipv6_loopback_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("::ffff:127.0.0.1"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("mapped.example", 80)


async def test_dns_resolution_failure_blocked(monkeypatch):
    def _raise(*_a, **_k):
        raise socket.gaierror("name resolution failed")
    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(BlockedURLError, match="DNS resolution failed"):
        await resolve_and_validate_host("nonexistent.invalid", 80)


async def test_multi_answer_dns_any_private_blocks_all(monkeypatch):
    """DNS rebinding-style multi-answer: if even one resolved address is
    private, the whole request must be blocked — not just skip the bad one."""
    def _multi(*_a, **_k):
        return _fake_getaddrinfo("93.184.216.34") + _fake_getaddrinfo("10.0.0.1")
    monkeypatch.setattr(socket, "getaddrinfo", _multi)
    with pytest.raises(BlockedURLError, match="non-public address"):
        await resolve_and_validate_host("mixed.example", 80)


# ── validate_request_url — full pipeline ────────────────────────────────────────

async def test_full_validation_blocks_scheme_before_dns(monkeypatch):
    """file:// must be rejected without ever touching DNS resolution."""
    def _fail_if_called(*_a, **_k):
        raise AssertionError("DNS should not be resolved for a blocked scheme")
    monkeypatch.setattr(socket, "getaddrinfo", _fail_if_called)
    with pytest.raises(BlockedURLError, match="scheme not allowed"):
        await validate_request_url("file:///etc/passwd")


async def test_full_validation_blocks_private_ip_after_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("10.1.2.3"))
    with pytest.raises(BlockedURLError, match="non-public address"):
        await validate_request_url("https://internal.example/secret")


async def test_full_validation_allows_public_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_getaddrinfo("93.184.216.34"))
    await validate_request_url("https://example.com/")  # must not raise
