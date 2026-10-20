"""
ElevenLabsVoiceProvider — reference VoiceProvider adapter.

Concrete implementation of src.capability.multimodal.VoiceProvider backed by
the ElevenLabs REST API (text-to-speech + speech-to-text). This is the only
concrete voice adapter today; OpenAI/Google/others can be added later as
sibling files implementing the same ABC, with zero changes to routes or the
registry.

Security
--------
* The API key is read from settings (backend-only, injected at startup in
  main.py's lifespan). It is never accepted from a request body or header.
* Nothing here logs the API key or embeds it in an exception message.
"""
from __future__ import annotations

import httpx

from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceSynthesisFailed,
    VoiceTranscriptionFailed,
)

_BASE_URL = "https://api.elevenlabs.io/v1"
_DEFAULT_TIMEOUT = 30.0


class ElevenLabsVoiceProvider(VoiceProvider):
    """ElevenLabs adapter for text-to-speech and speech-to-text.

    ``http_client`` is injectable so tests can pass a fake/mocked transport
    instead of hitting the network.
    """

    def __init__(
        self,
        *,
        api_key: str,
        tts_voice_id: str = "",
        tts_model_id: str = "eleven_multilingual_v2",
        stt_model_id: str = "scribe_v1",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._tts_voice_id = tts_voice_id
        self._tts_model_id = tts_model_id
        self._stt_model_id = stt_model_id
        self._http_client = http_client

    @property
    def provider_name(self) -> str:
        return "elevenlabs"

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._api_key}

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        if not self._api_key:
            raise VoiceProviderNotConfigured("ELEVENLABS_API_KEY")

        resolved_voice_id = voice_id or self._tts_voice_id
        if not resolved_voice_id:
            raise VoiceProviderNotConfigured("ELEVENLABS_TTS_VOICE_ID")

        payload = {"text": text, "model_id": self._tts_model_id}
        url = f"{_BASE_URL}/text-to-speech/{resolved_voice_id}"
        headers = {**self._headers(), "Accept": "audio/mpeg"}

        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise VoiceSynthesisFailed(f"ElevenLabs TTS request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code != 200:
            raise VoiceSynthesisFailed(
                f"ElevenLabs TTS returned {resp.status_code}: {_safe_error_body(resp)}"
            )

        content_type = resp.headers.get("content-type", "audio/mpeg")
        return SynthesisResult(audio=resp.content, content_type=content_type, voice_id=resolved_voice_id)

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        if not self._api_key:
            raise VoiceProviderNotConfigured("ELEVENLABS_API_KEY")

        url = f"{_BASE_URL}/speech-to-text"
        files = {"file": ("audio", audio, content_type)}
        data = {"model_id": self._stt_model_id}

        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        try:
            resp = await client.post(url, headers=self._headers(), files=files, data=data)
        except httpx.HTTPError as exc:
            raise VoiceTranscriptionFailed(f"ElevenLabs STT request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code != 200:
            raise VoiceTranscriptionFailed(
                f"ElevenLabs STT returned {resp.status_code}: {_safe_error_body(resp)}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise VoiceTranscriptionFailed("ElevenLabs STT returned a non-JSON response") from exc

        text = body.get("text", "")
        return TranscriptionResult(
            text=text,
            language=body.get("language_code"),
            # Real duration from the API's own audio_duration_secs field — needed
            # for STT cost accounting (ElevenLabs Scribe bills per hour of audio
            # processed, see voice/cost.py). Never estimated from file size.
            duration_seconds=body.get("audio_duration_secs"),
            signals=None,  # ElevenLabs Scribe does not return prosody/emotion signals today
        )


def _safe_error_body(resp: httpx.Response) -> str:
    """Return a short, log-safe error body — never echoes request headers/key."""
    try:
        return resp.text[:300]
    except Exception:
        return "<unreadable body>"
