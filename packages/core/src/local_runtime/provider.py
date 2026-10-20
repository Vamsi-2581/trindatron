"""llm.local — the local LLM provider. Experimental: off by default, never chosen by the Model Router.

It exists so a local model can be measured and, later, deliberately promoted — not so it
can quietly take traffic. Routing selects from trinity_router's PROVIDER_CATALOG only; the
local model is recorded in trinity_router.lab.LAB_CATALOG, which routing cannot reach. A
caller that wants this provider has to ask for it explicitly, and still gets refused while
LOCAL_LLM_ENABLED is false.

Limits enforced before anything is sent:
  - loopback runtime only (LocalLLMClient refuses any other host)
  - max_tokens capped at LOCAL_LLM_MAX_TOKENS
  - prompt + completion must fit LOCAL_LLM_CONTEXT_TOKENS (estimated conservatively)
  - one timeout per call
Errors carry a stable code only: no path, host, model file or runtime text reaches a caller.

Cost: no provider bill, but not free — CPU/RAM time is the price. `compute_seconds` is
returned so it can be priced like a GPU/CPU runner second (media/runners) when a tier exists.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.local_runtime.llm import LocalLLMClient, LocalRuntimeUnavailable, local_runtime_settings
from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.local_runtime")

CAPABILITY = "llm.local"
# Portuguese runs close to 3 characters per token on Qwen-family tokenizers; 3 over-estimates
# English. Over-estimating only refuses earlier, never lets an oversized prompt through.
_CHARS_PER_TOKEN = 3
_MESSAGE_OVERHEAD_TOKENS = 8

_requests = core_metrics.counter("local_llm_requests_total", "Local LLM calls, labeled by outcome.")
_latency_ms = core_metrics.histogram("local_llm_latency_ms", "Local LLM call latency in milliseconds.")
_tokens = core_metrics.counter("local_llm_tokens_total", "Local LLM tokens, labeled by direction.")

#: What the model is fit for, recorded where it is registered (see trinity_router.lab too).
QUALITY_NOTE = (
    "Qwen2.5-0.5B-Instruct Q4_K_M validates plumbing only. Measured 2026-09-18: follows short PT-BR prompts, "
    "fails trivial instructions (asked to translate a word, it paraphrased). Not for answers a person reads, "
    "reasoning, code, tool use, facts, or anything billed as quality."
)


class LocalLLMError(Exception):
    """A refused or failed local call. `code` is stable and safe to log or return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LocalLLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    tokens_per_second: float | None
    compute_seconds: float
    provider_cost_usd: float = 0.0  # nothing billed by a provider; compute is priced separately


def estimate_prompt_tokens(messages: list[dict]) -> int:
    chars = sum(len(str(message.get("content") or "")) for message in messages)
    return chars // _CHARS_PER_TOKEN + _MESSAGE_OVERHEAD_TOKENS * len(messages)


class LocalLLMProvider:
    capability = CAPABILITY

    def __init__(self, client: LocalLLMClient | None = None) -> None:
        self._client = client or LocalLLMClient()

    def enabled(self) -> bool:
        return local_runtime_settings().local_llm_enabled and self._client.configured()

    async def health(self) -> bool:
        return self.enabled() and await self._client.health()

    async def complete(self, messages: list[dict], *, max_tokens: int | None = None,
                       temperature: float = 0.2) -> LocalLLMResult:
        settings = local_runtime_settings()
        if not settings.local_llm_enabled:
            _requests.inc(outcome="disabled")
            raise LocalLLMError("disabled")
        if not self._client.configured():
            _requests.inc(outcome="not_configured")
            raise LocalLLMError("not_configured")
        if not messages or not all(isinstance(m, dict) and m.get("role") and "content" in m for m in messages):
            _requests.inc(outcome="invalid")
            raise LocalLLMError("invalid_request")
        limit = max(1, min(int(max_tokens or settings.local_llm_max_tokens), settings.local_llm_max_tokens))
        if estimate_prompt_tokens(messages) + limit > settings.local_llm_context_tokens:
            _requests.inc(outcome="context_exceeded")
            raise LocalLLMError("context_exceeded")
        started = time.monotonic()
        try:
            completion = await self._client.chat(messages, max_tokens=limit, temperature=temperature)
        except LocalRuntimeUnavailable as exc:
            _requests.inc(outcome="unavailable")
            logger.info("local llm: unavailable | %s", exc.reason)  # reasons carry no path or secret
            raise LocalLLMError("unavailable") from None
        latency_ms = (time.monotonic() - started) * 1000
        _requests.inc(outcome="completed")
        _latency_ms.observe(latency_ms, outcome="completed")
        _tokens.inc(completion.prompt_tokens, direction="input")
        _tokens.inc(completion.completion_tokens, direction="output")
        # The runtime's own model identifier is never passed on: llama-server may report a file path.
        return LocalLLMResult(text=completion.text, prompt_tokens=completion.prompt_tokens,
                              completion_tokens=completion.completion_tokens, latency_ms=latency_ms,
                              tokens_per_second=completion.tokens_per_second, compute_seconds=completion.seconds)
