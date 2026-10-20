"""What a voice call may cost before it runs, so a request never spends more than its plan allows.

Measured on a physical phone: a Free account (max 2.0 TCU per request) read one answer aloud; the first TTS provider
failed, the fallback ran, and the settlement recorded 4.34 TCU — more than twice the request's ceiling, discovered only
after the money was spent. The reservation had been the flat per-request figure, and nothing compared that figure with
what the provider about to run would charge.

Voice is billed in units known before the call, or bounded by them:

* ElevenLabs TTS — characters of the text. Exact.
* Gemini TTS — tokens: text in, audio out. Not exact, but bounded by the text: the audio is the text read aloud. The
  bounds below are about 1.8x what the stored readings show (input ~0.25 tokens per character, audio ~7-10 tokens per
  text token), so an estimate is never below the real cost.
* STT — seconds of audio. The duration is reported only after the call, but bounded by the upload size at a floor
  bitrate no real recording goes below: phone AAC and browser Opus both record well above 16 kbps.

So every provider in a fallback chain gets an upper-bound cost before anything runs, a provider whose bound exceeds
the request's ceiling is never tried, the reservation is the worst case of the providers that remain, and the
settlement cannot exceed what was reserved (see settle_tcu).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import TypeVar

from trinity_router.tcu import calculate_tcu

from src.voice.cost import (
    ELEVENLABS_STT_USD_PER_HOUR,
    ELEVENLABS_TTS_USD_PER_1K_CHARS,
    GEMINI_TTS_INPUT_USD_PER_1M_TOKENS,
    GEMINI_TTS_OUTPUT_USD_PER_1M_TOKENS,
    GROQ_STT_MINIMUM_SECONDS,
    GROQ_STT_USD_PER_HOUR,
)

logger = logging.getLogger(__name__)

#: Upper bounds for Gemini TTS: a fixed part every call pays (the stored readings show ~7 input and 57-70 audio
#: tokens even for a few words) plus a part per character of text. See the module docstring for the margins.
GEMINI_INPUT_TOKENS_FIXED = 16
GEMINI_AUDIO_TOKENS_FIXED = 200
GEMINI_INPUT_TOKENS_PER_CHAR = 0.5
GEMINI_AUDIO_TOKENS_PER_CHAR = 4.0
#: The lowest bitrate a real recording uses, in bytes per second (16 kbps). Fewer bytes per second would mean more
#: seconds per byte, so this is the side that makes the duration bound safe.
STT_FLOOR_BYTES_PER_SECOND = 2_000


def tts_cost_bound_usd(provider: str, characters: int) -> float | None:
    """The most a TTS call of `characters` can cost on `provider`; None for a provider with no known bound."""
    if provider == "elevenlabs":
        return characters / 1_000 * ELEVENLABS_TTS_USD_PER_1K_CHARS
    if provider == "gemini":
        input_tokens = GEMINI_INPUT_TOKENS_FIXED + characters * GEMINI_INPUT_TOKENS_PER_CHAR
        audio_tokens = GEMINI_AUDIO_TOKENS_FIXED + characters * GEMINI_AUDIO_TOKENS_PER_CHAR
        return (input_tokens * GEMINI_TTS_INPUT_USD_PER_1M_TOKENS
                + audio_tokens * GEMINI_TTS_OUTPUT_USD_PER_1M_TOKENS) / 1_000_000
    return None


def stt_cost_bound_usd(provider: str, audio_bytes: int) -> float | None:
    """The most transcribing `audio_bytes` of audio can cost on `provider`; None when there is no known bound."""
    seconds = audio_bytes / STT_FLOOR_BYTES_PER_SECOND
    if provider == "elevenlabs":
        return seconds / 3600 * ELEVENLABS_STT_USD_PER_HOUR
    if provider == "groq":
        return max(GROQ_STT_MINIMUM_SECONDS, seconds) / 3600 * GROQ_STT_USD_PER_HOUR
    if provider == "whisper_local":
        return 0.0  # runs on this machine; no API is called
    return None


def tcu_bound(cost_usd: float | None) -> float | None:
    return None if cost_usd is None else calculate_tcu(cost_usd)


R = TypeVar("R")


def affordable(chain: Sequence[R], bound_tcu: Callable[[R], float | None], ceiling_tcu: float) -> list[tuple[R, float]]:
    """The providers of a fallback chain whose worst case fits under `ceiling_tcu`, in chain order, with that worst
    case. A provider with no known bound is left out: running it would be spending an unknown amount."""
    kept = []
    for provider in chain:
        bound = bound_tcu(provider)
        if bound is not None and bound <= ceiling_tcu:
            kept.append((provider, bound))
    return kept


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def longest_affordable_text(text: str, fits: Callable[[int], bool]) -> str:
    """The longest leading run of whole sentences whose length `fits`; "" when not even the first one does.

    A long answer on a small plan is read from the start up to what the plan allows, ending on a sentence, instead
    of being refused outright — the response says it was shortened (routes/voice.py), so nothing is hidden.
    """
    kept = ""
    for sentence in _SENTENCE_END.split(text.strip()):
        candidate = f"{kept} {sentence}".strip()
        if not fits(len(candidate)):
            break
        kept = candidate
    return kept


def settle_tcu(actual_tcu: float, reserved_tcu: float, *, what: str) -> float:
    """What the person is charged: the real cost, never more than was reserved for it.

    The reservation is an upper bound computed before the call, so the real cost cannot exceed it unless a price or a
    bound in this module is wrong. If that ever happens the provider's overage is the product's to absorb, not the
    person's, and it is logged loudly so the bound gets fixed.
    """
    if actual_tcu > reserved_tcu + 1e-9:
        logger.error("voice budget: %s cost %.4f TCU over its reserved bound %.4f — charged the bound; fix the bound",
                     what, actual_tcu, reserved_tcu)
        return reserved_tcu
    return actual_tcu
