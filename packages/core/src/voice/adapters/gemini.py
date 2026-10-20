"""
GeminiVoiceProvider — Gemini 2.5 Flash Preview TTS adapter.

Concrete implementation of src.capability.multimodal.VoiceProvider backed by
the Gemini API's generateContent endpoint with responseModalities=["AUDIO"].

Confirmed against official Google documentation (checked 2026-08-30):
  https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-preview-tts
  https://ai.google.dev/gemini-api/docs/generate-content/speech-generation
  https://ai.google.dev/gemini-api/docs/pricing

Endpoint:  POST https://generativelanguage.googleapis.com/v1beta/models/
                {model_id}:generateContent
Auth:      header "x-goog-api-key: <GEMINI_API_KEY>" (never a query param —
           keeps the key out of URLs/access logs).
Request:   {"contents": [{"parts": [{"text": ...}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                  "speechConfig": {"voiceConfig":
                                    {"prebuiltVoiceConfig": {"voiceName": ...}}}}}
Response:  candidates[0].content.parts[0].inlineData.data — base64-encoded
           RAW PCM audio, confirmed 24kHz / mono / 16-bit. There is no
           container (no WAV/RIFF header), so a browser <audio> element
           cannot play it directly — this adapter wraps the PCM in a
           minimal WAV header before returning (see _pcm_to_wav below).
           usageMetadata.promptTokenCount / candidatesTokenCount give the
           real input/output token counts used for cost calculation
           (voice/cost.py) — never estimated.

Speech-to-text: gemini-2.5-flash-preview-tts is a TTS-ONLY model — it has no
speech-recognition capability. transcribe() raises VoiceTranscriptionFailed
rather than silently routing to a different Gemini model or fabricating a
result. STT keeps defaulting to a different provider
(settings.voice_provider_stt) precisely because of this.

Security
--------
* The API key is read from settings (backend-only). Never accepted from a
  request, never logged, never present in an exception message.
"""
from __future__ import annotations

import base64
import struct

import httpx

from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceSynthesisFailed,
    VoiceTranscriptionFailed,
)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_TIMEOUT = 30.0

# Confirmed raw PCM spec for Gemini TTS output — see module docstring sources.
_PCM_SAMPLE_RATE = 24_000
_PCM_CHANNELS = 1
_PCM_BITS_PER_SAMPLE = 16


def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    """Wrap raw PCM bytes in a canonical 44-byte WAV/RIFF header.

    Gemini returns headerless PCM; browsers need a container to play it.
    Pure stdlib (struct) — no new dependency.
    """
    byte_rate = _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_BITS_PER_SAMPLE // 8
    block_align = _PCM_CHANNELS * _PCM_BITS_PER_SAMPLE // 8
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, _PCM_CHANNELS, _PCM_SAMPLE_RATE, byte_rate, block_align, _PCM_BITS_PER_SAMPLE,
        b"data", data_size,
    )
    return header + pcm_bytes


class GeminiVoiceProvider(VoiceProvider):
    """Gemini 2.5 Flash Preview TTS adapter. TTS only — see module docstring.

    ``http_client`` is injectable so tests can pass a fake/mocked transport
    instead of hitting the network.
    """

    def __init__(
        self,
        *,
        api_key: str,
        tts_voice_name: str = "Kore",
        tts_model_id: str = "gemini-2.5-flash-preview-tts",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._tts_voice_name = tts_voice_name
        self._tts_model_id = tts_model_id
        self._http_client = http_client

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        if not self._api_key:
            raise VoiceProviderNotConfigured("GEMINI_API_KEY")

        voice_name = voice_id or self._tts_voice_name
        url = f"{_BASE_URL}/{self._tts_model_id}:generateContent"
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}
                },
            },
        }

        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise VoiceSynthesisFailed(f"Gemini TTS request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code != 200:
            raise VoiceSynthesisFailed(
                f"Gemini TTS returned {resp.status_code}: {_safe_error_body(resp)}"
            )

        try:
            body = resp.json()
            part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
            pcm_bytes = base64.b64decode(part["data"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VoiceSynthesisFailed("Gemini TTS returned an unexpected response shape") from exc

        usage = body.get("usageMetadata", {})
        input_tokens = int(usage.get("promptTokenCount", 0) or 0)
        output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

        wav_bytes = _pcm_to_wav(pcm_bytes)
        return SynthesisResult(
            audio=wav_bytes,
            content_type="audio/wav",
            voice_id=voice_name,
            metadata={"input_tokens": input_tokens, "output_tokens": output_tokens},
        )

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        raise VoiceTranscriptionFailed(
            f"{self._tts_model_id} does not support speech-to-text; "
            "configure VOICE_PROVIDER_STT to a provider that does (e.g. elevenlabs)"
        )


def _safe_error_body(resp: httpx.Response) -> str:
    """Return a short, log-safe error body — never echoes request headers/key."""
    try:
        return resp.text[:300]
    except Exception:
        return "<unreadable body>"
