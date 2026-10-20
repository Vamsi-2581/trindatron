"""
Unit tests for ElevenLabsVoiceProvider — no real network calls.

A fake httpx.AsyncClient-like object is injected via the constructor so
these tests never hit the ElevenLabs API.
"""
from __future__ import annotations

import pytest

from src.capability.multimodal import (
    VoiceProviderNotConfigured,
    VoiceSynthesisFailed,
    VoiceTranscriptionFailed,
)
from src.voice.adapters.elevenlabs import ElevenLabsVoiceProvider


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", json_body=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._json_body = json_body
        self.headers = headers or {}

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def aclose(self):
        pass


# ── synthesize ────────────────────────────────────────────────────────────


async def test_synthesize_missing_api_key_raises_not_configured():
    provider = ElevenLabsVoiceProvider(api_key="", tts_voice_id="voice-1")
    with pytest.raises(VoiceProviderNotConfigured) as exc_info:
        await provider.synthesize("olá")
    assert exc_info.value.env_var == "ELEVENLABS_API_KEY"


async def test_synthesize_missing_voice_id_raises_not_configured():
    provider = ElevenLabsVoiceProvider(api_key="key-123", tts_voice_id="")
    with pytest.raises(VoiceProviderNotConfigured) as exc_info:
        await provider.synthesize("olá")
    assert exc_info.value.env_var == "ELEVENLABS_TTS_VOICE_ID"


async def test_synthesize_success_returns_audio_bytes():
    fake_client = _FakeClient(_FakeResponse(200, content=b"\x00\x01fake-mp3", headers={"content-type": "audio/mpeg"}))
    provider = ElevenLabsVoiceProvider(api_key="key-123", tts_voice_id="voice-1", http_client=fake_client)

    result = await provider.synthesize("olá mundo")

    assert result.audio == b"\x00\x01fake-mp3"
    assert result.content_type == "audio/mpeg"
    assert result.voice_id == "voice-1"
    url, kwargs = fake_client.calls[0]
    assert url.endswith("/text-to-speech/voice-1")
    assert kwargs["headers"]["xi-api-key"] == "key-123"


async def test_synthesize_request_override_voice_id():
    fake_client = _FakeClient(_FakeResponse(200, content=b"audio"))
    provider = ElevenLabsVoiceProvider(api_key="key-123", tts_voice_id="default-voice", http_client=fake_client)

    result = await provider.synthesize("hi", voice_id="override-voice")

    assert result.voice_id == "override-voice"
    url, _ = fake_client.calls[0]
    assert url.endswith("/text-to-speech/override-voice")


async def test_synthesize_upstream_failure_raises_synthesis_failed():
    fake_client = _FakeClient(_FakeResponse(401, content=b"unauthorized"))
    provider = ElevenLabsVoiceProvider(api_key="bad-key", tts_voice_id="voice-1", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed):
        await provider.synthesize("olá")


async def test_synthesize_error_message_never_contains_api_key():
    fake_client = _FakeClient(_FakeResponse(401, content=b"unauthorized"))
    provider = ElevenLabsVoiceProvider(api_key="super-secret-key", tts_voice_id="voice-1", http_client=fake_client)

    with pytest.raises(VoiceSynthesisFailed) as exc_info:
        await provider.synthesize("olá")
    assert "super-secret-key" not in str(exc_info.value)


# ── transcribe ────────────────────────────────────────────────────────────


async def test_transcribe_missing_api_key_raises_not_configured():
    provider = ElevenLabsVoiceProvider(api_key="")
    with pytest.raises(VoiceProviderNotConfigured) as exc_info:
        await provider.transcribe(b"audio-bytes", content_type="audio/webm")
    assert exc_info.value.env_var == "ELEVENLABS_API_KEY"


async def test_transcribe_success_returns_text_and_language():
    fake_client = _FakeClient(
        _FakeResponse(200, json_body={"text": "olá mundo", "language_code": "pt", "audio_duration_secs": 3.25})
    )
    provider = ElevenLabsVoiceProvider(api_key="key-123", http_client=fake_client)

    result = await provider.transcribe(b"audio-bytes", content_type="audio/webm")

    assert result.text == "olá mundo"
    assert result.language == "pt"
    # Real duration from the API's own field — needed for STT cost accounting.
    assert result.duration_seconds == 3.25
    assert result.signals is None  # no emotion detector — never fabricated
    url, kwargs = fake_client.calls[0]
    assert url.endswith("/speech-to-text")
    assert kwargs["files"]["file"][2] == "audio/webm"


async def test_transcribe_upstream_failure_raises_transcription_failed():
    fake_client = _FakeClient(_FakeResponse(500, content=b"server error"))
    provider = ElevenLabsVoiceProvider(api_key="key-123", http_client=fake_client)

    with pytest.raises(VoiceTranscriptionFailed):
        await provider.transcribe(b"audio-bytes")


async def test_transcribe_non_json_response_raises_transcription_failed():
    fake_client = _FakeClient(_FakeResponse(200, content=b"not json"))
    provider = ElevenLabsVoiceProvider(api_key="key-123", http_client=fake_client)

    with pytest.raises(VoiceTranscriptionFailed):
        await provider.transcribe(b"audio-bytes")


async def test_transcribe_missing_duration_field_stays_none_not_fabricated():
    fake_client = _FakeClient(_FakeResponse(200, json_body={"text": "oi", "language_code": "pt"}))
    provider = ElevenLabsVoiceProvider(api_key="key-123", http_client=fake_client)

    result = await provider.transcribe(b"audio-bytes")

    assert result.duration_seconds is None
