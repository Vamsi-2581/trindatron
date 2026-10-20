"""groq.oss_120b / groq.oss_20b — FREE CORE CANDIDATE providers on Groq's free tier.

CP10/CP11 measured `openai/gpt-oss-120b` (and, as a lighter reference, the ACTIVE
`openai/gpt-oss-20b`) against the internal benchmark harness. CP12 turns that
measurement into a controllable, isolated code path — never a change to the real
Model Router. Both models stay recorded in `trinity_router.lab.LAB_CATALOG`, which
routing cannot reach (see `packages/model-router/tests/test_lab_isolation.py`); a
caller here has to ask for this module BY NAME, and still gets refused while its
settings flag is false.

Two structural fixes this module exists to enforce, not to leave to caller discipline:

1. **The tool-call fix (CP11's finding).** CP11 measured Groq's own server-side
   validation rejecting `openai/gpt-oss-120b`'s and `openai/gpt-oss-20b`'s OWN
   response with HTTP 400 ("Tool choice is none, but model called a tool") when a
   prompt looked tool-shaped but no `tools` field was sent at all — confirmed live
   2026-09-19/20, worse on the 20B (2 distinct tasks, 100% failure) than the 120B
   (1 task, 3 of 4 calls). `trinity_router.router.py`'s shared `complete()` sends
   `tools=request.tools or None` when the caller has none — the universally-safe
   value for OpenAI/Anthropic/Mistral/Gemini/xAI (see that module's own comment),
   but exactly the shape that trips this Groq-specific quirk. This module NEVER
   omits `tools`: it sends `tools=[], tool_choice="none"` when the caller wants no
   tools, and the caller's real schema with `tool_choice="auto"` (or an explicit
   choice) when it does. This fix is scoped to this module ONLY — `router.py` and
   every other provider are unchanged; the bug is Groq-specific, not a router-wide
   one (gpt-4o-mini/mistral-small never showed it in production).

2. **The free-tier guard.** Groq's own documented Free-plan limits (confirmed live
   2026-09-19, console.groq.com/docs/rate-limits) are IDENTICAL for both models:
   30 RPM / 1,000 RPD / 8,000 TPM / 200,000 TPD — tracked PER MODEL, not per
   account (CP10/CP11 measured both models' budgets depleting independently). A
   process-local `FreeTierGuard` estimates a call's tokens BEFORE sending it and
   refuses outright once a configurable safety margin of any window would be
   exceeded — never a retry-past-the-limit, never a silent fallback to a paid
   provider (see `FreeTierExhausted`). This is a conservative, single-process
   safety net on top of Groq's own enforcement, not a replacement for it: it does
   not survive a restart and is not shared across replicas.

Quality: every completion is judged by the SAME, unmodified
`trinity_router.quality_gate.evaluate_from_messages` production already uses for
every other provider — empty response, boilerplate refusal (with the existing
sensitive-topic carve-out), language mismatch. Nothing here relaxes it. A tool call
this module receives is additionally validated against the `tools` the caller
actually declared (name and JSON-parseable arguments) — an unrequested or
malformed tool call is a controlled failure here, never passed through as if it
were a real answer.

Billing: nothing here ever calls `entitlement_service`/`UsageRecord` — no TCU is
charged, by construction, matching `llm.local`'s (`src/local_runtime/provider.py`)
own contract. Consumption is observability only (`groq_candidate_*` metrics) plus
the in-process quota counters this module keeps for its own guard.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from src.local_runtime.llm import local_runtime_settings
from src.observability.metrics import core_metrics

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
# CP10 finding: Groq's Cloudflare WAF returns a bare 403 (error 1010, a bot-signature
# block) for a default HTTP client User-Agent. Never omit this header.
_USER_AGENT = "trindatron-groq-candidate/1.0"

# Confirmed live 2026-09-19 (console.groq.com/docs/rate-limits) — identical for both
# models despite the 6x parameter difference. Per-model, not per-account.
FREE_TIER_LIMITS = {
    "openai/gpt-oss-20b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200_000},
    "openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200_000},
}
#: Never spend the LAST slice of any window — leaves room for the estimate's own
#: error margin and for anything else sharing the same Groq account.
SAFETY_MARGIN = 0.85
_CHARS_PER_TOKEN = 3  # see local_runtime/provider.py's own estimator and comment

_requests = core_metrics.counter("groq_candidate_requests_total",
                                 "Groq FREE CORE CANDIDATE calls, labeled by model and outcome.")
_latency_ms = core_metrics.histogram("groq_candidate_latency_ms",
                                     "Groq FREE CORE CANDIDATE call latency in milliseconds.")
_tokens = core_metrics.counter("groq_candidate_tokens_total",
                               "Groq FREE CORE CANDIDATE tokens, labeled by model and direction.")
_quota_denied = core_metrics.counter("groq_candidate_quota_denied_total",
                                     "Calls refused before sending because the free-tier guard was near a limit.")


class GroqCandidateError(Exception):
    """A refused or failed candidate call. `code` is stable and safe to log or return.
    Never triggers a fallback to a paid provider by itself — that decision, if any,
    belongs entirely to the caller (CP12 rule: no automatic paid fallback here)."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail  # safe, provider-side detail only — never a secret or a path


class FreeTierExhausted(GroqCandidateError):
    def __init__(self, window: str) -> None:
        super().__init__("quota_exhausted", detail=window)
        self.window = window


@dataclass
class _Call:
    at: float
    tokens: int


@dataclass
class FreeTierGuard:
    """Process-local pre-flight budget for ONE Groq model. Not a substitute for
    Groq's own 429s — see the module docstring. `now_fn`/`today_fn` are injectable
    so tests never depend on real wall-clock time."""

    rpm: int
    rpd: int
    tpm: int
    tpd: int
    safety_margin: float = SAFETY_MARGIN
    now_fn: "callable[[], float]" = field(default=time.monotonic)
    today_fn: "callable[[], str]" = field(default=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    _minute: list[_Call] = field(default_factory=list)
    _day: list[_Call] = field(default_factory=list)
    _day_key: str = field(default="")

    def _purge(self) -> None:
        now = self.now_fn()
        self._minute = [c for c in self._minute if now - c.at < 60.0]
        today = self.today_fn()
        if today != self._day_key:
            self._day_key = today
            self._day = []

    def snapshot(self) -> dict:
        """Current consumption, for observability — never mutates state."""
        self._purge()
        return {
            "requests_this_minute": len(self._minute), "rpm_limit": self.rpm,
            "requests_today": len(self._day), "rpd_limit": self.rpd,
            "tokens_this_minute": sum(c.tokens for c in self._minute), "tpm_limit": self.tpm,
            "tokens_today": sum(c.tokens for c in self._day), "tpd_limit": self.tpd,
        }

    def reserve(self, estimated_tokens: int) -> None:
        """Raise FreeTierExhausted BEFORE a call would push any window past its
        safety-margined budget. Reserves optimistically on success; `settle()`
        corrects the reservation once the real token count is known."""
        self._purge()
        margin = self.safety_margin
        if len(self._minute) + 1 > self.rpm * margin:
            raise FreeTierExhausted("rpm")
        if len(self._day) + 1 > self.rpd * margin:
            raise FreeTierExhausted("rpd")
        if sum(c.tokens for c in self._minute) + estimated_tokens > self.tpm * margin:
            raise FreeTierExhausted("tpm")
        if sum(c.tokens for c in self._day) + estimated_tokens > self.tpd * margin:
            raise FreeTierExhausted("tpd")
        call = _Call(self.now_fn(), estimated_tokens)
        self._minute.append(call)
        self._day.append(call)

    def settle(self, reserved_estimate: int, actual_tokens: int) -> None:
        """Replace the optimistic estimate with what was really spent, on the most
        recent reservation — keeps the running totals honest without a per-call
        handle to thread back through the caller. `reserve()` appends the SAME
        `_Call` object to both `_minute` and `_day`, so it is mutated exactly
        once here; applying `delta` to each list's last entry independently
        would double-count it against that one shared object."""
        delta = actual_tokens - reserved_estimate
        if not delta:
            return
        target = self._minute[-1] if self._minute else (self._day[-1] if self._day else None)
        if target is not None:
            target.tokens = max(0, target.tokens + delta)


def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return chars // _CHARS_PER_TOKEN + 8 * len(messages) + max_tokens


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, as the provider sent it


@dataclass(frozen=True)
class GroqCandidateResult:
    text: str
    model: str
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    quota: dict
    provider_cost_usd: float = 0.0  # free tier: nothing billed by Groq while within quota


# One guard per model, process-wide (module-level singletons — matches the fact that
# Groq tracks these budgets per model, per account, not per request or per caller).
_guards: dict[str, FreeTierGuard] = {
    model: FreeTierGuard(**limits) for model, limits in FREE_TIER_LIMITS.items()
}


def guard_for(model: str) -> FreeTierGuard:
    if model not in _guards:
        raise GroqCandidateError("unknown_model", detail=model)
    return _guards[model]


class GroqCandidateProvider:
    """One Groq FREE CORE CANDIDATE model: `openai/gpt-oss-20b` or `openai/gpt-oss-120b`.

    `fallback` is never automatic: on `FreeTierExhausted` or any other
    `GroqCandidateError`, the caller decides — e.g. explicitly instantiating a
    second `GroqCandidateProvider(model="openai/gpt-oss-20b")` — never something
    this class does on its own, and never a paid provider (CP12 rule)."""

    def __init__(self, model: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if model not in FREE_TIER_LIMITS:
            raise GroqCandidateError("unknown_model", detail=model)
        self.model = model
        self._transport = transport
        self._guard = guard_for(model)

    def enabled(self) -> bool:
        settings = local_runtime_settings()
        from src.config import settings as core_settings  # local import: avoids a config<->local_runtime cycle
        if self.model == "openai/gpt-oss-120b" and not settings.groq_oss_120b_enabled:
            return False
        return bool(core_settings.groq_api_key)

    def quota(self) -> dict:
        return self._guard.snapshot()

    async def complete(self, messages: list[dict], *, max_tokens: int = 512,
                       tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                       response_format: dict | None = None) -> GroqCandidateResult:
        from src.config import settings as core_settings

        if self.model == "openai/gpt-oss-120b" and not local_runtime_settings().groq_oss_120b_enabled:
            _requests.inc(model=self.model, outcome="disabled")
            raise GroqCandidateError("disabled")
        api_key = core_settings.groq_api_key
        if not api_key:
            _requests.inc(model=self.model, outcome="not_configured")
            raise GroqCandidateError("not_configured")
        if not messages or not all(isinstance(m, dict) and m.get("role") and "content" in m for m in messages):
            _requests.inc(model=self.model, outcome="invalid")
            raise GroqCandidateError("invalid_request")

        estimated = estimate_tokens(messages, max_tokens)
        try:
            self._guard.reserve(estimated)
        except FreeTierExhausted as exc:
            _requests.inc(model=self.model, outcome="quota_exhausted")
            _quota_denied.inc(model=self.model, window=exc.window)
            raise

        # The tool-call fix: `tools` is NEVER omitted. An empty declared list plus
        # tool_choice="none" is what actually stops Groq's own validator from
        # later rejecting a spontaneous tool call as "Tool choice is none, but
        # model called a tool" — omitting the key entirely (litellm's/this
        # module's naive default) is exactly the shape CP11 found broken.
        if tools:
            payload_tools: list[dict] = tools
            payload_choice = tool_choice or "auto"
        else:
            payload_tools = []
            payload_choice = "none"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
            "tools": payload_tools,
            "tool_choice": payload_choice,
        }
        if response_format is not None:
            # CP12, confirmed live: Groq's strict json_schema mode 400s unless every
            # object in the schema sets "additionalProperties": false explicitly —
            # the caller's schema, not this module's job to inject; surfaced as an
            # ordinary provider_error below if missing, never guessed or patched here.
            payload["response_format"] = response_format

        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json",
                  "User-Agent": _USER_AGENT}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=60.0, trust_env=False) as client:
                response = await client.post(GROQ_ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            _requests.inc(model=self.model, outcome="unavailable")
            raise GroqCandidateError("unavailable", detail=type(exc).__name__) from None
        latency_ms = (time.monotonic() - started) * 1000

        if response.status_code >= 400:
            # Groq's own detail text is safe to keep (no secret, no path) but
            # capped — never let a provider body balloon into a log line.
            try:
                detail = response.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = f"HTTP {response.status_code}"
            outcome = "rate_limited" if response.status_code == 429 else "provider_error"
            _requests.inc(model=self.model, outcome=outcome)
            raise GroqCandidateError(outcome, detail=detail)

        body = response.json()
        try:
            message = body["choices"][0]["message"]
            usage = body.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            _requests.inc(model=self.model, outcome="malformed_response")
            raise GroqCandidateError("malformed_response") from None

        raw_tool_calls = message.get("tool_calls") or []
        declared_names = {t.get("function", {}).get("name") for t in (tools or [])}
        validated: list[ToolCall] = []
        for call in raw_tool_calls:
            fn = call.get("function") or {}
            name, arguments = fn.get("name"), fn.get("arguments")
            if name not in declared_names or not isinstance(arguments, str):
                _requests.inc(model=self.model, outcome="invalid_tool_call")
                raise GroqCandidateError("invalid_tool_call", detail=str(name))
            try:
                json.loads(arguments)
            except (ValueError, TypeError):
                _requests.inc(model=self.model, outcome="invalid_tool_call")
                raise GroqCandidateError("invalid_tool_call", detail="arguments not valid JSON")
            validated.append(ToolCall(id=call.get("id", ""), name=name, arguments=arguments))

        text = message.get("content") or ""
        from trinity_router import quality_gate
        gate = quality_gate.evaluate_from_messages(
            messages=messages, response_text=text, has_tool_calls=bool(validated),
        )
        if not gate.passed:
            _requests.inc(model=self.model, outcome=f"quality_gate_{gate.primary_reason}")
            raise GroqCandidateError(f"quality_gate:{gate.primary_reason}")

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        self._guard.settle(estimated, input_tokens + output_tokens)
        _requests.inc(model=self.model, outcome="completed")
        _latency_ms.observe(latency_ms, model=self.model, outcome="completed")
        _tokens.inc(input_tokens, model=self.model, direction="input")
        _tokens.inc(output_tokens, model=self.model, direction="output")
        return GroqCandidateResult(
            text=text, model=self.model, tool_calls=tuple(validated),
            input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms,
            quota=self._guard.snapshot(),
        )
