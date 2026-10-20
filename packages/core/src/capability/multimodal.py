"""
Multimodal provider interfaces — Fase I stub.

These ABCs define the contract for future IMAGE, VIDEO, and AUDIO providers.
No concrete implementations exist yet for IMAGE/VIDEO; the capability router
intercepts requests for those capabilities and returns a "coming soon"
response before reaching the LLM.

Audio/voice sub-concepts (Tool/Connector Foundation etapa — corrects an
inconsistency found during the Post-V2 Architecture Audit: Capability.AUDIO
was documented/gated as fully "unimplemented" while transcription/synthesis
actually work today via a separate subsystem). "AUDIO" is not one thing —
these four sub-concepts never share a single gate or status:

  VOICE_INPUT (transcription/STT)   — IMPLEMENTED. VoiceProvider.transcribe()
                                       -> /voice/transcribe. Concrete adapter:
                                       src/voice/adapters/elevenlabs.py.
  VOICE_OUTPUT (synthesis/TTS)      — IMPLEMENTED. VoiceProvider.synthesize()
                                       -> /voice/synthesize. Same adapter.
  AUDIO_GENERATION (e.g. music/podcast/sound-effect from a text prompt)
                                     — NOT IMPLEMENTED. No ABC exists for this
                                       yet; gated by Capability.AUDIO's
                                       "coming soon" message below.
  AUDIO_PROCESSING (e.g. editing/mixing/format conversion)
                                     — NOT IMPLEMENTED. No ABC exists yet.

VOICE_INPUT/VOICE_OUTPUT are reached ONLY through the dedicated mic ->
STT -> TRINITY CORE -> TTS flow (src/api/routes/voice.py) — a client
calling that flow directly, never through the in-chat capability
classifier. The classifier-driven Capability.AUDIO chat gate below is a
COMPLETELY SEPARATE code path: it governs in-chat requests like
"generate a podcast about X" (AUDIO_GENERATION/AUDIO_PROCESSING), which
remain genuinely unimplemented — no LLM here fabricates audio, so that
gate is intentionally unchanged. The two paths answering differently to
"does TRINDATRON support audio?" is expected and correct once read this
way: one is about voice conversation I/O, the other about generating an
audio file as a chat deliverable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


from src.capability.classifier import Capability  # noqa: E402 — imported after ABCs

# Friendly "coming soon" messages shown to the user when an unimplemented
# capability is detected. Keyed by Capability; values are plain text.
# What TRINDATRON says when a request needs a capability it does not have.
# Honest and exact — never a promise ("em breve") and never a general refusal.
# IMAGE and VIDEO are real media jobs now (services/chat_media.py); these two
# texts are only reached if that path is bypassed, and still say the truth.
COMING_SOON_MESSAGES: dict[Capability, str] = {
    Capability.IMAGE: (
        "Não consegui iniciar a criação da imagem por aqui. "
        "Peça na conversa, por exemplo: \"Crie uma imagem de um farol, formato 9:16\"."
    ),
    Capability.VIDEO: (
        "Não consegui iniciar a criação do vídeo por aqui. "
        "Peça na conversa, por exemplo: \"Crie um vídeo de 5 segundos de ondas no mar\"."
    ),
    # Transcription and read-aloud DO work (the voice feature). What is missing is
    # generating an audio file as a deliverable, and that is named exactly.
    Capability.AUDIO: (
        "Criar um arquivo de áudio a partir de texto (música, podcast, efeitos sonoros) "
        "não é uma capacidade do TRINDATRON hoje. Você pode falar comigo pelo microfone "
        "e ouvir as respostas em voz."
    ),
}


class ImageProvider(ABC):
    """Provider that generates images from text prompts.

    Connector Foundation note: this IS the "ImageGenerationConnector" the
    Post-V2 Architecture Audit's Connector Foundation refers to — kept
    under its pre-existing name rather than renamed, since it already had
    the right shape and no concrete implementation exists yet to migrate.
    connector_name/is_available() were added here (previously absent) so
    a future concrete adapter can be registered in a
    src.connectors.registry.ConnectorRegistry[ImageProvider] exactly like
    any other connector kind.
    """

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "higgsfield", "krea"."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> bytes:
        """Generate an image and return raw bytes (e.g. PNG)."""

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        API key is configured). Defaults to True."""
        return True


@dataclass
class ProsodySignals:
    """Placeholder structure for future emotion/prosody signals.

    All fields are optional and default to None. They exist so the
    interface is ready to carry real signals once a provider actually
    returns them — nothing here is inferred, guessed, or fabricated.
    A provider that does not support a signal simply leaves it None.
    """

    emotion: str | None = None       # e.g. "neutral", "frustrated" — provider-reported only
    confidence: float | None = None  # provider's confidence in the emotion label, 0..1
    pace: float | None = None        # speaking rate signal (e.g. words/sec), if provided
    urgency: float | None = None     # urgency score, 0..1, if provided
    prosody: dict | None = None      # raw provider-specific prosody payload, if provided


@dataclass(frozen=True)
class TranscriptSegment:
    """One timed stretch of a transcript, in seconds from the start of the audio."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    """Result of VoiceProvider.transcribe()."""

    text: str
    language: str | None = None
    duration_seconds: float | None = None
    signals: ProsodySignals | None = None  # None until a provider actually supports it
    # Timed segments, when the provider returns them (captions need them; a chat turn does not).
    segments: tuple[TranscriptSegment, ...] | None = None


@dataclass
class SynthesisResult:
    """Result of VoiceProvider.synthesize()."""

    audio: bytes
    content_type: str = "audio/mpeg"
    voice_id: str | None = None
    metadata: dict = field(default_factory=dict)


class VoiceProviderError(Exception):
    """Base exception for VoiceProvider failures."""


class VoiceProviderNotConfigured(VoiceProviderError):
    """Raised when a required configuration value (e.g. an API key) is missing.

    ``env_var`` names the exact environment variable the caller must set —
    routes surface this directly instead of inventing a fallback secret.
    """

    def __init__(self, env_var: str) -> None:
        self.env_var = env_var
        super().__init__(f"Voice provider is not configured: missing {env_var}")


class VoiceSynthesisFailed(VoiceProviderError):
    """Raised when the provider's text-to-speech call fails."""


class VoiceTranscriptionFailed(VoiceProviderError):
    """Raised when the provider's speech-to-text call fails."""


class VoiceProvider(ABC):
    """Provider that synthesizes speech or transcribes audio.

    Concrete implementations live outside the TRINITY Core, under
    src/voice/adapters/ (e.g. elevenlabs.py). The Core only imports this
    ABC, never a concrete adapter — providers are swapped via the
    VoiceProviderRegistry (src/voice/registry.py) and settings.voice_provider.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique identifier for this provider, e.g. "elevenlabs"."""

    @abstractmethod
    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        """Convert text to speech and return a SynthesisResult.

        Raises:
            VoiceProviderNotConfigured: if a required setting (API key, voice_id) is missing.
            VoiceSynthesisFailed: if the provider call fails.
        """

    @abstractmethod
    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        """Transcribe audio bytes to a TranscriptionResult.

        Raises:
            VoiceProviderNotConfigured: if a required setting (API key) is missing.
            VoiceTranscriptionFailed: if the provider call fails.
        """


class VideoProvider(ABC):
    """Provider that generates short video clips from text prompts.

    Connector Foundation note: this IS the "VideoGenerationConnector" the
    Post-V2 Architecture Audit's Connector Foundation refers to — same
    reasoning as ImageProvider above (kept under its pre-existing name,
    connector_name/is_available() added for the same registry pattern).
    """

    @property
    @abstractmethod
    def connector_name(self) -> str:
        """Unique identifier for this connector, e.g. "runway", "kling"."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> bytes:
        """Generate a video clip and return raw bytes (e.g. MP4)."""

    async def is_available(self) -> bool:
        """Whether this connector can be used right now (e.g. a required
        API key is configured). Defaults to True."""
        return True
