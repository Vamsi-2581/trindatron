"""
Integration tests for the voice API router (STT/TTS) — Voice Fase II
(Gemini default TTS provider + mandatory billing).

Uses the conftest.py fixtures:
  db            — SQLite in-memory session (per function)
  client        — TestClient(app) with get_db overridden
  registered_user / auth_headers — authenticated user via /auth/register

Test matrix
-----------
AUTH GATE
  V01) Missing auth header -> 401 on /voice/transcribe
  V02) Missing auth header -> 401 on /voice/synthesize

NOT CONFIGURED (no keys in test env — default registry from lifespan)
  V03) /voice/synthesize -> 424, names GEMINI_API_KEY (new TTS default)
  V04) /voice/transcribe -> 424, names ELEVENLABS_API_KEY (unchanged STT default)

PROVIDER SELECTION
  V05) Gemini is the default TTS provider — happy path uses it, correct cost/tokens
  V06) ElevenLabs remains selectable for TTS via VOICE_PROVIDER override
  V07) ElevenLabs remains the default STT provider — happy path uses it
  V08) Unregistered provider name -> 503

BILLING (check_and_reserve -> provider -> finalize_usage / cancel_reservation)
  V09) Successful synthesize creates exactly one completed UsageRecord with
       correct provider:operation:model label, cost_usd, tcu, tokens=0 (ElevenLabs)
  V10) Successful synthesize via Gemini records real input/output tokens
  V11) Successful transcribe creates a completed UsageRecord costed by duration
  V12) Entitlement limit exceeded -> 429, provider never called, no new UsageRecord
  V13) Provider failure -> reservation cancelled (status=failed, tcu=0), no charge
  V14) No double billing: exactly one UsageRecord per call, pending count stays sane

VALIDATION
  V15) /voice/transcribe with unsupported content-type -> 415
  V16) /voice/transcribe with empty file -> 400
  V17) /voice/synthesize with empty text -> 422

NO SECRETS LEAKED
  V18) Response bodies never contain the configured API key
"""
from __future__ import annotations

import io

import pytest

from src.api.deps import get_voice_registry
from src.capability.multimodal import (
    SynthesisResult,
    TranscriptionResult,
    VoiceProvider,
    VoiceProviderNotConfigured,
    VoiceSynthesisFailed,
    VoiceTranscriptionFailed,
)
from src.config import settings
from src.main import app
from src.models.usage import UsageRecord
from src.models.user import User
from src.voice.registry import VoiceProviderRegistry


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeGeminiProvider(VoiceProvider):
    """Deterministic fake mirroring GeminiVoiceProvider's real contract:
    TTS returns real-looking token metadata; STT is unsupported."""

    def __init__(self, fail: Exception | None = None):
        self._fail = fail

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        if self._fail:
            raise self._fail
        return SynthesisResult(
            audio=b"GEMINI-WAV-BYTES",
            content_type="audio/wav",
            voice_id=voice_id or "Kore",
            metadata={"input_tokens": 10, "output_tokens": 200},
        )

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        raise VoiceTranscriptionFailed("gemini-2.5-flash-preview-tts does not support speech-to-text")


class FakeElevenLabsProvider(VoiceProvider):
    """Deterministic fake mirroring ElevenLabsVoiceProvider's real contract."""

    def __init__(self, fail: Exception | None = None):
        self._fail = fail

    @property
    def provider_name(self) -> str:
        return "elevenlabs"

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        if self._fail:
            raise self._fail
        return SynthesisResult(audio=b"FAKE-AUDIO-BYTES", content_type="audio/mpeg", voice_id=voice_id or "default-voice")

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        if self._fail:
            raise self._fail
        return TranscriptionResult(text="transcrição de teste", language="pt", duration_seconds=2.0, signals=None)


def _install_fake_registry(*, gemini_fail=None, elevenlabs_fail=None) -> VoiceProviderRegistry:
    reg = VoiceProviderRegistry()
    reg.register(FakeGeminiProvider(fail=gemini_fail))
    reg.register(FakeElevenLabsProvider(fail=elevenlabs_fail))
    app.dependency_overrides[get_voice_registry] = lambda: reg
    return reg


def _clear_voice_override():
    app.dependency_overrides.pop(get_voice_registry, None)


def _audio_file(name="clip.webm", content_type="audio/webm", data=b"\x00\x01\x02fake-audio"):
    return {"file": (name, io.BytesIO(data), content_type)}


@pytest.fixture
def voice_fakes():
    """Install both fakes for the duration of one test, restore after."""
    reg = _install_fake_registry()
    try:
        yield reg
    finally:
        _clear_voice_override()


@pytest.fixture
def set_plan(db):
    """Set the given user's plan directly (mirrors test_entitlement.py's plan_user)."""
    def _set(user_id: str, plan: str):
        user = db.query(User).filter(User.id == user_id).first()
        user.plan = plan
        db.commit()
    return _set


# ── Auth gate ────────────────────────────────────────────────────────────


def test_transcribe_requires_auth(client):
    resp = client.post("/voice/transcribe", files=_audio_file())
    assert resp.status_code == 401


def test_synthesize_requires_auth(client):
    resp = client.post("/voice/synthesize", json={"text": "olá"})
    assert resp.status_code == 401


# ── Not configured (default lifespan registry, no API keys in test env) ────


def test_synthesize_without_api_key_returns_424_gemini(client, auth_headers):
    """New default TTS provider is gemini — the missing var must reflect that.

    Installs fakes that raise VoiceProviderNotConfigured directly rather than
    relying on the ambient .env lacking a real key — the real
    VoiceProviderRegistry is built once at app startup from whatever
    GEMINI_API_KEY/ELEVENLABS_API_KEY happen to be configured in this dev
    environment (which may hold real keys for other, unrelated live-provider
    validation work), so this "not configured" path must be forced
    explicitly to stay correct regardless of what's actually in .env.
    """
    # Both adapters unconfigured: TTS fails over, so a terminal 424 only
    # happens when there is nowhere left to go.
    _install_fake_registry(
        gemini_fail=VoiceProviderNotConfigured("GEMINI_API_KEY"),
        elevenlabs_fail=VoiceProviderNotConfigured("ELEVENLABS_API_KEY"),
    )
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code == 424
        # Which variable is missing is an operator concern (logged); the user
        # sees neither a vendor nor a configuration name.
        detail = resp.json()["detail"]
        assert "API_KEY" not in detail and "GEMINI" not in detail.upper()
    finally:
        _clear_voice_override()


def test_transcribe_without_api_key_returns_424_elevenlabs(client, auth_headers):
    """STT default provider is still elevenlabs — unaffected by the TTS default flip.

    Same rationale as the Gemini test above — forces the "not configured"
    path via a fake rather than depending on the ambient .env.
    """
    _install_fake_registry(elevenlabs_fail=VoiceProviderNotConfigured("ELEVENLABS_API_KEY"))
    try:
        resp = client.post("/voice/transcribe", files=_audio_file(), headers=auth_headers)
        assert resp.status_code == 424
        # The missing key is logged for operators; the user sees no vendor or variable.
        detail = resp.json()["detail"]
        assert "ELEVENLABS" not in detail.upper() and "API_KEY" not in detail
    finally:
        _clear_voice_override()


# ── Provider selection ──────────────────────────────────────────────────────


def test_synthesize_uses_gemini_by_default(client, auth_headers, voice_fakes):
    resp = client.post("/voice/synthesize", json={"text": "olá mundo"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == b"GEMINI-WAV-BYTES"
    assert resp.headers["content-type"].startswith("audio/wav")


def test_synthesize_can_select_elevenlabs_explicitly(client, auth_headers, voice_fakes, monkeypatch):
    monkeypatch.setattr(settings, "voice_provider", "elevenlabs")
    resp = client.post("/voice/synthesize", json={"text": "olá mundo"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == b"FAKE-AUDIO-BYTES"
    assert resp.headers["content-type"].startswith("audio/mpeg")


def test_transcribe_uses_elevenlabs_by_default(client, auth_headers, voice_fakes):
    resp = client.post("/voice/transcribe", files=_audio_file(), headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["text"] == "transcrição de teste"


def test_synthesize_unregistered_provider_returns_503(client, auth_headers, voice_fakes, monkeypatch):
    monkeypatch.setattr(settings, "voice_provider", "openai")  # never registered
    resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
    assert resp.status_code == 503


# ── Billing ──────────────────────────────────────────────────────────────


def test_synthesize_elevenlabs_creates_usage_record_with_char_cost(client, auth_headers, registered_user, voice_fakes, monkeypatch, db):
    monkeypatch.setattr(settings, "voice_provider", "elevenlabs")
    text = "a" * 1000  # exactly 1000 chars -> $0.10 at $0.10/1k chars
    resp = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert resp.status_code == 200

    records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(records) == 1
    record = records[0]
    assert record.status == "completed"
    assert record.model == f"elevenlabs:tts:{settings.elevenlabs_tts_model_id}"
    assert record.input_tokens == 0 and record.output_tokens == 0
    assert float(record.cost_usd) == pytest.approx(0.10, abs=1e-6)
    assert float(record.tcu) == pytest.approx(10.0, abs=1e-4)  # 1 TCU = $0.01


def test_synthesize_gemini_records_real_tokens_and_cost(client, auth_headers, registered_user, voice_fakes, db):
    resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
    assert resp.status_code == 200

    records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(records) == 1
    record = records[0]
    assert record.model == f"gemini:tts:{settings.gemini_tts_model_id}"
    assert record.input_tokens == 10
    assert record.output_tokens == 200
    # 10/1e6*0.50 + 200/1e6*10.00 = 0.000005 + 0.002 = 0.002005
    assert float(record.cost_usd) == pytest.approx(0.002005, abs=1e-8)
    assert float(record.tcu) == pytest.approx(0.2005, abs=1e-6)


def test_transcribe_creates_usage_record_costed_by_duration(client, auth_headers, registered_user, voice_fakes, db):
    resp = client.post("/voice/transcribe", files=_audio_file(), headers=auth_headers)
    assert resp.status_code == 200

    records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(records) == 1
    record = records[0]
    assert record.status == "completed"
    assert record.model == f"elevenlabs:stt:{settings.elevenlabs_stt_model_id}"
    # duration_seconds=2.0 -> hours = 2/3600, cost = hours * 0.22
    expected_cost = (2.0 / 3600.0) * 0.22
    assert float(record.cost_usd) == pytest.approx(expected_cost, abs=1e-8)


def test_entitlement_blocks_synthesize_before_provider_call(client, auth_headers, registered_user, voice_fakes, set_plan, db):
    from src.entitlements import get_plan_entitlement
    from src.services.entitlement_service import _day_window

    set_plan(registered_user["user_id"], "free")
    entitlement = get_plan_entitlement("free")

    # Seed at the daily cap so the next reservation is refused outright.
    day_start, _ = _day_window()
    seed = UsageRecord(
        user_id=registered_user["user_id"], session_id=None, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        # Seeded to the daily ceiling itself. Under the ADMIT rule
        # (2026-09-11) a window with balance left admits a clamped request
        # instead of refusing it, so 'one per-request estimate short of the
        # cap' no longer blocks — reaching the cap is what blocks. The
        # invariant under test is unchanged.
        cost_usd=0.0, tcu=entitlement.daily_tcu_limit,
        duration_ms=0, status="completed", created_at=day_start.replace(hour=12),
    )
    db.add(seed)
    db.commit()

    resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
    assert resp.status_code == 429

    # Only the seed row exists — no new (pending or completed) row was created.
    records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(records) == 1
    assert records[0].id == seed.id


def test_provider_failure_cancels_reservation_no_charge(client, auth_headers, registered_user, db):
    reg = _install_fake_registry(
        gemini_fail=VoiceSynthesisFailed("upstream exploded"),
        elevenlabs_fail=VoiceSynthesisFailed("also exploded"),
    )
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code == 502

        records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
        assert len(records) == 1
        record = records[0]
        assert record.status == "failed"
        assert float(record.tcu) == 0.0
    finally:
        _clear_voice_override()


def test_provider_not_configured_cancels_reservation_no_charge(client, auth_headers, registered_user, db):
    reg = _install_fake_registry(
        gemini_fail=VoiceProviderNotConfigured("GEMINI_API_KEY"),
        elevenlabs_fail=VoiceProviderNotConfigured("ELEVENLABS_API_KEY"),
    )
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code == 424

        records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
        assert len(records) == 1
        assert records[0].status == "failed"
        assert float(records[0].tcu) == 0.0
    finally:
        _clear_voice_override()


def test_no_double_billing_across_two_calls(client, auth_headers, registered_user, voice_fakes, db):
    client.post("/voice/synthesize", json={"text": "primeira"}, headers=auth_headers)
    client.post("/voice/synthesize", json={"text": "segunda"}, headers=auth_headers)

    records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(records) == 2
    assert all(r.status == "completed" for r in records)
    # Each call billed independently — no accumulation/double counting in one record.
    assert records[0].cost_usd != 0 and records[1].cost_usd != 0


# ── Validation ──────────────────────────────────────────────────────────


def test_transcribe_rejects_unsupported_content_type(client, auth_headers, voice_fakes):
    resp = client.post(
        "/voice/transcribe",
        files=_audio_file(name="clip.txt", content_type="text/plain"),
        headers=auth_headers,
    )
    assert resp.status_code == 415


def test_transcribe_rejects_empty_file(client, auth_headers, voice_fakes):
    resp = client.post("/voice/transcribe", files=_audio_file(data=b""), headers=auth_headers)
    assert resp.status_code == 400


def test_synthesize_rejects_empty_text(client, auth_headers):
    resp = client.post("/voice/synthesize", json={"text": ""}, headers=auth_headers)
    assert resp.status_code == 422


# ── No secrets leaked ──────────────────────────────────────────────────────


def test_error_response_never_contains_api_key(client, auth_headers):
    """CP8: this used to hit the real, unmocked Gemini provider and rely on
    whatever error a live (and now network-guarded) call happened to produce —
    the same accidental-real-call hazard CP8 closes everywhere else. Both
    providers now fail deterministically instead, the same way every other
    failure-path test in this file already does (_install_fake_registry)."""
    _install_fake_registry(
        gemini_fail=VoiceProviderNotConfigured("GEMINI_API_KEY"),
        elevenlabs_fail=VoiceProviderNotConfigured("ELEVENLABS_API_KEY"),
    )
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code >= 400
        assert "sk-" not in resp.text
        assert "xi-api-key" not in resp.text.lower()
        assert "x-goog-api-key" not in resp.text.lower()
    finally:
        _clear_voice_override()


# ── TTS provider failover ───────────────────────────────────────────────────
#
# Production, 2026-09-10: Gemini TTS began returning
# "429 You exceeded your current quota" and read-aloud died, while a fully
# configured second adapter sat unused in the registry. These pin the
# behaviour that fixes it.


def test_synthesize_falls_over_when_the_primary_is_out_of_quota(client, auth_headers):
    """The exact production failure: primary errors, request still succeeds."""
    _install_fake_registry(gemini_fail=VoiceSynthesisFailed("Gemini TTS returned 429: quota exceeded"))
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/")
        assert len(resp.content) > 0
    finally:
        _clear_voice_override()


def test_synthesize_falls_over_when_the_primary_is_unconfigured(client, auth_headers):
    """A missing key on one vendor must not take the feature down."""
    _install_fake_registry(gemini_fail=VoiceProviderNotConfigured("GEMINI_API_KEY"))
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        assert resp.status_code == 200
    finally:
        _clear_voice_override()


def test_failover_bills_the_provider_that_actually_delivered(
    client, auth_headers, registered_user, db,
):
    """One reservation, one charge, attributed to whoever produced the audio —
    a fallback must never double-bill or misattribute."""
    _install_fake_registry(gemini_fail=VoiceSynthesisFailed("429 quota"))
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá mundo"}, headers=auth_headers)
        assert resp.status_code == 200

        records = db.query(UsageRecord).filter(
            UsageRecord.user_id == registered_user["user_id"],
        ).all()
        assert len(records) == 1, "a fallback must not create a second usage record"
        record = records[0]
        assert record.status == "completed"
        assert record.model.startswith("elevenlabs:tts:"), record.model
        assert float(record.tcu) > 0
    finally:
        _clear_voice_override()


def test_healthy_primary_is_never_bypassed(client, auth_headers, registered_user, voice_fakes, db):
    """Failover is a fallback, not a load balancer: when the configured
    provider works it is the only one called, and the one billed."""
    resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
    assert resp.status_code == 200

    records = db.query(UsageRecord).filter(
        UsageRecord.user_id == registered_user["user_id"],
    ).all()
    assert len(records) == 1
    assert records[0].model.startswith("gemini:tts:"), records[0].model


def test_failover_error_never_leaks_a_key(client, auth_headers):
    """Whatever the last provider said reaches the client — it must still be
    scrubbed of anything secret."""
    _install_fake_registry(
        gemini_fail=VoiceSynthesisFailed("boom"),
        elevenlabs_fail=VoiceSynthesisFailed("boom"),
    )
    try:
        resp = client.post("/voice/synthesize", json={"text": "olá"}, headers=auth_headers)
        body = resp.text
        assert resp.status_code == 502
        for secret in (settings.gemini_api_key, settings.elevenlabs_api_key):
            if secret:
                assert secret not in body
    finally:
        _clear_voice_override()


# ── The codec parameter the browser always sends ──────────────────────────
#
# MediaRecorder reports its mimeType with the codec attached, and the web
# composer passes that straight through as the blob's type. The route used to
# compare the whole header against the allowlist, so every real browser
# recording came back 415 "Unsupported audio content type: audio/webm;
# codecs=opus" — while the same bytes labelled plainly as "audio/webm" were
# accepted. A media type's parameters do not change its type (RFC 9110), so
# the fix is to read the type, not the whole header.

@pytest.mark.parametrize("content_type", [
    "audio/webm;codecs=opus",
    "audio/webm; codecs=opus",
    "audio/ogg;codecs=opus",
    "AUDIO/WEBM;codecs=opus",
    "audio/mp4;codecs=mp4a.40.2",
])
def test_transcribe_accepts_browser_codec_parameters(
    client, auth_headers, voice_fakes, content_type,
):
    resp = client.post(
        "/voice/transcribe",
        files=_audio_file(name="clip.webm", content_type=content_type),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


def test_transcribe_still_rejects_a_type_we_do_not_support():
    """The parameter fix must not have turned the allowlist into a doormat."""
    from src.api.routes.voice import _ALLOWED_CONTENT_TYPES, _normalize_media_type

    assert _normalize_media_type("audio/x-aiff;rate=44100") == "audio/x-aiff"
    assert "audio/x-aiff" not in _ALLOWED_CONTENT_TYPES
    # And a missing header still falls back to the browser's usual container.
    assert _normalize_media_type(None) in _ALLOWED_CONTENT_TYPES
