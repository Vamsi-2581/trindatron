"""Agent Check Source — the CHECK step of Persistent Agent Runtime V1's
WAKE -> CHECK -> DECIDE -> ACT -> SLEEP loop (see agent_schedule_service.py's
`_fire_due_schedules()`).

Tier 1 scope, enforced by construction here: CHECK is exclusively
non-LLM. Nothing in this module imports trinity_router, entitlement_service,
or anything that could reserve TCU — a CheckSource implementation has no
way to bill anything even if it tried, short of importing those modules
itself against this file's own intent. DECIDE (whether the detected
change is worth acting on) is a separate concern —
adaptive_budget_service.should_skip_llm() — not this module's job.

Why a small ABC and not a bigger "data source framework"
------------------------------------------------------------
CheckSource has exactly one method. `_fire_due_schedules()` picks WHICH
implementation to use per-schedule via `build_check_source_for_schedule()`
below, based on `schedule.job_payload["check"]["type"]` — a future
CheckSource watching a different kind of external signal (a file's mtime,
a webhook-delivered event, a metric) is a straightforward addition of ONE
more class implementing this same interface plus one more branch in that
factory; nothing else changes.

async, not sync — a deliberate, necessary change from Tier 1
------------------------------------------------------------------
Tier 1 shipped `check()` as a plain `def` because its only implementation
(PayloadSignatureCheckSource) is pure CPU-bound hashing — no I/O. The
first real external CheckSource (HttpCheckSource, below) makes a network
call, and `_fire_due_schedules()` runs inside `run_schedule_poll_loop()`,
a background asyncio task sharing the SAME event loop as every HTTP
request and the durable execution worker. A blocking synchronous HTTP
call here would stall the entire process for up to the check's timeout,
once per due HTTP-checked schedule, every poll cycle — a real, silent
throughput/latency regression for everything else running on that loop,
not a hypothetical. Every other I/O in this codebase already goes through
`asyncio`/`asyncio.to_thread()` for exactly this reason (see
execution_worker.py, model_router_provider.py); making CHECK's own
interface `async def` is applying that SAME established pattern here, not
inventing a new one. PayloadSignatureCheckSource.check() becomes
`async def` too (trivially — it still does no `await` internally) so
both implementations share one interface.

Two implementations, two different "first observation" semantics —
deliberate, not inconsistent
-----------------------------------------------------------------------
PayloadSignatureCheckSource: `changed=True` on the very first check (no
previous signature) — it detects "the schedule's OWN task definition
changed", and a brand-new schedule's payload is definitionally new
relative to nothing; running it once on creation is the useful behavior,
and this is the ALREADY-SHIPPED, tested Tier-1 behavior (unchanged here).

HttpCheckSource: `changed=False` on the very first check — it detects
"the observed EXTERNAL content changed since I last looked", and there is
no previous look to compare against yet, so the first check can only
ever establish a baseline, never observe a "change". Creating an HTTP
monitoring agent must never fire its action immediately just because
nothing has been observed yet — the mission's own explicit preference,
verified against `_fire_due_schedules()`'s own logic (which has no
opinion on this either way — the decision lives entirely inside each
CheckSource's own `changed` computation, exactly the extension point the
abstraction was designed for) and found to have no conflict.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from src.config import settings
from src.models.agent_schedule import AgentSchedule

logger = logging.getLogger("trinity.agent_check")

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class CheckResult:
    """Result of one CHECK — see module docstring. `signature` is
    whatever string form of "the observed state" the source used to
    decide `changed`; persisted verbatim onto
    AgentSchedule.last_check_signature so the NEXT check has something to
    compare against. `reason` is observability-only (logged, never
    validated against a controlled vocabulary — unlike
    AgentSchedule.last_decision, which is)."""

    changed: bool
    signature: str | None
    reason: str | None = None
    # Persistent Agent Runtime — Tier 2 (LLM DECIDE) only. A SMALL, bounded
    # sample of whatever was actually observed this cycle, given to the
    # DECIDE prompt as context — see agent_decision.py's module docstring
    # on why this is treated strictly as untrusted DATA, never instructions.
    # None for a CheckSource with nothing meaningful to show (e.g.
    # PayloadSignatureCheckSource — the schedule's own payload isn't
    # "observed external content"). Never used by Tier 1's non-LLM DECIDE
    # or by anything that could bill/execute — purely descriptive.
    observed_excerpt: str | None = None


class CheckSource(ABC):
    """Non-LLM, non-billing observation. Implementations must never call
    the Model Router or entitlement_service — see module docstring."""

    @abstractmethod
    async def check(self, schedule: AgentSchedule) -> CheckResult:
        ...


class PayloadSignatureCheckSource(CheckSource):
    """Tier 1 default — see module docstring's "first observation"
    section for why this treats the first check as a change and
    HttpCheckSource does not. Signs `schedule.job_payload` with a stable
    hash (sorted keys, so key-order-only differences never register as a
    change) and compares it against `schedule.last_check_signature`."""

    async def check(self, schedule: AgentSchedule) -> CheckResult:
        signature = self._signature(schedule.job_payload)
        previous = schedule.last_check_signature
        changed = signature != previous
        if previous is None:
            reason = "no_previous_signature"
        elif changed:
            reason = "payload_changed"
        else:
            reason = "payload_unchanged"
        return CheckResult(changed=changed, signature=signature, reason=reason)

    @staticmethod
    def _signature(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InvalidCheckUrl(ValueError):
    """Raised by _validate_url() — scheme not allowed, missing host, or
    (unless allow_private_networks) resolves to a private/loopback/
    link-local/reserved/multicast address. Never includes the raw
    exception from socket.getaddrinfo() verbatim in a way that could leak
    internal resolver detail beyond the address itself, which is already
    what's being rejected, not a secret."""


async def _validate_url(url: str, *, allow_private_networks: bool) -> None:
    if not url or not isinstance(url, str):
        raise InvalidCheckUrl("missing url")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise InvalidCheckUrl(f"scheme {parsed.scheme!r} not allowed (only http/https)")
    if not parsed.hostname:
        raise InvalidCheckUrl("missing host")
    if allow_private_networks:
        # Test-only escape hatch — see HttpCheckSource.__init__'s
        # docstring. Never True in any production code path.
        return

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except socket.gaierror as exc:
        raise InvalidCheckUrl(f"DNS resolution failed for host {parsed.hostname!r}: {exc}") from exc

    for info in infos:
        raw_ip = info[4][0]
        ip = ipaddress.ip_address(raw_ip.split("%")[0])  # strip IPv6 zone id if present
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise InvalidCheckUrl(f"host {parsed.hostname!r} resolves to a disallowed address ({ip})")


class HttpCheckSource(CheckSource):
    """CHECK for `job_payload["check"]["type"] == "http"` — a single
    bounded GET/HEAD request, fingerprinted by (status_code, body sample).
    Deliberately minimal: no cookies/redirect-following beyond httpx's
    own default, no headers/auth support, no scraping/parsing — a plain
    "did the raw response change" signal for the first real use case.
    See module docstring for the security constraints this enforces.

    Config read from schedule.job_payload["check"]:
      url (required), method (default "GET"),
      timeout_seconds (default settings.agent_http_check_default_timeout_seconds,
      hard-capped at settings.agent_http_check_max_timeout_seconds
      regardless of what's configured).
    """

    def __init__(self, *, allow_private_networks: bool = False) -> None:
        # allow_private_networks: TEST ONLY — lets a test point this at a
        # local httpserver (127.0.0.1) without weakening the real SSRF
        # guard. build_check_source_for_schedule() (the only production
        # call site) never sets this True.
        self._allow_private_networks = allow_private_networks

    async def check(self, schedule: AgentSchedule) -> CheckResult:
        config = (schedule.job_payload or {}).get("check") or {}
        url = config.get("url")
        method = str(config.get("method") or "GET").upper()
        timeout = min(
            float(config.get("timeout_seconds") or settings.agent_http_check_default_timeout_seconds),
            settings.agent_http_check_max_timeout_seconds,
        )

        try:
            await _validate_url(url, allow_private_networks=self._allow_private_networks)
        except InvalidCheckUrl as exc:
            logger.warning("agent_check(http): invalid url | schedule=%s reason=%s", schedule.id, exc)
            return CheckResult(changed=False, signature=schedule.last_check_signature, reason=f"invalid_url:{exc}")

        try:
            status_code, body_sample = await self._fetch(url, method, timeout)
        except httpx.TimeoutException:
            logger.warning("agent_check(http): timeout | schedule=%s timeout=%.1fs", schedule.id, timeout)
            return CheckResult(changed=False, signature=schedule.last_check_signature, reason="timeout")
        except httpx.HTTPError as exc:
            # Connection-level failure (refused, DNS race, TLS error, ...)
            # — never the raw exception text verbatim (may embed request
            # detail); class name is enough for a metric/log reason.
            logger.warning("agent_check(http): connection error | schedule=%s type=%s", schedule.id, type(exc).__name__)
            return CheckResult(changed=False, signature=schedule.last_check_signature, reason="connection_error")

        # The HTTP status IS part of the signature (a site starting to
        # error, e.g. 200 -> 500, is itself an observable change worth
        # detecting) — this is deliberately NOT treated as a check
        # failure; see module docstring.
        signature = self._signature(status_code, body_sample)
        # Tier 2 (LLM DECIDE) context only — a small, bounded, best-effort
        # decode of the SAME already-in-memory body_sample used for the
        # signature above (no additional fetch, no larger buffer than
        # settings.agent_http_check_max_response_bytes already allows).
        # errors="replace" rather than raising on invalid encoding — this
        # is untrusted external bytes, never allowed to break CHECK.
        excerpt = body_sample[:2000].decode("utf-8", errors="replace")
        previous = schedule.last_check_signature
        if previous is None:
            # See module docstring's "first observation" section —
            # HttpCheckSource ALWAYS establishes a baseline on the first
            # check, never reports a change.
            return CheckResult(changed=False, signature=signature, reason="baseline_established", observed_excerpt=excerpt)
        changed = signature != previous
        reason = "content_changed" if changed else "content_unchanged"
        return CheckResult(changed=changed, signature=signature, reason=reason, observed_excerpt=excerpt)

    async def _fetch(self, url: str, method: str, timeout: float) -> tuple[int, bytes]:
        """Streams up to settings.agent_http_check_max_response_bytes and
        then abandons the connection — never buffers an unbounded
        response. Never logs headers (would risk echoing a Set-Cookie or
        similar) — only status_code and the byte count are ever logged."""
        max_bytes = settings.agent_http_check_max_response_bytes
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(method, url) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        break
                return response.status_code, bytes(body[:max_bytes])

    @staticmethod
    def _signature(status_code: int, body_sample: bytes) -> str:
        digest = hashlib.sha256(body_sample).hexdigest()
        return f"{status_code}:{digest}"


def default_check_source() -> CheckSource:
    return PayloadSignatureCheckSource()


def build_check_source_for_schedule(schedule: AgentSchedule) -> CheckSource:
    """The one place job_payload["check"]["type"] is mapped to a concrete
    CheckSource — called fresh per schedule per cycle (never cached), so
    editing a schedule's check config takes effect on its very next wake.
    A schedule with no "check" key at all (every schedule that predates
    this feature, and every schedule that just wants the Tier-1 default)
    falls back to PayloadSignatureCheckSource unchanged."""
    payload = schedule.job_payload if isinstance(schedule.job_payload, dict) else {}
    check_config = payload.get("check")
    if not check_config:
        return default_check_source()
    check_type = check_config.get("type")
    if check_type == "http":
        return HttpCheckSource()
    logger.warning(
        "agent_check: unknown check.type %r for schedule=%s — falling back to the default check source",
        check_type, schedule.id,
    )
    return default_check_source()
