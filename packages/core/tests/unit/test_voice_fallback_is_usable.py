"""The TTS fallback has to be able to actually run.

Production, 2026-09-11: Gemini TTS was returning 429 quota errors, the
failover chain correctly moved on to ElevenLabs — and ElevenLabs raised
VoiceProviderNotConfigured("ELEVENLABS_TTS_VOICE_ID") because the voice id
defaulted to "". Read-aloud died anyway, with a 424 instead of a 502.

The failover logic was right; the fallback was inert. A provider that cannot
synthesise without an extra setting nobody provisioned is not a fallback, so
the default voice is now a real premade id (verified against the live
/v1/voices catalogue). These tests keep it that way.
"""
from __future__ import annotations

import pytest

from src.config import settings
from src.voice.adapters.elevenlabs import ElevenLabsVoiceProvider
from src.capability.multimodal import VoiceProviderNotConfigured


def test_a_voice_id_is_configured_out_of_the_box():
    """With no environment overrides, the fallback must be runnable."""
    assert settings.elevenlabs_tts_voice_id, (
        "ELEVENLABS_TTS_VOICE_ID has no default — the TTS fallback cannot run "
        "without an extra setting, which is how read-aloud died in production"
    )


def test_the_default_voice_id_looks_like_a_real_identifier():
    """ElevenLabs voice ids are 20-character alphanumeric strings. This
    guards against a placeholder like 'default' or 'TODO' slipping in."""
    voice_id = settings.elevenlabs_tts_voice_id
    assert len(voice_id) == 20, voice_id
    assert voice_id.isalnum(), voice_id


def test_the_multilingual_model_is_the_default():
    """pt-BR is a first-class language for this product; the monolingual
    English model would mispronounce every answer the router returns."""
    assert settings.elevenlabs_tts_model_id == "eleven_multilingual_v2"


@pytest.mark.asyncio
async def test_adapter_does_not_demand_a_voice_id_when_one_is_configured():
    """The adapter must reach the network call rather than refusing up
    front. It fails here on the fake key, which is the point: the failure is
    about credentials, not about missing configuration."""
    provider = ElevenLabsVoiceProvider(
        api_key="fake-key-for-this-test",
        tts_voice_id=settings.elevenlabs_tts_voice_id,
    )
    with pytest.raises(Exception) as exc:
        await provider.synthesize("olá")
    assert not isinstance(exc.value, VoiceProviderNotConfigured), (
        "a configured voice id must not raise 'not configured'"
    )


@pytest.mark.asyncio
async def test_an_explicitly_blank_voice_id_still_reports_what_is_missing():
    """Operators who deliberately clear the setting should still get the
    precise, actionable error — the default must not hide that path."""
    provider = ElevenLabsVoiceProvider(api_key="fake-key", tts_voice_id="")
    with pytest.raises(VoiceProviderNotConfigured) as exc:
        await provider.synthesize("olá")
    assert exc.value.env_var == "ELEVENLABS_TTS_VOICE_ID"


@pytest.mark.asyncio
async def test_a_caller_supplied_voice_still_wins():
    """Per-request voice selection must override the default."""
    provider = ElevenLabsVoiceProvider(api_key="fake-key", tts_voice_id="")
    with pytest.raises(Exception) as exc:
        await provider.synthesize("olá", voice_id="CwhRBWXzGAHq8TQ4Fs17")
    assert not isinstance(exc.value, VoiceProviderNotConfigured)
