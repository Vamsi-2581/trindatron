"""Speech-to-text fails over like speech synthesis: another transcriber answers, and only it is billed."""
from __future__ import annotations

import io

import pytest

from src.api.deps import get_voice_registry
from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceTranscriptionFailed,
)
from src.main import app
from src.models.usage import UsageRecord
from src.voice.registry import VoiceProviderRegistry


class FakeTranscriber(VoiceProvider):
    def __init__(self, name: str, *, fail: Exception | None = None, text: str = "", duration: float = 2.0) -> None:
        self.name, self.fail, self.text, self.duration, self.calls = name, fail, text or f"texto de {name}", duration, 0

    @property
    def provider_name(self) -> str:
        return self.name

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        raise VoiceProviderNotConfigured(f"{self.name.upper()}_TTS")

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return TranscriptionResult(text=self.text, language="pt", duration_seconds=self.duration)


class TtsOnly(VoiceProvider):
    calls = 0

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        return SynthesisResult(audio=b"wav", content_type="audio/wav")

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        TtsOnly.calls += 1
        raise VoiceTranscriptionFailed("tts only")


@pytest.fixture
def registry():
    created: dict = {}

    def install(*providers: VoiceProvider) -> VoiceProviderRegistry:
        reg = VoiceProviderRegistry()
        for provider in providers:
            reg.register(provider)
        app.dependency_overrides[get_voice_registry] = lambda: reg
        created["reg"] = reg
        return reg

    yield install
    app.dependency_overrides.pop(get_voice_registry, None)


def _audio():
    return {"file": ("clip.webm", io.BytesIO(b"\x00\x01fake-audio"), "audio/webm")}


def test_when_the_configured_transcriber_is_out_of_quota_the_next_one_answers(client, auth_headers, registry, db):
    primary = FakeTranscriber("elevenlabs", fail=VoiceTranscriptionFailed("429"))
    backup = FakeTranscriber("groq", duration=30.0)
    TtsOnly.calls = 0
    registry(primary, backup, TtsOnly())

    response = client.post("/voice/transcribe", files=_audio(), headers=auth_headers)

    assert response.status_code == 200 and response.json()["text"] == "texto de groq"
    assert primary.calls == 1 and backup.calls == 1 and TtsOnly.calls == 0
    record = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert record.model.startswith("groq:stt:")
    assert float(record.cost_usd) == pytest.approx(30 / 3600 * 0.04, abs=1e-8)


def test_a_healthy_configured_transcriber_is_never_bypassed(client, auth_headers, registry):
    primary, backup = FakeTranscriber("elevenlabs"), FakeTranscriber("groq")
    registry(primary, backup)
    response = client.post("/voice/transcribe", files=_audio(), headers=auth_headers)
    assert response.json()["text"] == "texto de elevenlabs" and backup.calls == 0


def test_when_every_transcriber_fails_nothing_is_charged_and_no_detail_leaks(client, auth_headers, registry, db):
    registry(FakeTranscriber("elevenlabs", fail=VoiceTranscriptionFailed("secret-a")),
             FakeTranscriber("groq", fail=VoiceProviderNotConfigured("GROQ_API_KEY")))
    response = client.post("/voice/transcribe", files=_audio(), headers=auth_headers)
    assert response.status_code in (424, 502)
    assert "secret" not in response.text and "GROQ_API_KEY" not in response.text
    assert db.query(UsageRecord).filter(UsageRecord.status == "completed").count() == 0


def test_whisper_local_is_the_last_resort_when_registered_and_costs_nothing(client, auth_headers, registry, db):
    """CP18: whisper_local joins STT_CAPABLE as the last fallback, only when actually registered."""
    primary = FakeTranscriber("elevenlabs", fail=VoiceTranscriptionFailed("429"))
    secondary = FakeTranscriber("groq", fail=VoiceTranscriptionFailed("quota"))
    local = FakeTranscriber("whisper_local", duration=5.0)
    registry(primary, secondary, local)

    response = client.post("/voice/transcribe", files=_audio(), headers=auth_headers)

    assert response.status_code == 200 and response.json()["text"] == "texto de whisper_local"
    assert primary.calls == 1 and secondary.calls == 1 and local.calls == 1
    record = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert record.model.startswith("whisper_local:stt:")
    assert float(record.cost_usd) == 0.0 and float(record.tcu) == 0.0


def test_whisper_local_not_registered_leaves_the_existing_chain_unchanged(client, auth_headers, registry):
    """Adding it to STT_CAPABLE must not affect deployments where it is never enabled."""
    primary, backup = FakeTranscriber("elevenlabs"), FakeTranscriber("groq")
    registry(primary, backup)  # no whisper_local registered — mirrors STT_LOCAL_ENABLED=false
    response = client.post("/voice/transcribe", files=_audio(), headers=auth_headers)
    assert response.json()["text"] == "texto de elevenlabs" and backup.calls == 0
