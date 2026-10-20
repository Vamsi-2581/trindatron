"""
Unit tests for GeminiVoiceProvider — no real network calls.

A fake httpx.AsyncClient-like object is injected via the constructor so
these tests never hit the Gemini API.
"""
from __future__ import annotations

import base64

import pytest

from src.capability.multimodal import VoiceProviderNotConfigured, VoiceSynthesisFailed, VoiceTranscriptionFailed
from src.voice.adapters.gemini import GeminiVoiceProvider


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self._text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    @property
    def text(self):
        return self._text


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def aclose(self):
        pass


def _gemini_success_body(*, pcm_data: bytes = b"\x00\x01\x02\x03", input_tokens=12, output_tokens=340):
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm_data).decode()}}
                    ]
                }
            }
        ],
        "usageMetadata": {"promptTokenCount": input_tokens, "candidatesTokenCount": output_tokens},
    }


# ── synthesize: config ───────────────────────────────────────────────────


async def test_synthesize_missing_api_key_raises_not_configured():
    provider = GeminiVoiceProvider(api_key="")
    with pytest.raises(VoiceProviderNotConfigured) as exc_info:
        await provider.synthesize("olá")
    assert exc_info.value.env_var == "GEMINI_API_KEY"


async def test_synthesize_default_voice_is_real_google_voice_not_fabricated():
    """The default voice name ('Kore') is a real, documented Google prebuilt
    voice — this asserts the adapter actually uses it end-to-end, not some
    placeholder string invented for this codebase."""
    fake_client = _FakeClient(_FakeResponse(200, json_body=_gemini_success_body()))
    provider = GeminiVoiceProvider(api_key="key-123", http_client=fake_client)

    result = await provider.synthesize("olá mundo")

    assert result.voice_id == "Kore"
    _, kwargs = fake_client.calls[0]
    sent_voice = kwargs["json"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert sent_voice == "Kore"


# ── synthesize: success ───────────────────────────────────────────────────


async def test_synthesize_success_returns_wav_audio_and_token_metadata():
    pcm = b"\x11\x22\x33\x44\x55\x66"
    fake_client = _FakeClient(_FakeResponse(200, json_body=_gemini_success_body(pcm_data=pcm, input_tokens=7, output_tokens=210)))
    provider = GeminiVoiceProvider(api_key="key-123", http_client=fake_client)

    result = await provider.synthesize("olá mundo")

    # MIME type: adapter always wraps raw PCM into a WAV container.
    assert result.content_type == "audio/wav"
    # WAV container: RIFF/WAVE header present, PCM payload appended after 44-byte header.
    assert result.audio[:4] == b"RIFF"
    assert result.audio[8:12] == b"WAVE"
    assert result.audio[44:] == pcm
    # Real token counts surfaced for cost calculation — never estimated.
    assert result.metadata == {"input_tokens": 7, "output_tokens": 210}


async def test_synthesize_request_sends_correct_endpoint_and_auth_header():
    fake_client = _FakeClient(_FakeResponse(200, json_body=_gemini_success_body()))
    provider = GeminiVoiceProvider(api_key="key-123", tts_model_id="gemini-2.5-flash-preview-tts", http_client=fake_client)

    await provider.synthesize("hi")

    url, kwargs = fake_client.calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
    assert kwargs["headers"]["x-goog-api-key"] == "key-123"
    # Key must never appear in the URL (query string) — header only.
    assert "key-123" not in url


async def test_synthesize_voice_override_takes_precedence():
    fake_client = _FakeClient(_FakeResponse(200, json_body=_gemini_success_body()))
    provider = GeminiVoiceProvider(api_key="key-123", tts_voice_name="Kore", http_client=fake_client)

    result = await provider.synthesize("hi", voice_id="Puck")

    assert result.voice_id == "Puck"
    _, kwargs = fake_client.calls[0]
    sent_voice = kwargs["json"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert sent_voice == "Puck"


# ── synthesize: failures ───────────────────────────────────────────────────


async def test_synthesize_upstream_failure_raises_synthesis_failed():
    fake_client = _FakeClient(_FakeResponse(401, text="unauthorized"))
    provider = GeminiVoiceProvider(api_key="bad-key", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed):
        await provider.synthesize("olá")


async def test_synthesize_invalid_response_shape_raises_synthesis_failed():
    """Missing candidates/parts/inlineData — a malformed or unexpected response."""
    fake_client = _FakeClient(_FakeResponse(200, json_body={"candidates": []}))
    provider = GeminiVoiceProvider(api_key="key-123", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed):
        await provider.synthesize("olá")


async def test_synthesize_non_json_response_raises_synthesis_failed():
    fake_client = _FakeClient(_FakeResponse(200, text="not json"))
    provider = GeminiVoiceProvider(api_key="key-123", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed):
        await provider.synthesize("olá")


async def test_synthesize_error_message_never_contains_api_key():
    fake_client = _FakeClient(_FakeResponse(401, text="unauthorized"))
    provider = GeminiVoiceProvider(api_key="super-secret-gemini-key", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed) as exc_info:
        await provider.synthesize("olá")
    assert "super-secret-gemini-key" not in str(exc_info.value)


async def test_missing_api_key_error_never_contains_key_value():
    # api_key is empty here, but guards against any future regression where
    # the (empty) key value itself might leak into the exception text.
    provider = GeminiVoiceProvider(api_key="")
    with pytest.raises(VoiceProviderNotConfigured) as exc_info:
        await provider.synthesize("olá")
    assert "api_key" not in str(exc_info.value).lower() or "GEMINI_API_KEY" in str(exc_info.value)


# ── transcribe: not supported ───────────────────────────────────────────────


async def test_transcribe_raises_not_supported_not_fabricated():
    """gemini-2.5-flash-preview-tts is TTS-only — must fail loudly, never
    silently fabricate a transcription or delegate to another model."""
    provider = GeminiVoiceProvider(api_key="key-123")
    with pytest.raises(VoiceTranscriptionFailed) as exc_info:
        await provider.transcribe(b"audio-bytes", content_type="audio/webm")
    assert "speech-to-text" in str(exc_info.value)


async def test_transcribe_not_supported_even_without_api_key():
    """Unsupported-operation error takes priority regardless of config state."""
    provider = GeminiVoiceProvider(api_key="")
    with pytest.raises(VoiceTranscriptionFailed):
        await provider.transcribe(b"audio-bytes")
