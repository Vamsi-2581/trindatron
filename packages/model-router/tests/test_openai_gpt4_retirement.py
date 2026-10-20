"""OpenAI retires gpt-4o, gpt-4o-mini, gpt-4.1 and gpt-4.1-mini on 2027-01-14.

The catalog moved off them on 2026-10-20 while keeping the two alias names
(they are stored in kill switches and runtime settings). These tests pin what
that move has to keep true: the aliases point at the GPT-6 replacements, the
retired ids stay out of the catalog, and the per-model call parameter that
keeps gpt-6-luna fast actually reaches litellm on both call paths. No model
is called.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trinity_router import ModelRouter, RoutingRequest, TaskType
from trinity_router.providers import PROVIDER_CATALOG, RETIRED_MODEL_IDS


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _make_response():
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    usage = MagicMock(); usage.prompt_tokens = 10; usage.completion_tokens = 5
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


def _request(**overrides) -> RoutingRequest:
    defaults = dict(messages=[{"role": "user", "content": "Hi"}],
                    task_type=TaskType.CHAT, user_plan="pro", session_id="s1")
    defaults.update(overrides)
    return RoutingRequest(**defaults)


def test_openai_aliases_point_at_the_gpt6_replacements():
    assert PROVIDER_CATALOG["gpt-4o-mini"].model_id == "gpt-6-luna"
    assert PROVIDER_CATALOG["gpt-4o"].model_id == "gpt-6.1-sol"


def test_every_retiring_openai_id_is_on_the_retired_list():
    for model_id in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"):
        assert model_id in RETIRED_MODEL_IDS, model_id


def test_luna_is_called_with_reasoning_effort_none():
    assert dict(PROVIDER_CATALOG["gpt-4o-mini"].call_params) == {"reasoning_effort": "none"}


def test_no_other_model_carries_call_params():
    """call_params is an exception, not a habit: a stray one would be sent
    to a provider that may reject it."""
    others = {k for k, m in PROVIDER_CATALOG.items() if m.call_params and k != "gpt-4o-mini"}
    assert not others, others


async def test_complete_passes_call_params_to_litellm(monkeypatch):
    seen = []

    async def track(*args, **kwargs):
        seen.append(kwargs)
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    await ModelRouter(sleep_fn=_no_delay_sleep).complete(_request(forced_provider_key="gpt-4o-mini"))
    assert seen[0]["model"] == "gpt-6-luna"
    assert seen[0]["reasoning_effort"] == "none"


async def test_models_without_call_params_get_no_extra_kwargs(monkeypatch):
    seen = []

    async def track(*args, **kwargs):
        seen.append(kwargs)
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    await ModelRouter(sleep_fn=_no_delay_sleep).complete(_request(forced_provider_key="gpt-4o"))
    assert seen[0]["model"] == "gpt-6.1-sol"
    assert "reasoning_effort" not in seen[0]


async def test_stream_passes_call_params_to_litellm(monkeypatch):
    seen = []

    async def track(*args, **kwargs):
        seen.append(kwargs)

        async def gen():
            if False:
                yield None
        return gen()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)
    try:
        async for _ in router.stream(_request(forced_provider_key="gpt-4o-mini")):
            pass
    except Exception:
        pass  # an empty stream may be treated as a failure; only the call matters here
    assert seen, "stream() never reached litellm"
    assert seen[0]["model"] == "gpt-6-luna"
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[0]["stream"] is True
