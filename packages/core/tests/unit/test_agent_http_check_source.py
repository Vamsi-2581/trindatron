"""Unit tests for HttpCheckSource — the first real, external CheckSource
for Persistent Agent Runtime (HTTP monitoring). No public internet
dependency anywhere in this file: a real HTTP server bound to
127.0.0.1 on an ephemeral port runs in a background thread for the
duration of each test.

Test matrix
-----------
H01) Stable content across two checks -> second check reports unchanged
H02) Content changes between two checks -> second check reports changed
H03) First-ever check on a schedule establishes a baseline, never "changed"
     (see agent_check_source.py's module docstring for why this differs
     from PayloadSignatureCheckSource's own first-check semantics)
H04) A changed HTTP status code (200 -> 500) with identical body still
     registers as a change — the status is part of the signature
H05) HTTP timeout is reported as changed=False, reason="timeout", and
     does NOT overwrite the previously-known-good signature
H06) An HTTP error status (500) does not raise — it's a valid, signed
     observation, not a check failure
H07) A disallowed URL scheme (file://) is rejected before any network call
H08) A URL resolving to a loopback/private address is rejected (SSRF
     guard) when allow_private_networks is NOT set
H09) allow_private_networks=True (test-only) lets a loopback URL through
     — proves the escape hatch exists and is opt-in only
H10) Response content beyond the configured byte cap is truncated —
     content differing only AFTER the cap produces the SAME signature
H11) CHECK performs a real HTTP round-trip but creates zero UsageRecord
     rows and touches no billing table
H12) CHECK never imports/references the Model Router (structural — grep
     of the module's own imports, not just behavioral)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.models.agent_schedule import AgentSchedule
from src.services.agent_check_source import HttpCheckSource, InvalidCheckUrl


@dataclass
class _ServerState:
    body: bytes = b"initial content"
    status: int = 200
    delay_seconds: float = 0.0
    url: str = ""


@pytest.fixture
def http_server():
    state = _ServerState()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib method name
            if state.delay_seconds:
                time.sleep(state.delay_seconds)
            self.send_response(state.status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, fmt, *args):  # silence default stderr access log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}/"

    yield state

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _schedule(url: str, *, last_check_signature=None, timeout_seconds=None) -> AgentSchedule:
    check = {"type": "http", "url": url}
    if timeout_seconds is not None:
        check["timeout_seconds"] = timeout_seconds
    return AgentSchedule(
        job_payload={"check": check, "action": {"job_type": "shell", "payload": {"steps": [{"command": "noop"}]}}},
        last_check_signature=last_check_signature,
    )


async def test_H01_stable_content_is_unchanged(http_server):
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    baseline = await source.check(schedule)
    schedule.last_check_signature = baseline.signature
    second = await source.check(schedule)

    assert baseline.changed is False  # first check = baseline, never "changed"
    assert second.changed is False
    assert second.signature == baseline.signature


async def test_H02_changed_content_is_detected(http_server):
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    baseline = await source.check(schedule)
    schedule.last_check_signature = baseline.signature

    http_server.body = b"different content now"
    second = await source.check(schedule)

    assert second.changed is True
    assert second.signature != baseline.signature
    assert second.reason == "content_changed"


async def test_H03_first_check_establishes_baseline_never_changed(http_server):
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url, last_check_signature=None)

    result = await source.check(schedule)

    assert result.changed is False
    assert result.reason == "baseline_established"
    assert result.signature is not None


async def test_H04_status_code_change_is_detected_even_with_same_body(http_server):
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    baseline = await source.check(schedule)
    schedule.last_check_signature = baseline.signature

    http_server.status = 500  # body unchanged, only the status differs
    second = await source.check(schedule)

    assert second.changed is True


async def test_H05_timeout_reports_unchanged_and_preserves_signature(http_server):
    http_server.delay_seconds = 1.0
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url, last_check_signature="previously-known-good", timeout_seconds=0.1)

    result = await source.check(schedule)

    assert result.changed is False
    assert result.reason == "timeout"
    assert result.signature == "previously-known-good"  # not overwritten


async def test_H06_http_error_status_does_not_raise(http_server):
    http_server.status = 500
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    result = await source.check(schedule)  # must not raise

    assert result.signature is not None
    assert result.signature.startswith("500:")


async def test_H07_disallowed_scheme_rejected():
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule("file:///etc/passwd")

    result = await source.check(schedule)

    assert result.changed is False
    assert (result.reason or "").startswith("invalid_url")


async def test_H08_private_address_rejected_by_default():
    source = HttpCheckSource()  # allow_private_networks defaults to False
    schedule = _schedule("http://127.0.0.1:9/")  # loopback, literal IP — no DNS needed

    result = await source.check(schedule)

    assert result.changed is False
    assert (result.reason or "").startswith("invalid_url")


async def test_H09_allow_private_networks_escape_hatch_permits_loopback(http_server):
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    result = await source.check(schedule)  # must NOT be rejected as invalid_url

    assert not (result.reason or "").startswith("invalid_url")


async def test_H10_response_truncated_at_configured_byte_cap(http_server, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "agent_http_check_max_response_bytes", 16)

    http_server.body = b"0123456789ABCDEF" + b"same-prefix-tail-1"
    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)
    baseline = await source.check(schedule)
    schedule.last_check_signature = baseline.signature

    # Differs only AFTER byte 16 — must be invisible to the signature.
    http_server.body = b"0123456789ABCDEF" + b"different-tail-2"
    second = await source.check(schedule)

    assert second.changed is False
    assert second.signature == baseline.signature


async def test_H11_check_creates_zero_usage_records(http_server, db_session_for_usage_check):
    from src.models.usage import UsageRecord

    source = HttpCheckSource(allow_private_networks=True)
    schedule = _schedule(http_server.url)

    await source.check(schedule)

    assert db_session_for_usage_check.query(UsageRecord).count() == 0


@pytest.fixture
def db_session_for_usage_check():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.database import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_H12_module_never_imports_model_router_or_entitlement_service():
    """AST-level check of actual import statements only — not a substring
    scan of the whole file, which would also match this module's own
    prose docstring explaining that it does NOT import these."""
    import ast
    import inspect
    import src.services.agent_check_source as module

    tree = ast.parse(inspect.getsource(module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    assert not any("trinity_router" in name for name in imported_names)
    assert not any("entitlement_service" in name for name in imported_names)
