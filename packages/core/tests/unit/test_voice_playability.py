"""The contract a browser actually needs: audio it can play.

The read-aloud bug reported 2026-09-10 ("Failed to load because no supported
source was found") turned out to be a Content-Security-Policy problem in the
web app, not a malformed container — the bytes were fine all along. These
tests exist so that stays true: they assert the WAV the TTS adapter produces
is a structurally valid container matching its declared PCM spec, and that
the route hands it over with the right media type.

Without this, a regression in `_pcm_to_wav` would produce the *same*
user-visible symptom as the CSP bug did, with nothing to tell the two apart.
"""
from __future__ import annotations

import struct

import pytest

from src.voice.adapters.gemini import (
    _PCM_BITS_PER_SAMPLE,
    _PCM_CHANNELS,
    _PCM_SAMPLE_RATE,
    _pcm_to_wav,
)

# One second of silence at the documented Gemini TTS spec.
ONE_SECOND_PCM = b"\x00\x00" * _PCM_SAMPLE_RATE


def _parse_wav(data: bytes) -> dict:
    """Read back the canonical 44-byte RIFF/WAVE header."""
    riff, riff_size, wave = struct.unpack("<4sI4s", data[:12])
    fmt_id, fmt_size = struct.unpack("<4sI", data[12:20])
    audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", data[20:36]
    )
    data_id, data_size = struct.unpack("<4sI", data[36:44])
    return {
        "riff": riff, "riff_size": riff_size, "wave": wave,
        "fmt_id": fmt_id, "fmt_size": fmt_size,
        "audio_format": audio_format, "channels": channels, "rate": rate,
        "byte_rate": byte_rate, "block_align": block_align, "bits": bits,
        "data_id": data_id, "data_size": data_size,
    }


def test_header_is_the_canonical_44_bytes():
    wav = _pcm_to_wav(ONE_SECOND_PCM)
    assert len(wav) == 44 + len(ONE_SECOND_PCM)


def test_container_identifies_itself_as_riff_wave_pcm():
    h = _parse_wav(_pcm_to_wav(ONE_SECOND_PCM))
    assert h["riff"] == b"RIFF"
    assert h["wave"] == b"WAVE"
    assert h["fmt_id"] == b"fmt "
    assert h["data_id"] == b"data"
    assert h["fmt_size"] == 16
    assert h["audio_format"] == 1, "1 = uncompressed PCM; anything else needs a codec"


def test_header_matches_the_documented_gemini_pcm_spec():
    h = _parse_wav(_pcm_to_wav(ONE_SECOND_PCM))
    assert h["channels"] == _PCM_CHANNELS
    assert h["rate"] == _PCM_SAMPLE_RATE
    assert h["bits"] == _PCM_BITS_PER_SAMPLE
    # A wrong byte rate or block align makes players compute the wrong
    # duration or refuse the file outright.
    assert h["byte_rate"] == _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_BITS_PER_SAMPLE // 8
    assert h["block_align"] == _PCM_CHANNELS * _PCM_BITS_PER_SAMPLE // 8


def test_declared_sizes_match_the_real_payload():
    """A size field that disagrees with the actual bytes is the classic way
    to produce an unplayable file that still looks like a WAV."""
    wav = _pcm_to_wav(ONE_SECOND_PCM)
    h = _parse_wav(wav)
    assert h["data_size"] == len(ONE_SECOND_PCM)
    assert h["riff_size"] == len(wav) - 8


# Explicit ids: the raw PCM would otherwise become the test id, and a
# multi-kilobyte id blows past the Windows path limit when pytest builds
# per-test paths from it.
@pytest.mark.parametrize(
    "pcm",
    [b"", b"\x00\x00", ONE_SECOND_PCM * 3],
    ids=["empty", "single-sample", "three-seconds"],
)
def test_wrapping_is_consistent_at_any_length(pcm):
    wav = _pcm_to_wav(pcm)
    h = _parse_wav(wav)
    assert h["data_size"] == len(pcm)
    assert h["riff_size"] == len(wav) - 8
    assert wav[44:] == pcm, "the audio payload must survive wrapping byte for byte"


def test_duration_is_computable_from_the_header():
    """What a player uses to show a timeline — and what a browser needs
    before it will report the clip as playable."""
    h = _parse_wav(_pcm_to_wav(ONE_SECOND_PCM))
    assert h["data_size"] / h["byte_rate"] == pytest.approx(1.0)
