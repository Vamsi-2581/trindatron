"""
Voice schemas — request/response models for the /voice API.

Emotion/prosody fields are always present on TranscribeResponse but default
to None. They are NOT computed here — they only carry through whatever a
provider actually returns via TranscriptionResult.signals. No detector is
implemented; see src/capability/multimodal.py ProsodySignals for the contract.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SynthesizeRequest(BaseModel):
    """Request body for POST /voice/synthesize."""

    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str | None = Field(
        default=None,
        description="Overrides the server-configured default voice for this call only.",
    )


class TranscribeResponse(BaseModel):
    """Response body for POST /voice/transcribe."""

    text: str
    language: str | None = None
    duration_seconds: float | None = None

    # Prosody/emotion placeholders — structure only, never fabricated.
    emotion: str | None = None
    confidence: float | None = None
    pace: float | None = None
    urgency: float | None = None
    prosody: dict | None = None
