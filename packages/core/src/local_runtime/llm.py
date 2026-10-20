"""LocalLLMClient: chat completions against a local llama.cpp server or Ollama. Loopback only.

    LOCAL_LLM_BASE_URL=http://127.0.0.1:8181/v1   (llama-server)   | empty = off
    LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1  (ollama serve)
    LOCAL_LLM_MODEL=<name the runtime serves>

Open weights are not free to run: `usage` and `timings` are returned so a benchmark can
price CPU/GPU seconds the same way runners are priced (media/runners). No API key is
sent or needed; nothing here leaves the machine.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # llm.local — experimental; off unless enabled AND pointed at a loopback runtime.
    local_llm_enabled: bool = False
    local_llm_base_url: str = ""
    local_llm_model: str = ""
    local_llm_timeout_s: float = 60.0
    local_llm_max_tokens: int = 512        # ceiling on any single completion
    local_llm_context_tokens: int = 4096   # the context the runtime was started with (-c)

    # stt.local — experimental; registered with the voice registry only when enabled.
    stt_local_enabled: bool = False
    whisper_local_base_url: str = ""
    whisper_local_timeout_s: float = 120.0
    whisper_local_max_bytes: int = 10 * 1024 * 1024
    whisper_local_max_concurrency: int = 1  # one model in RAM, one job at a time

    # groq.oss_120b candidate (CP12) — FREE CORE CANDIDATE, off by default; the Model
    # Router cannot reach it (trinity_router.lab.LAB_CATALOG only). Reuses the same
    # GROQ_API_KEY the ACTIVE groq-llama4-scout catalog entry already has configured —
    # no new credential. See src/local_runtime/groq_candidate.py for the free-tier
    # guard and the tool-call fix this flag gates.
    groq_oss_120b_enabled: bool = False


@lru_cache
def local_runtime_settings() -> LocalRuntimeSettings:
    return LocalRuntimeSettings()


class LocalRuntimeUnavailable(Exception):
    """Not configured, not loopback, not running, or it failed. `reason` is for logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def loopback_base_url(url: str) -> str:
    """`url` without a trailing slash, if it points at this machine over http. Raises LocalRuntimeUnavailable."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "http" or not parts.hostname or parts.username or parts.password:
        raise LocalRuntimeUnavailable("base url must be http://<loopback>:<port>")
    host = parts.hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise LocalRuntimeUnavailable("base url is not loopback")
    return url.strip().rstrip("/")


@dataclass(frozen=True)
class LocalCompletion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    tokens_per_second: float | None


class LocalLLMClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, *,
                 timeout_s: float | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = local_runtime_settings()
        self._raw_base = base_url if base_url is not None else settings.local_llm_base_url
        self._model = model if model is not None else settings.local_llm_model
        self._timeout = timeout_s or settings.local_llm_timeout_s
        self._transport = transport

    def configured(self) -> bool:
        try:
            loopback_base_url(self._raw_base)
        except LocalRuntimeUnavailable:
            return False
        return bool(self._model)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout, trust_env=False)

    async def health(self) -> bool:
        """The runtime answers and serves `model`. Never raises."""
        if not self.configured():
            return False
        base = loopback_base_url(self._raw_base)
        try:
            async with self._client() as client:
                response = await client.get(f"{base}/models")
            if response.status_code != 200:
                return False
            data = response.json().get("data") or []
            return any(isinstance(item, dict) and item.get("id") in (self._model, f"{self._model}:latest")
                       for item in data) or (len(data) == 1)  # llama-server serves exactly one model
        except Exception:
            return False

    async def chat(self, messages: list[dict], *, max_tokens: int = 256, temperature: float = 0.2) -> LocalCompletion:
        if not self._model:
            raise LocalRuntimeUnavailable("no model configured")
        base = loopback_base_url(self._raw_base)
        body = {"model": self._model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                "stream": False}
        started = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.post(f"{base}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LocalRuntimeUnavailable(f"runtime unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise LocalRuntimeUnavailable(f"runtime returned HTTP {response.status_code}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LocalRuntimeUnavailable("runtime returned an unexpected body") from exc
        usage = data.get("usage") or {}
        timings = data.get("timings") or {}
        seconds = time.monotonic() - started
        completion = int(usage.get("completion_tokens") or 0)
        speed = timings.get("predicted_per_second") or (completion / seconds if seconds > 0 and completion else None)
        return LocalCompletion(text=text.strip(), model=str(data.get("model") or self._model),
                               prompt_tokens=int(usage.get("prompt_tokens") or 0), completion_tokens=completion,
                               seconds=seconds, tokens_per_second=float(speed) if speed else None)
