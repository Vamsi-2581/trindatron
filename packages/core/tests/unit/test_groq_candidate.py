"""groq.oss_120b / groq.oss_20b FREE CORE CANDIDATE — src/local_runtime/groq_candidate.py.

Every call goes through httpx.MockTransport; nothing here reaches the real network
(conftest.py's autouse block_real_network fixture would refuse it anyway). Covers
CP12's mandate: A) no-tools request, B) with-tools request, C) the model inventing a
tool call Groq itself rejects, D) a valid tool call, E) structured output, F) a
controlled failure path — plus the free-tier guard and the reused Quality Gate.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.local_runtime.groq_candidate import (
    FreeTierExhausted,
    FreeTierGuard,
    GroqCandidateError,
    GroqCandidateProvider,
)

MESSAGES = [{"role": "user", "content": "What is 2 + 2?"}]
# Same prompt as the internal benchmark's chat_language_mirroring task — enough
# recognizable Portuguese stopwords ("em", "o", "que", "é", "um", "de") for
# quality_gate._detect_language's conservative threshold to fire confidently.
MESSAGES_PT = [{"role": "user", "content": "Explique em duas frases o que é um índice de banco de dados."}]


def _response(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def _completion(content=None, tool_calls=None, prompt_tokens=50, completion_tokens=20) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def provider(handler, *, model="openai/gpt-oss-120b", enabled=True, monkeypatch=None) -> GroqCandidateProvider:
    p = GroqCandidateProvider(model, transport=httpx.MockTransport(handler))
    if monkeypatch is not None:
        monkeypatch.setattr("src.config.settings.groq_api_key", "gsk_test_key_not_real")
        if model == "openai/gpt-oss-120b":
            from src.local_runtime.llm import local_runtime_settings
            monkeypatch.setattr(local_runtime_settings(), "groq_oss_120b_enabled", enabled)
    return p


@pytest.fixture(autouse=True)
def _fresh_guards():
    """Each test gets untouched per-model quota counters — the guards are process-wide
    singletons (src.local_runtime.groq_candidate._guards), same leakage risk conftest.py
    already documents for request_control/agent_scheduler."""
    from src.local_runtime import groq_candidate as gc
    for model, limits in gc.FREE_TIER_LIMITS.items():
        gc._guards[model] = gc.FreeTierGuard(**limits)
    yield


# ── A. Request with NO tools ──────────────────────────────────────────────────

async def test_a_no_tools_request_sends_explicit_empty_list_and_none(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(_completion(content="4"))

    p = provider(handler, monkeypatch=monkeypatch)
    result = await p.complete(MESSAGES, max_tokens=50)

    assert seen["body"]["tools"] == []
    assert seen["body"]["tool_choice"] == "none"
    assert result.text == "4"
    assert result.tool_calls == ()


# ── B. Request WITH tools ─────────────────────────────────────────────────────

async def test_b_with_tools_request_sends_the_real_schema_and_auto(monkeypatch):
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(_completion(content="ok"))

    p = provider(handler, monkeypatch=monkeypatch)
    await p.complete(MESSAGES, max_tokens=50, tools=tools)

    assert seen["body"]["tools"] == tools
    assert seen["body"]["tool_choice"] == "auto"


async def test_b_explicit_tool_choice_is_respected(monkeypatch):
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(_completion(content="ok"))

    p = provider(handler, monkeypatch=monkeypatch)
    await p.complete(MESSAGES, max_tokens=50, tools=tools, tool_choice="required")

    assert seen["body"]["tool_choice"] == "required"


# ── C. The model inventing a tool call with none declared ────────────────────

async def test_c_groqs_own_rejection_of_an_invented_tool_call_is_a_controlled_failure(monkeypatch):
    """This is exactly CP11's finding, reproduced at the HTTP layer: even with
    tools=[] and tool_choice="none" correctly sent (the fix), Groq's own server
    can still refuse ITS model's spontaneous tool-call attempt with a 400 — the
    module must turn that into a stable GroqCandidateError, never crash or
    silently fabricate a text answer."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _response({"error": {"message": "Tool choice is none, but model called a tool"}}, status=400)

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "provider_error"
    assert "Tool choice is none" in exc_info.value.detail


# ── D. A valid tool call ───────────────────────────────────────────────────────

async def test_d_a_valid_tool_call_is_parsed_and_validated(monkeypatch):
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    tool_calls = [{"id": "call_1", "function": {"name": "get_weather", "arguments": '{"city":"Sao Paulo"}'}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(content=None, tool_calls=tool_calls))

    p = provider(handler, monkeypatch=monkeypatch)
    result = await p.complete(MESSAGES, max_tokens=50, tools=tools)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_weather"
    assert json.loads(result.tool_calls[0].arguments) == {"city": "Sao Paulo"}


async def test_d_an_undeclared_tool_name_is_rejected_not_trusted(monkeypatch):
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    tool_calls = [{"id": "call_1", "function": {"name": "delete_everything", "arguments": "{}"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(content=None, tool_calls=tool_calls))

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50, tools=tools)
    assert exc_info.value.code == "invalid_tool_call"


async def test_d_malformed_tool_arguments_are_rejected(monkeypatch):
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    tool_calls = [{"id": "call_1", "function": {"name": "get_weather", "arguments": "{not json"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(content=None, tool_calls=tool_calls))

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50, tools=tools)
    assert exc_info.value.code == "invalid_tool_call"


# ── E. Structured output alongside tools ──────────────────────────────────────

async def test_e_structured_output_request_format_is_forwarded(monkeypatch):
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True}}
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(_completion(content='{"a": 1}'))

    p = provider(handler, monkeypatch=monkeypatch)
    result = await p.complete(MESSAGES, max_tokens=50, response_format=schema)

    assert seen["body"]["response_format"] == schema
    assert json.loads(result.text) == {"a": 1}


async def test_e_structured_output_together_with_a_declared_tool(monkeypatch):
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True}}
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(_completion(content='{"a": 1}'))

    p = provider(handler, monkeypatch=monkeypatch)
    await p.complete(MESSAGES, max_tokens=50, tools=tools, response_format=schema)

    assert seen["body"]["response_format"] == schema
    assert seen["body"]["tools"] == tools
    assert seen["body"]["tool_choice"] == "auto"


# ── F. Controlled failure paths ───────────────────────────────────────────────

async def test_f_disabled_by_default_refuses_before_any_network_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _response(_completion(content="should never be reached"))

    p = GroqCandidateProvider("openai/gpt-oss-120b", transport=httpx.MockTransport(handler))
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "disabled"
    assert called["n"] == 0


async def test_f_missing_credential_is_a_controlled_failure(monkeypatch):
    from src.local_runtime.llm import local_runtime_settings
    monkeypatch.setattr(local_runtime_settings(), "groq_oss_120b_enabled", True)
    monkeypatch.setattr("src.config.settings.groq_api_key", "")

    p = GroqCandidateProvider("openai/gpt-oss-120b", transport=httpx.MockTransport(lambda r: _response({})))
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "not_configured"


async def test_f_quota_exhausted_refuses_before_sending_never_retries_past_it(monkeypatch):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _response(_completion(content="should never be reached"))

    p = provider(handler, monkeypatch=monkeypatch)
    p._guard.reserve = lambda estimated: (_ for _ in ()).throw(FreeTierExhausted("tpd"))

    with pytest.raises(FreeTierExhausted) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "quota_exhausted"
    assert exc_info.value.window == "tpd"
    assert called["n"] == 0, "a quota-exhausted call must never reach the network"


async def test_f_quality_gate_rejection_is_a_controlled_failure_not_a_relaxed_pass(monkeypatch):
    """The SAME production Quality Gate, unmodified: an empty response is
    rejected here exactly like it would be for any other provider."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(content="   "))

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "quality_gate:empty_response"


async def test_f_language_mismatch_is_caught_by_the_real_quality_gate(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(
            content="An index is a data structure that speeds up the lookup of rows in a table."))

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES_PT, max_tokens=50)
    assert exc_info.value.code == "quality_gate:language_mismatch"


async def test_f_a_tool_call_turn_is_never_judged_empty_by_the_quality_gate(monkeypatch):
    """A tool-call response legitimately has no prose — the Quality Gate's own
    carve-out (has_tool_calls=True) must apply here exactly as it does in
    router.py, not be bypassed or reimplemented more strictly."""
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    tool_calls = [{"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_completion(content=None, tool_calls=tool_calls))

    p = provider(handler, monkeypatch=monkeypatch)
    result = await p.complete(MESSAGES, max_tokens=50, tools=tools)
    assert result.tool_calls[0].name == "get_weather"


async def test_f_rate_limited_is_a_distinct_controlled_outcome(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _response({"error": {"message": "rate limit reached"}}, status=429)

    p = provider(handler, monkeypatch=monkeypatch)
    with pytest.raises(GroqCandidateError) as exc_info:
        await p.complete(MESSAGES, max_tokens=50)
    assert exc_info.value.code == "rate_limited"


# ── Free-tier guard, standalone ───────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0
        self.day = "2026-09-20"

    def now(self) -> float:
        return self.t

    def today(self) -> str:
        return self.day


def test_guard_reserve_refuses_past_the_safety_margin_not_the_raw_limit():
    clock = _Clock()
    guard = FreeTierGuard(rpm=10, rpd=100, tpm=1000, tpd=10_000, safety_margin=0.8,
                          now_fn=clock.now, today_fn=clock.today)
    # 8 calls of 1 token each = at the 80% RPM margin (10*0.8=8); the 9th must refuse.
    for _ in range(8):
        guard.reserve(1)
    with pytest.raises(FreeTierExhausted) as exc_info:
        guard.reserve(1)
    assert exc_info.value.window == "rpm"


def test_guard_tpm_window_rolls_off_after_60_seconds():
    clock = _Clock()
    guard = FreeTierGuard(rpm=1000, rpd=1000, tpm=100, tpd=100_000, safety_margin=1.0,
                          now_fn=clock.now, today_fn=clock.today)
    guard.reserve(90)
    with pytest.raises(FreeTierExhausted):
        guard.reserve(20)
    clock.t += 61
    guard.reserve(20)  # the old 90-token reservation has rolled off the 60s window


def test_guard_tpd_window_resets_on_a_new_utc_day():
    clock = _Clock()
    guard = FreeTierGuard(rpm=1000, rpd=1000, tpm=100_000, tpd=100, safety_margin=1.0,
                          now_fn=clock.now, today_fn=clock.today)
    guard.reserve(90)
    with pytest.raises(FreeTierExhausted):
        guard.reserve(20)
    clock.day = "2026-09-21"
    guard.reserve(20)  # yesterday's spend does not carry over


def test_guard_settle_corrects_the_estimate_without_double_counting():
    clock = _Clock()
    guard = FreeTierGuard(rpm=1000, rpd=1000, tpm=1000, tpd=100_000, safety_margin=1.0,
                          now_fn=clock.now, today_fn=clock.today)
    guard.reserve(500)          # optimistic estimate
    guard.settle(500, 200)      # the real call only used 200
    snap = guard.snapshot()
    assert snap["tokens_this_minute"] == 200


# ── No TCU / billing ───────────────────────────────────────────────────────────

def test_module_never_imports_entitlement_or_usage_billing():
    """Structural check: this candidate must never be able to charge TCU. Parses
    the AST rather than grepping the source text, so the module's own docstrings
    (which mention entitlement_service/UsageRecord BY NAME, in prose, to state
    this exact guarantee) can't produce a false positive."""
    import ast

    import src.local_runtime.groq_candidate as gc

    tree = ast.parse(open(gc.__file__, encoding="utf-8").read())
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
            if node.module:
                imported_names.add(node.module)
    assert not any("entitlement" in name for name in imported_names)
    assert not any("usage" in name.lower() for name in imported_names)
