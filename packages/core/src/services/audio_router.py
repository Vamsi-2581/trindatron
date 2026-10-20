"""
AudioRouter — concrete executor backing ExecutorKind.AUDIO.

Thin dispatch wrapper around a VoiceProvider. Powers the dedicated mic ->
STT -> TRINITY CORE -> TTS flow exposed at src/api/routes/voice.py.

Scope note
----------
This is NOT wired into the classifier-driven chat/agent pipeline
(sessions.py, agent_service.py). A chat message classified as
Capability.AUDIO (e.g. "generate a podcast about X") still returns the
"coming soon" response from UNIMPLEMENTED_CAPABILITIES — no LLM call here
fabricates audio content, and connecting real audio bytes to the SSE
chat stream is future work (see capability/capability_router.py). What IS
real: transcribing a recorded utterance and synthesizing speech from text,
both via an actual provider call.
"""
from __future__ import annotations

from src.capability.multimodal import SynthesisResult, TranscriptionResult, VoiceProvider


class AudioRouter:
    """Dispatches transcribe/synthesize calls to a registered VoiceProvider."""

    def __init__(self, provider: VoiceProvider) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        return await self._provider.transcribe(audio, content_type=content_type, **kwargs)

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        return await self._provider.synthesize(text, voice_id=voice_id, **kwargs)
