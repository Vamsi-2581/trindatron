"""llm.local and stt.local — MockTransport only, no runtime needed, nothing leaves the machine."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from src.capability.multimodal import VoiceProviderNotConfigured, VoiceTranscriptionFailed
from src.local_runtime.llm import LocalLLMClient, LocalRuntimeUnavailable, local_runtime_settings, loopback_base_url
from src.local_runtime.provider import LocalLLMError, LocalLLMProvider, estimate_prompt_tokens
from src.voice.adapters import whisper_local
from src.voice.adapters.whisper_local import WhisperLocalVoiceProvider, build_whisper_local_provider

CORE = Path(__file__).resolve().parents[2]
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 80
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 80
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 80


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # Set, never deleted: an environment value wins over .env, so a real local config is never read here.
    for name, value in (("LOCAL_LLM_ENABLED", "false"), ("LOCAL_LLM_BASE_URL", ""), ("LOCAL_LLM_MODEL", ""),
                        ("STT_LOCAL_ENABLED", "false"), ("WHISPER_LOCAL_BASE_URL", ""),
                        ("LOCAL_LLM_MAX_TOKENS", "512"), ("LOCAL_LLM_CONTEXT_TOKENS", "4096")):
        monkeypatch.setenv(name, value)
    local_runtime_settings.cache_clear()
    whisper_local._slots = None
    yield
    local_runtime_settings.cache_clear()
    whisper_local._slots = None


def _enable_llm(monkeypatch, **extra):
    monkeypatch.setenv("LOCAL_LLM_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8181/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen")
    for key, value in extra.items():
        monkeypatch.setenv(key, str(value))
    local_runtime_settings.cache_clear()


# ── Loopback only ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://127.0.0.1:8181/v1", "http://localhost:11434/v1", "http://[::1]:8181/v1/"])
def test_loopback_urls_are_accepted(url):
    assert loopback_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["", "https://127.0.0.1:8181/v1", "http://192.168.0.14:8181/v1", "http://example.com/v1",
                                 "http://user:pw@127.0.0.1:8181/v1", "http://10.0.0.5:11434", "ftp://127.0.0.1/"])
def test_anything_off_this_machine_is_refused(url):
    with pytest.raises(LocalRuntimeUnavailable):
        loopback_base_url(url)


# ── llm.local ────────────────────────────────────────────────────────────────

def _runtime(status: int = 200, *, models=("qwen",), content: str = "Olá!", model_name="C:\\secret\\path\\m.gguf"):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "body": json.loads(request.content) if request.content else None})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        if status != 200:
            return httpx.Response(status, text="C:\\TRINITY-LAB\\models\\boom at /internal/path")
        return httpx.Response(200, json={"model": model_name, "choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                                         "timings": {"predicted_per_second": 55.5}})

    return httpx.MockTransport(handler), seen


def _provider(transport) -> LocalLLMProvider:
    return LocalLLMProvider(LocalLLMClient(transport=transport))


async def test_llm_local_is_off_by_default_and_calls_nothing():
    transport, seen = _runtime()
    provider = _provider(transport)
    assert provider.enabled() is False and await provider.health() is False
    with pytest.raises(LocalLLMError) as refused:
        await provider.complete([{"role": "user", "content": "oi"}])
    assert refused.value.code == "disabled" and seen == []


async def test_llm_local_completes_with_metrics_and_no_key(monkeypatch):
    _enable_llm(monkeypatch)
    transport, seen = _runtime()
    result = await _provider(transport).complete([{"role": "user", "content": "oi"}], max_tokens=16)
    assert result.text == "Olá!" and (result.prompt_tokens, result.completion_tokens) == (12, 3)
    assert result.tokens_per_second == pytest.approx(55.5) and result.latency_ms >= 0 and result.provider_cost_usd == 0
    assert seen[-1]["auth"] is None and seen[-1]["body"]["max_tokens"] == 16
    assert "gguf" not in repr(result) and "secret" not in repr(result)  # the runtime's model path is never passed on


async def test_max_tokens_are_capped(monkeypatch):
    _enable_llm(monkeypatch, LOCAL_LLM_MAX_TOKENS=64)
    transport, seen = _runtime()
    await _provider(transport).complete([{"role": "user", "content": "oi"}], max_tokens=10_000)
    assert seen[-1]["body"]["max_tokens"] == 64


async def test_a_prompt_over_the_context_is_refused_before_sending(monkeypatch):
    _enable_llm(monkeypatch, LOCAL_LLM_CONTEXT_TOKENS=1000, LOCAL_LLM_MAX_TOKENS=256)
    transport, seen = _runtime()
    with pytest.raises(LocalLLMError) as refused:
        await _provider(transport).complete([{"role": "user", "content": "palavra " * 800}])
    assert refused.value.code == "context_exceeded" and seen == []
    assert estimate_prompt_tokens([{"role": "user", "content": "x" * 300}]) == 108


async def test_runtime_failures_are_a_clean_code_without_paths(monkeypatch):
    _enable_llm(monkeypatch)
    transport, _ = _runtime(status=500)
    with pytest.raises(LocalLLMError) as failed:
        await _provider(transport).complete([{"role": "user", "content": "x"}])
    assert failed.value.code == "unavailable" and str(failed.value) == "unavailable"
    assert failed.value.__cause__ is None


@pytest.mark.parametrize("messages", [[], [{"content": "x"}], ["texto"], [{"role": "user"}]])
async def test_malformed_messages_are_refused(monkeypatch, messages):
    _enable_llm(monkeypatch)
    transport, seen = _runtime()
    with pytest.raises(LocalLLMError) as refused:
        await _provider(transport).complete(messages)
    assert refused.value.code == "invalid_request" and seen == []


async def test_a_remote_base_url_is_never_configured(monkeypatch):
    _enable_llm(monkeypatch, LOCAL_LLM_BASE_URL="http://api.example.com/v1")
    transport, seen = _runtime()
    with pytest.raises(LocalLLMError) as refused:
        await _provider(transport).complete([{"role": "user", "content": "x"}])
    assert refused.value.code == "not_configured" and seen == []


async def test_health_needs_the_configured_model_when_the_runtime_serves_several():
    transport, _ = _runtime(models=("a", "b"))
    assert await LocalLLMClient("http://127.0.0.1:11434/v1", "qwen", transport=transport).health() is False
    transport, _ = _runtime(models=("qwen:latest", "b"))
    assert await LocalLLMClient("http://127.0.0.1:11434/v1", "qwen", transport=transport).health() is True


# ── stt.local ────────────────────────────────────────────────────────────────

def _whisper(payload, status=200):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.content
        return httpx.Response(status, json=payload) if isinstance(payload, dict) else httpx.Response(status, text=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _stt(client=None, **kwargs) -> WhisperLocalVoiceProvider:
    return WhisperLocalVoiceProvider(base_url="http://127.0.0.1:8282", http_client=client, **kwargs)


async def test_stt_local_transcribes_through_the_existing_voice_contract():
    client, seen = _whisper({"language": "portuguese", "duration": 30.0, "text": " primeira linha\n segunda linha "})
    result = await _stt(client).transcribe(WEBM, content_type="audio/webm", language="pt-BR")
    assert result.text == "primeira linha segunda linha" and result.language == "portuguese"
    assert result.duration_seconds == 30.0
    assert seen["url"] == "http://127.0.0.1:8282/inference"
    assert b"verbose_json" in seen["body"] and b"audio.webm" in seen["body"] and b'name="language"\r\n\r\npt' in seen["body"]


@pytest.mark.parametrize("audio, ctype", [(WAV, "audio/wav"), (M4A, "audio/mp4"), (WEBM, "audio/ogg"),
                                          (b"OggS" + b"\x00" * 80, "audio/webm")])
async def test_supported_formats_by_label_and_by_signature(audio, ctype):
    client, _ = _whisper({"text": "ok"})
    assert (await _stt(client).transcribe(audio, content_type=ctype)).text == "ok"


@pytest.mark.parametrize("audio, ctype", [
    (b"%PDF-1.7" + b"\x00" * 80, "audio/webm"),       # not audio, whatever the label
    (WAV, "audio/webm"),                              # label and bytes disagree
    (WAV, "application/octet-stream"),                # unsupported label
    (b"\x00" * 10, "audio/wav"),                      # too short
    (b"#!/bin/sh\nrm -rf /" + b"\x00" * 80, "audio/wav"),
])
async def test_anything_else_is_refused_before_sending(audio, ctype):
    client, seen = _whisper({"text": "x"})
    with pytest.raises(VoiceTranscriptionFailed):
        await _stt(client).transcribe(audio, content_type=ctype)
    assert seen == {}


async def test_audio_over_the_size_limit_is_refused_before_sending():
    client, seen = _whisper({"text": "x"})
    with pytest.raises(VoiceTranscriptionFailed, match="too large"):
        await _stt(client, max_bytes=100).transcribe(WAV + b"\x00" * 200, content_type="audio/wav")
    assert seen == {}


@pytest.mark.parametrize("language", ["pt; rm -rf /", "../../etc", "portuguese-long", "PT_br", "1"])
async def test_language_codes_are_validated(language):
    client, seen = _whisper({"text": "x"})
    with pytest.raises(VoiceTranscriptionFailed, match="language"):
        await _stt(client).transcribe(WAV, content_type="audio/wav", language=language)
    assert seen == {}


async def test_one_job_at_a_time_and_a_busy_error_when_waiting_too_long():
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json={"text": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    first = asyncio.create_task(_stt(client).transcribe(WAV, content_type="audio/wav"))
    await asyncio.sleep(0.01)
    with pytest.raises(VoiceTranscriptionFailed, match="busy"):
        await _stt(client, timeout_s=0.05).transcribe(WAV, content_type="audio/wav")
    release.set()
    assert (await first).text == "ok"


async def test_stt_local_failures_are_fixed_messages():
    with pytest.raises(VoiceProviderNotConfigured):
        await WhisperLocalVoiceProvider(base_url="http://192.168.0.20:8282").transcribe(WAV, content_type="audio/wav")
    for payload, status in (({"error": "C:\\models\\x.bin missing"}, 200), ({"text": "x"}, 500), ("not json", 200)):
        client, _ = _whisper(payload, status)
        with pytest.raises(VoiceTranscriptionFailed) as failed:
            await _stt(client).transcribe(WAV, content_type="audio/wav")
        assert "models" not in str(failed.value) and "127.0.0.1" not in str(failed.value)
    with pytest.raises(VoiceProviderNotConfigured):
        await _stt().synthesize("olá")


def test_stt_local_is_built_only_when_enabled_and_loopback(monkeypatch):
    assert build_whisper_local_provider() is None  # default
    monkeypatch.setenv("STT_LOCAL_ENABLED", "true")
    monkeypatch.setenv("WHISPER_LOCAL_BASE_URL", "http://10.0.0.9:8282")
    local_runtime_settings.cache_clear()
    assert build_whisper_local_provider() is None  # not loopback
    monkeypatch.setenv("WHISPER_LOCAL_BASE_URL", "http://127.0.0.1:8282")
    local_runtime_settings.cache_clear()
    assert build_whisper_local_provider().provider_name == "whisper_local"


def test_stt_local_is_the_last_automatic_fallback_and_always_costs_zero():
    """CP18: whisper_local joined STT_CAPABLE as the last resort — never preferred, always registered-gated."""
    from src.api.routes.voice import STT_CAPABLE
    from src.voice.budget import stt_cost_bound_usd
    from src.voice.cost import calculate_voice_cost_usd

    assert STT_CAPABLE == ("elevenlabs", "groq", "whisper_local")
    assert stt_cost_bound_usd("whisper_local", 1_000_000) == 0.0
    assert calculate_voice_cost_usd(provider_name="whisper_local", operation="stt", duration_seconds=3600.0) == 0.0


def test_nothing_reaches_the_model_router():
    router_sources = "".join(p.read_text(encoding="utf-8") for p in (CORE / "src").rglob("*.py")
                             if "trinity_router" in p.parts or p.name in ("model_router.py", "router.py"))
    assert "local_runtime" not in router_sources and "LocalLLM" not in router_sources
    main = (CORE / "src" / "main.py").read_text(encoding="utf-8")
    assert "LocalLLMProvider" not in main and "WebResearchCapability" not in main


async def test_all_local_capabilities_are_off_by_default(monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_ENABLED", "false")
    from src.capability.web_research.service import web_research_settings
    from src.local_runtime.capabilities import local_capabilities

    web_research_settings.cache_clear()
    try:
        rows = await local_capabilities(probe=True)
    finally:
        web_research_settings.cache_clear()
    assert [(r.name, r.enabled) for r in rows] == [
        ("llm.local", False), ("stt.local", False), ("web_research", False), ("groq.oss_120b", False),
    ]
    assert all(r.healthy is None for r in rows)
    assert all(not any(word in repr(r).lower() for word in ("llama", "whisper", "crawl4ai", "qwen")) for r in rows)
