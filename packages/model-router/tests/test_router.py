"""Tests for ModelRouter.complete() and ModelRouter.stream()."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    RoutingRequest,
    RoutingResult,
    RouterStreamChunk,
    TaskType,
)
from trinity_router.tcu import TCU_EXCHANGE_RATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(content="Answer", input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


def _make_stream_chunks(text="Hello world"):
    """Yield fake litellm stream chunks then a usage chunk."""
    chunks = []
    for word in text.split():
        c = MagicMock()
        delta = MagicMock(); delta.content = word + " "
        c.choices = [MagicMock(delta=delta)]
        c.usage = None
        chunks.append(c)
    # Final chunk with usage
    final = MagicMock()
    final.choices = [MagicMock(delta=MagicMock(content=None))]
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.prompt_tokens_details = None
    final.usage = usage
    chunks.append(final)
    return chunks


# ---------------------------------------------------------------------------
# complete() — success path
# ---------------------------------------------------------------------------

class TestRouterCompleteSuccess:
    async def test_returns_routing_result(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(return_value=_make_response()))
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        assert isinstance(result, RoutingResult)
        assert result.content == "Answer"

    async def test_provider_is_server_assigned(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(return_value=_make_response()))
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        # Provider must be a known server-controlled value
        assert result.provider in ("anthropic", "openai", "google", "groq")
        assert result.model != ""

    async def test_tcu_is_server_calculated(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(return_value=_make_response(input_tokens=1000, output_tokens=500)))
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        # TCU must be proportional to cost
        assert result.tcu == pytest.approx(result.cost_usd * TCU_EXCHANGE_RATE, rel=1e-3)

    async def test_cost_usd_is_positive_for_nonzero_tokens(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(return_value=_make_response(input_tokens=100, output_tokens=50)))
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        assert result.cost_usd > 0
        assert result.tcu > 0

    async def test_failover_not_used_on_first_success(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(return_value=_make_response()))
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        assert result.failover_used is False
        assert result.failover_reason is None


# ---------------------------------------------------------------------------
# complete() — failover
# ---------------------------------------------------------------------------

class TestRouterFailover:
    async def test_failover_to_second_provider(self, monkeypatch):
        call_count = 0
        ok_response = _make_response(content="Fallback answer")

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("connection timeout")
            return ok_response

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",  # max_failover_attempts=1 → 2 providers allowed
            session_id="s1",
        ))
        assert result.content == "Fallback answer"
        assert result.failover_used is True
        assert result.failover_reason is not None
        assert call_count == 2

    async def test_failover_reason_is_sanitized(self, monkeypatch):
        call_count = 0
        ok_response = _make_response()

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate an error message that contains a fake API key
                raise Exception("AuthenticationError: sk-ant-secret-key-12345 is invalid")
            return ok_response

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        # Sanitized reason must NEVER contain the raw exception message
        if result.failover_reason:
            assert "sk-ant" not in result.failover_reason
            assert "secret-key" not in result.failover_reason
            assert "12345" not in result.failover_reason

    async def test_all_providers_fail_raises(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(side_effect=Exception("all down")))
        router = ModelRouter()
        with pytest.raises(ProviderUnavailableError) as exc_info:
            await router.complete(RoutingRequest(
                messages=[{"role": "user", "content": "Hi"}],
                task_type=TaskType.CHAT,
                user_plan="start",
                session_id="s1",
            ))
        assert len(exc_info.value.providers_tried) >= 1

    async def test_failover_bounded_by_plan(self, monkeypatch):
        """Failover must not exceed plan.max_failover_attempts + 1 providers."""
        call_count = 0

        async def always_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("down")

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fail)
        router = ModelRouter()
        from trinity_router.limits import get_plan_limits
        plan_limits = get_plan_limits("start")
        max_expected_calls = plan_limits.max_failover_attempts + 1

        with pytest.raises(ProviderUnavailableError):
            await router.complete(RoutingRequest(
                messages=[{"role": "user", "content": "Hi"}],
                task_type=TaskType.CHAT,
                user_plan="start",
                session_id="s1",
            ))
        assert call_count <= max_expected_calls

    async def test_provider_unavailable_error_lists_providers(self, monkeypatch):
        monkeypatch.setattr("trinity_router.router.litellm.acompletion",
                            AsyncMock(side_effect=Exception("down")))
        router = ModelRouter()
        with pytest.raises(ProviderUnavailableError) as exc_info:
            await router.complete(RoutingRequest(
                messages=[{"role": "user", "content": "Hi"}],
                task_type=TaskType.CHAT,
                user_plan="start",
                session_id="s1",
            ))
        # Error message names providers but not API keys
        err_msg = str(exc_info.value)
        assert "sk-" not in err_msg.lower()
        assert len(exc_info.value.providers_tried) > 0


# ---------------------------------------------------------------------------
# stream() — success path
# ---------------------------------------------------------------------------

class _FakeStreamWithUsage:
    """Mimics litellm CustomStreamWrapper: async iterable + .usage after loop."""
    def __init__(self, chunks, final_usage=None):
        self._chunks = chunks
        self.usage = final_usage  # populated as litellm's internal accumulator

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for c in self._chunks:
            yield c


class TestRouterStream:
    async def test_yields_text_chunks_then_final(self, monkeypatch):
        chunks = _make_stream_chunks("Hello world")

        async def fake_acompletion(*args, **kwargs):
            return _FakeStreamWithUsage(chunks)

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_acompletion)
        router = ModelRouter()

        results = []
        async for chunk in router.stream(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s2",
        )):
            results.append(chunk)

        text_chunks = [c for c in results if not c.is_final]
        final_chunks = [c for c in results if c.is_final]

        assert len(text_chunks) >= 1
        assert len(final_chunks) == 1
        final = final_chunks[0]
        assert final.provider in ("anthropic", "openai", "google", "groq")
        # Token counts must be non-zero when the mock provides real usage
        assert final.input_tokens == 100
        assert final.output_tokens == 50
        assert final.cost_usd > 0
        # abs tolerance, not just rel: tcu and cost_usd are each independently
        # rounded (calculate_tcu rounds to 4 decimals) — a tight rel-only
        # tolerance can fail right at a rounding boundary even though both
        # values are correct (e.g. cost_usd*rate landing exactly on .00225,
        # which float rounding pushes to 0.0023 while a fresh, unrounded
        # recomputation here gives 0.00225 — a ~2% relative gap that is
        # entirely a rounding artifact, not a real mismatch).
        assert final.tcu == pytest.approx(final.cost_usd * TCU_EXCHANGE_RATE, rel=1e-3, abs=1e-4)

    async def test_stream_tokens_from_split_events(self, monkeypatch):
        """max() accumulation: input_tokens in one chunk, output_tokens in another.

        Anthropic sends message_start (prompt_tokens=N, completion_tokens=0) and
        message_delta (prompt_tokens=0, completion_tokens=M) as separate stream events.
        Assignment would overwrite N with 0; max() preserves both.
        """
        chunk_in = MagicMock()
        chunk_in.choices = [MagicMock(delta=MagicMock(content=None))]
        chunk_in.usage = MagicMock(prompt_tokens=80, completion_tokens=0,
                                    prompt_tokens_details=None)

        chunk_text = MagicMock()
        chunk_text.choices = [MagicMock(delta=MagicMock(content="Hello "))]
        chunk_text.usage = None

        chunk_out = MagicMock()
        chunk_out.choices = [MagicMock(delta=MagicMock(content=None))]
        chunk_out.usage = MagicMock(prompt_tokens=0, completion_tokens=30,
                                     prompt_tokens_details=None)

        async def fake_acompletion(*args, **kwargs):
            return _FakeStreamWithUsage([chunk_in, chunk_text, chunk_out])

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_acompletion)
        router = ModelRouter()

        results = []
        async for c in router.stream(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s_split",
        )):
            results.append(c)

        final = next(c for c in results if c.is_final)
        assert final.input_tokens == 80
        assert final.output_tokens == 30
        assert final.cost_usd > 0
        assert final.tcu > 0

    async def test_stream_tokens_from_response_usage_fallback(self, monkeypatch):
        """When all chunks have usage=None, tokens come from response.usage after loop.

        litellm's CustomStreamWrapper accumulates usage internally even when individual
        chunks don't carry it. This is the primary path for Anthropic streaming in
        litellm >= 1.50 without stream_options={'include_usage': True}.
        """
        chunk_text = MagicMock()
        chunk_text.choices = [MagicMock(delta=MagicMock(content="Hi "))]
        chunk_text.usage = None

        chunk_final = MagicMock()
        chunk_final.choices = [MagicMock(delta=MagicMock(content=None))]
        chunk_final.usage = None

        wrapper_usage = MagicMock()
        wrapper_usage.prompt_tokens = 60
        wrapper_usage.completion_tokens = 20
        wrapper_usage.prompt_tokens_details = None

        async def fake_acompletion(*args, **kwargs):
            return _FakeStreamWithUsage([chunk_text, chunk_final],
                                        final_usage=wrapper_usage)

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_acompletion)
        router = ModelRouter()

        results = []
        async for c in router.stream(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s_fallback",
        )):
            results.append(c)

        final = next(c for c in results if c.is_final)
        assert final.input_tokens == 60
        assert final.output_tokens == 20
        assert final.cost_usd > 0
        assert final.tcu > 0


# ---------------------------------------------------------------------------
# Budget limit enforcement
# ---------------------------------------------------------------------------

class TestRouterBudgetEnforcement:
    async def test_token_count_silently_capped(self, monkeypatch):
        """Requesting more tokens than plan allows must be silently reduced, not an error."""
        captured_kwargs = {}

        async def capture(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_response()

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", capture)
        from trinity_router.limits import get_plan_limits
        plan_limits = get_plan_limits("start")

        router = ModelRouter()
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "Hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
            max_output_tokens=999_999,  # Way over the plan limit
        ))
        called_max_tokens = captured_kwargs.get("max_tokens", 0)
        assert called_max_tokens <= plan_limits.max_output_tokens_per_request
