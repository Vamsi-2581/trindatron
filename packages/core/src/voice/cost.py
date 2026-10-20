"""
Voice cost calculators — one function per (provider, operation) billing unit.

Unlike trinity_router/tcu.py's calculate_actual_cost_usd() (strictly
token-based, built for text chat/agent completions), voice providers bill in
different units depending on the provider and the operation:

  Gemini 2.5 Flash Preview TTS  — tokens (input text tokens, output audio
    tokens), same *shape* as chat billing but a different price tier. Real
    counts come from the API's own usageMetadata (see
    voice/adapters/gemini.py) — never estimated here.

  ElevenLabs TTS                — characters of input text sent. The text
    the caller submits IS the meter, regardless of resulting audio length.

  ElevenLabs STT (Scribe)       — seconds of audio processed. Real duration
    comes from the API's own `audio_duration_secs` field (see
    voice/adapters/elevenlabs.py) — never estimated from file size.

Prices are the exact published rates at the time this was written. They are
NOT re-verified at runtime — if a provider changes pricing, these constants
must be updated by hand.

Sources (checked 2026-08-30):
  Gemini:     https://ai.google.dev/gemini-api/docs/pricing
              (model: gemini-2.5-flash-preview-tts)
  ElevenLabs: https://elevenlabs.io/pricing/api
"""
from __future__ import annotations

# ── Gemini 2.5 Flash Preview TTS ────────────────────────────────────────────
GEMINI_TTS_INPUT_USD_PER_1M_TOKENS: float = 0.50
GEMINI_TTS_OUTPUT_USD_PER_1M_TOKENS: float = 10.00

# ── ElevenLabs ───────────────────────────────────────────────────────────────
# TTS rate is for the Multilingual v2 / v3 model family — our configured
# default (settings.elevenlabs_tts_model_id). Flash/Turbo models are billed
# at half this rate; if that model becomes the default, update this constant.
ELEVENLABS_TTS_USD_PER_1K_CHARS: float = 0.10
ELEVENLABS_STT_USD_PER_HOUR: float = 0.22


# ── Groq (Whisper large-v3-turbo) ────────────────────────────────────────────
# console.groq.com pricing, checked 2026-09-14: $0.04 per hour, 10 s minimum per request.
GROQ_STT_USD_PER_HOUR: float = 0.04
GROQ_STT_MINIMUM_SECONDS: float = 10.0


def calculate_groq_stt_cost_usd(duration_seconds: float) -> float:
    """Cost of one Groq transcription from the audio duration it reports (10 s minimum)."""
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")
    billed = max(GROQ_STT_MINIMUM_SECONDS, duration_seconds)
    return round(billed / 3600.0 * GROQ_STT_USD_PER_HOUR, 8)


def calculate_gemini_tts_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Cost of one Gemini TTS call from real token counts (usageMetadata)."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    input_cost = (input_tokens / 1_000_000) * GEMINI_TTS_INPUT_USD_PER_1M_TOKENS
    output_cost = (output_tokens / 1_000_000) * GEMINI_TTS_OUTPUT_USD_PER_1M_TOKENS
    return round(input_cost + output_cost, 8)


def calculate_elevenlabs_tts_cost_usd(characters: int) -> float:
    """Cost of one ElevenLabs TTS call from the character count sent."""
    if characters < 0:
        raise ValueError("characters cannot be negative")
    return round((characters / 1_000) * ELEVENLABS_TTS_USD_PER_1K_CHARS, 8)


def calculate_elevenlabs_stt_cost_usd(duration_seconds: float) -> float:
    """Cost of one ElevenLabs STT call from the real audio duration processed."""
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")
    hours = duration_seconds / 3600.0
    return round(hours * ELEVENLABS_STT_USD_PER_HOUR, 8)


def calculate_whisper_local_stt_cost_usd(duration_seconds: float) -> float:
    """Cost of one local Whisper transcription: always $0 — no API is called, no matter the duration."""
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")
    return 0.0


def calculate_voice_cost_usd(
    *,
    provider_name: str,
    operation: str,  # "tts" | "stt"
    characters: int | None = None,
    duration_seconds: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> float:
    """Dispatch to the correct calculator for (provider_name, operation).

    Raises ValueError for a combination with no known billing unit — this is
    intentional: silently returning 0.0 would under-bill without anyone
    noticing. Every (provider, operation) actually reachable through
    routes/voice.py must have an entry here.
    """
    if provider_name == "gemini" and operation == "tts":
        return calculate_gemini_tts_cost_usd(input_tokens or 0, output_tokens or 0)
    if provider_name == "elevenlabs" and operation == "tts":
        return calculate_elevenlabs_tts_cost_usd(characters or 0)
    if provider_name == "elevenlabs" and operation == "stt":
        return calculate_elevenlabs_stt_cost_usd(duration_seconds or 0.0)
    if provider_name == "groq" and operation == "stt":
        return calculate_groq_stt_cost_usd(duration_seconds or 0.0)
    if provider_name == "whisper_local" and operation == "stt":
        return calculate_whisper_local_stt_cost_usd(duration_seconds or 0.0)
    raise ValueError(f"No cost calculator for provider={provider_name!r} operation={operation!r}")
