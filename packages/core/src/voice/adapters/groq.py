"""GroqVoiceProvider — speech-to-text with open-weight Whisper large-v3-turbo, hosted.

Transcription only: Groq serves no text-to-speech here, so `synthesize` reports
itself unconfigured and the TTS failover moves on. Whisper's weights are MIT
licensed; the hosted call is billed per hour of audio with a 10-second minimum
per request (voice/cost.py).

The API key is read from settings at startup, never from a request, and never
appears in a log line or exception message.
"""
from __future__ import annotations

import httpx

from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceTranscriptionFailed,
)

_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_TIMEOUT = 30.0


class GroqVoiceProvider(VoiceProvider):
    def __init__(self, *, api_key: str, stt_model_id: str = "whisper-large-v3-turbo",
                 http_client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._stt_model_id = stt_model_id
        self._http_client = http_client

    @property
    def provider_name(self) -> str:
        return "groq"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        raise VoiceProviderNotConfigured("GROQ_TTS")

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        if not self._api_key:
            raise VoiceProviderNotConfigured("GROQ_API_KEY")
        extension = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3",
                     "audio/mp4": "m4a", "audio/x-m4a": "m4a"}.get(content_type, "webm")
        files = {"file": (f"audio.{extension}", audio, content_type)}
        data = {"model": self._stt_model_id, "response_format": "verbose_json", "temperature": "0"}
        language = kwargs.get("language")
        if language:
            data["language"] = str(language)[:5]

        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        try:
            resp = await client.post(f"{_BASE_URL}/audio/transcriptions",
                                     headers={"Authorization": f"Bearer {self._api_key}"}, files=files, data=data)
        except httpx.HTTPError as exc:
            raise VoiceTranscriptionFailed(f"Groq STT request failed: {type(exc).__name__}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code != 200:
            raise VoiceTranscriptionFailed(f"Groq STT returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise VoiceTranscriptionFailed("Groq STT returned a non-JSON response") from exc
        duration = body.get("duration")
        return TranscriptionResult(
            text=str(body.get("text", "")).strip(),
            language=body.get("language"),
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
            signals=None,
        )
