"""Transcription through hosted open-weight Whisper: the request, the result, the price, and honest failure."""
from __future__ import annotations

import httpx
import pytest

from src.capability.multimodal import VoiceProviderNotConfigured, VoiceTranscriptionFailed
from src.voice.adapters.groq import GroqVoiceProvider
from src.voice.cost import calculate_groq_stt_cost_usd, calculate_voice_cost_usd


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transcribes_with_the_whisper_model_and_reports_duration():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], request.content
        return httpx.Response(200, json={"text": " Olá, tudo bem? ", "language": "portuguese", "duration": 3.4})

    result = await GroqVoiceProvider(api_key="gsk-test", http_client=_client(handler)).transcribe(
        b"webm-bytes", content_type="audio/webm")

    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer gsk-test"
    assert b"whisper-large-v3-turbo" in seen["body"] and b'filename="audio.webm"' in seen["body"]
    assert result.text == "Olá, tudo bem?" and result.duration_seconds == pytest.approx(3.4)


async def test_a_refusal_never_carries_provider_text_or_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "secret quota detail"}})

    with pytest.raises(VoiceTranscriptionFailed) as caught:
        await GroqVoiceProvider(api_key="gsk-test", http_client=_client(handler)).transcribe(b"x")
    assert "secret" not in str(caught.value) and "gsk-test" not in str(caught.value)


async def test_without_a_key_it_is_unconfigured_and_it_never_synthesizes():
    provider = GroqVoiceProvider(api_key="")
    with pytest.raises(VoiceProviderNotConfigured):
        await provider.transcribe(b"x")
    with pytest.raises(VoiceProviderNotConfigured):
        await GroqVoiceProvider(api_key="gsk").synthesize("olá")


def test_price_is_per_hour_with_a_ten_second_minimum():
    # Costs are recorded to 8 decimal places.
    assert calculate_groq_stt_cost_usd(3.0) == pytest.approx(10 / 3600 * 0.04, abs=1e-8)
    assert calculate_groq_stt_cost_usd(90.0) == pytest.approx(90 / 3600 * 0.04, abs=1e-8)
    assert calculate_voice_cost_usd(provider_name="groq", operation="stt", duration_seconds=90.0) == pytest.approx(0.001, abs=1e-4)
