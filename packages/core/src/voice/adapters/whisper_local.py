"""WhisperLocalVoiceProvider — stt.local: speech-to-text on this machine with whisper.cpp's server.

    whisper-server -m ggml-small-q5_1.bin --host 127.0.0.1 --port 8282 --convert
    STT_LOCAL_ENABLED=true  WHISPER_LOCAL_BASE_URL=http://127.0.0.1:8282   (both needed; default off)

Transcription only, like the hosted Whisper adapter (groq.py): synthesis reports itself
unconfigured so a TTS failover moves on. Registered with the voice registry only when
enabled (build_whisper_local_provider). Since CP18, it is last in the automatic STT
fallback (api/routes/voice.py STT_CAPABLE) — tried only after elevenlabs/groq, never
promoted to preferred/first by that order alone, and never tried at all unless it is
actually registered (STT_LOCAL_ENABLED=true). voice/budget.py knows its cost bound is
always $0 (calculate_whisper_local_stt_cost_usd), so it is never left out of a fallback
chain for lacking a known price the way an unrecognised provider would be.

Safety:
  - loopback only; the audio never leaves the machine and there is no key to leak
  - bytes only: no filesystem path is ever read, and ffmpeg runs inside the server with
    its own fixed arguments — nothing from a request reaches a command line
  - accepted formats by content type AND by the audio's own signature; size capped before
    anything is sent; language code validated; one job at a time (one model in RAM)
Errors carry fixed messages only: no host, path or server text.
"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    TranscriptSegment,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceTranscriptionFailed,
)
from src.local_runtime.llm import LocalRuntimeUnavailable, local_runtime_settings, loopback_base_url
from src.observability.metrics import core_metrics

CAPABILITY = "stt.local"
_EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/wave": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a"}
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2})?$")
_MIN_BYTES = 64

_requests = core_metrics.counter("stt_local_requests_total", "Local STT calls, labeled by outcome.")
_latency_ms = core_metrics.histogram("stt_local_latency_ms", "Local STT call latency in milliseconds.")
_audio_seconds = core_metrics.counter("stt_local_audio_seconds_total", "Seconds of audio transcribed locally.")

_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, local_runtime_settings().whisper_local_max_concurrency))
    return _slots


def _sniff(audio: bytes) -> str | None:
    """The container the bytes really are, from their signature — never trusted from the request alone."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "wav"
    if audio[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"  # EBML (WebM / Matroska)
    if audio[:4] == b"OggS":
        return "ogg"
    if audio[4:8] == b"ftyp":
        return "m4a"
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


class WhisperLocalVoiceProvider(VoiceProvider):
    def __init__(self, *, base_url: str, http_client: httpx.AsyncClient | None = None,
                 timeout_s: float | None = None, max_bytes: int | None = None) -> None:
        settings = local_runtime_settings()
        self._base_url = base_url
        self._http_client = http_client
        self._timeout = timeout_s or settings.whisper_local_timeout_s
        self._max_bytes = max_bytes or settings.whisper_local_max_bytes

    @property
    def provider_name(self) -> str:
        return "whisper_local"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        raise VoiceProviderNotConfigured("WHISPER_LOCAL_TTS")

    def _validate(self, audio: bytes, content_type: str, language) -> tuple[str, str]:
        if not isinstance(audio, (bytes, bytearray)) or len(audio) < _MIN_BYTES:
            raise VoiceTranscriptionFailed("Audio is empty or too short")
        if len(audio) > self._max_bytes:
            raise VoiceTranscriptionFailed("Audio is too large")
        declared = _EXTENSIONS.get((content_type or "").split(";")[0].strip().lower())
        actual = _sniff(bytes(audio[:16]))
        # webm and ogg are both Opus containers a phone may label either way; anything else must match exactly.
        opus_either_way = {declared, actual} <= {"webm", "ogg"}
        if declared is None or actual is None or (declared != actual and not opus_either_way):
            raise VoiceTranscriptionFailed("Unsupported audio format")
        lang = "auto"
        if language:
            lang = str(language).strip()
            if not _LANGUAGE.match(lang):
                raise VoiceTranscriptionFailed("Unsupported language code")
            lang = lang.split("-")[0].lower()
        return actual, lang

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        try:
            base = loopback_base_url(self._base_url)
        except LocalRuntimeUnavailable as exc:
            _requests.inc(outcome="not_configured")
            raise VoiceProviderNotConfigured("WHISPER_LOCAL_BASE_URL") from exc
        try:
            extension, language = self._validate(audio, content_type, kwargs.get("language"))
        except VoiceTranscriptionFailed:
            _requests.inc(outcome="invalid")
            raise
        files = {"file": (f"audio.{extension}", bytes(audio), f"audio/{'mp4' if extension == 'm4a' else extension}")}
        data = {"response_format": "verbose_json", "temperature": "0", "language": language}

        started = time.monotonic()
        slots = _semaphore()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            _requests.inc(outcome="busy")
            raise VoiceTranscriptionFailed("Local transcription is busy") from None
        client = self._http_client
        owns_client = client is None
        try:
            if owns_client:
                client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
            resp = await client.post(f"{base}/inference", files=files, data=data)
        except httpx.HTTPError as exc:
            _requests.inc(outcome="unavailable")
            raise VoiceTranscriptionFailed(f"Local transcription unavailable: {type(exc).__name__}") from None
        finally:
            slots.release()
            if owns_client and client is not None:
                await client.aclose()

        if resp.status_code != 200:
            _requests.inc(outcome="failed")
            raise VoiceTranscriptionFailed(f"Local transcription returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            _requests.inc(outcome="failed")
            raise VoiceTranscriptionFailed("Local transcription returned a non-JSON response") from None
        if not isinstance(body, dict) or "error" in body:
            _requests.inc(outcome="failed")
            raise VoiceTranscriptionFailed("Local transcription reported an error")
        duration = body.get("duration")
        duration = float(duration) if isinstance(duration, (int, float)) else None
        _requests.inc(outcome="completed")
        _latency_ms.observe((time.monotonic() - started) * 1000, outcome="completed")
        if duration:
            _audio_seconds.inc(duration)
        return TranscriptionResult(
            text=" ".join(str(body.get("text", "")).split()),  # the server breaks lines per segment
            language=body.get("language"),
            duration_seconds=duration,
            signals=None,
            segments=_segments(body.get("segments")),
        )


def _segments(raw) -> tuple[TranscriptSegment, ...]:
    """verbose_json's timed segments; a malformed entry is skipped, never guessed."""
    out = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        start, end, text = item.get("start"), item.get("end"), " ".join(str(item.get("text", "")).split())
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start and text:
            out.append(TranscriptSegment(float(start), float(end), text))
    return tuple(out)


def build_whisper_local_provider() -> WhisperLocalVoiceProvider | None:
    """The provider when stt.local is enabled and points at a loopback server; None otherwise (the default)."""
    settings = local_runtime_settings()
    if not settings.stt_local_enabled:
        return None
    try:
        loopback_base_url(settings.whisper_local_base_url)
    except LocalRuntimeUnavailable:
        return None
    return WhisperLocalVoiceProvider(base_url=settings.whisper_local_base_url)
