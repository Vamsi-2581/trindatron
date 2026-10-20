"""A voice request never spends more than its plan allows one request (src/voice/budget.py).

Reproduces what a physical phone showed: a Free account (2.0 TCU per request) read an answer aloud, Gemini failed,
ElevenLabs ran, and the settlement recorded 4.34 TCU. Only the providers are fakes; the routes, the plan limits, the
reservation and the settlement are the real ones.
"""
from __future__ import annotations

import io

import pytest

from src.api.deps import get_voice_registry
from src.capability.multimodal import SynthesisResult, TranscriptionResult, VoiceProvider, VoiceSynthesisFailed
from src.config import settings
from src.entitlements import get_plan_entitlement
from src.main import app
from src.models.usage import UsageRecord
from src.models.user import User
from src.voice.budget import (
    affordable,
    longest_affordable_text,
    settle_tcu,
    stt_cost_bound_usd,
    tcu_bound,
    tts_cost_bound_usd,
)
from src.voice.registry import VoiceProviderRegistry

FREE_CEILING = get_plan_entitlement("free").max_tcu_per_request


class Recording(VoiceProvider):
    """A provider that records what it was asked, fails on demand and reports the tokens it is told to."""

    def __init__(self, name: str, *, fail: bool = False, tokens: tuple[int, int] | None = None, seconds: float = 2.0):
        self._name, self._fail, self._tokens, self._seconds = name, fail, tokens, seconds
        self.texts: list[str] = []
        self.transcribed = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs) -> SynthesisResult:
        self.texts.append(text)
        if self._fail:
            raise VoiceSynthesisFailed(f"{self._name} failed")
        if self._tokens is None:  # a Gemini-shaped reading within its bound
            characters = len(text)
            metadata = {"input_tokens": 7 + characters // 4, "output_tokens": 60 + characters * 2}
        else:
            metadata = {"input_tokens": self._tokens[0], "output_tokens": self._tokens[1]}
        return SynthesisResult(audio=b"AUDIO", content_type="audio/wav", voice_id="v",
                               metadata=metadata if self._name == "gemini" else {})

    async def transcribe(self, audio: bytes, *, content_type: str = "audio/webm", **kwargs) -> TranscriptionResult:
        self.transcribed += 1
        if self._fail:
            raise VoiceSynthesisFailed(f"{self._name} failed")
        return TranscriptionResult(text="ok", language="pt", duration_seconds=self._seconds, signals=None)


@pytest.fixture
def providers():
    installed: dict[str, Recording] = {}

    def install(*fakes: Recording):
        registry = VoiceProviderRegistry()
        for fake in fakes:
            registry.register(fake)
            installed[fake.provider_name] = fake
        app.dependency_overrides[get_voice_registry] = lambda: registry
        return installed

    yield install
    app.dependency_overrides.pop(get_voice_registry, None)


@pytest.fixture
def plan(db, registered_user):
    def _set(name: str):
        db.query(User).filter(User.id == registered_user["user_id"]).update({"plan": name})
        db.commit()
    return _set


def _charged(db, user_id: str) -> list[UsageRecord]:
    db.expire_all()
    return db.query(UsageRecord).filter(UsageRecord.user_id == user_id).all()


def _sentences(count: int) -> str:
    return " ".join(f"Esta é a frase número {i} da resposta, lida em voz alta para o teste." for i in range(count))


# ── The bounds themselves ─────────────────────────────────────────────────

def test_elevenlabs_bound_is_the_published_price_and_explains_the_measured_charge():
    # 434 characters at $0.10 per 1,000 is $0.0434 — the 4.34 TCU the phone recorded.
    assert tcu_bound(tts_cost_bound_usd("elevenlabs", 434)) == pytest.approx(4.34)


def test_gemini_bound_covers_every_stored_reading():
    # (characters ~ 4 per input token, input tokens, audio tokens) from the dev database's gemini TTS records.
    for input_tokens, audio_tokens in ((7, 69), (7, 66), (7, 70), (7, 57), (12, 91), (45, 330)):
        characters = max(1, (input_tokens - 7) * 4)
        real = (input_tokens * 0.50 + audio_tokens * 10.00) / 1_000_000
        assert tts_cost_bound_usd("gemini", characters) >= real


def test_unknown_providers_have_no_bound_and_are_never_affordable():
    assert tts_cost_bound_usd("someone-new", 10) is None
    assert affordable(["someone-new"], lambda p: tcu_bound(tts_cost_bound_usd(p, 10)), 1_000) == []


def test_stt_bound_grows_with_the_upload():
    assert stt_cost_bound_usd("elevenlabs", 4_000_000) > stt_cost_bound_usd("elevenlabs", 40_000)
    assert stt_cost_bound_usd("groq", 1) > 0  # the 10 s minimum


def test_settlement_never_exceeds_the_reservation():
    assert settle_tcu(1.5, 2.0, what="t") == 1.5
    assert settle_tcu(2.0, 2.0, what="t") == 2.0
    assert settle_tcu(4.34, 2.0, what="t") == 2.0


def test_shortening_keeps_whole_sentences_from_the_start():
    text = "Primeira frase. Segunda frase. Terceira frase."
    assert longest_affordable_text(text, lambda n: n <= len("Primeira frase. Segunda frase.")) == "Primeira frase. Segunda frase."
    assert longest_affordable_text(text, lambda n: n < len("Primeira frase.")) == ""


# ── Through the real routes ───────────────────────────────────────────────

def test_below_the_limit_runs_and_charges_the_real_cost(client, auth_headers, registered_user, providers, plan, db):
    plan("free")
    fakes = providers(Recording("gemini"), Recording("elevenlabs"))
    response = client.post("/voice/synthesize", json={"text": "Olá, tudo bem?"}, headers=auth_headers)
    assert response.status_code == 200
    assert "X-Voice-Truncated" not in response.headers
    (record,) = _charged(db, registered_user["user_id"])
    assert record.status == "completed" and float(record.tcu) <= FREE_CEILING
    assert fakes["elevenlabs"].texts == []


def test_exactly_at_the_limit_is_allowed(client, auth_headers, registered_user, providers, plan, db, monkeypatch):
    plan("free")
    text = "Olá, tudo bem?"
    exact = tcu_bound(tts_cost_bound_usd("gemini", len(text)))
    monkeypatch.setattr("src.api.routes.voice._request_ceiling_tcu", lambda _plan: exact)
    fakes = providers(Recording("gemini"), Recording("elevenlabs"))
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200
    assert fakes["gemini"].texts == [text]


def test_the_measured_case_reads_only_what_the_fallback_can_afford(client, auth_headers, registered_user, providers, plan,
                                                                    db, monkeypatch):
    """Free, a 434-character answer, Gemini down. The whole text on ElevenLabs is the 4.34 TCU the phone recorded; now
    ElevenLabs reads only the sentences that fit in 2.0 TCU, and the response says the reading was shortened."""
    plan("free")
    monkeypatch.setattr(settings, "voice_provider", "gemini")
    fakes = providers(Recording("gemini", fail=True), Recording("elevenlabs"))
    text = ("x" * 99 + ". ") * 4 + "x" * 30  # 434 characters in five sentences
    assert len(text) == 434
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["X-Voice-Truncated"] == "1"
    (read,) = fakes["elevenlabs"].texts
    assert text.startswith(read) and len(read) <= 200
    (record,) = _charged(db, registered_user["user_id"])
    assert float(record.tcu) <= FREE_CEILING
    assert float(record.tcu) == pytest.approx(len(read) / 100)


def test_a_fallback_with_nothing_it_can_afford_is_never_called(client, auth_headers, registered_user, providers, plan, db,
                                                               monkeypatch):
    """No sentence of the text fits ElevenLabs on Free (one 300-character sentence): Gemini down means an error and
    no charge — never a reading over the ceiling."""
    plan("free")
    monkeypatch.setattr(settings, "voice_provider", "gemini")
    fakes = providers(Recording("gemini", fail=True), Recording("elevenlabs"))
    response = client.post("/voice/synthesize", json={"text": "y" * 300}, headers=auth_headers)
    assert response.status_code >= 400
    assert fakes["elevenlabs"].texts == []
    # The reservation is released, not settled: nothing is charged.
    assert [r.status for r in _charged(db, registered_user["user_id"])] == ["failed"]


def test_first_provider_fails_second_succeeds_within_the_limit(client, auth_headers, registered_user, providers, plan, db,
                                                               monkeypatch):
    plan("free")
    monkeypatch.setattr(settings, "voice_provider", "gemini")
    fakes = providers(Recording("gemini", fail=True), Recording("elevenlabs"))
    text = "Uma resposta curta."
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200
    assert fakes["elevenlabs"].texts == [text]
    (record,) = _charged(db, registered_user["user_id"])
    assert record.model.startswith("elevenlabs:tts") and float(record.tcu) == pytest.approx(len(text) / 100)  # $0.10 per 1,000 characters


def test_above_the_limit_a_long_answer_is_read_up_to_the_last_sentence_that_fits(client, auth_headers, registered_user,
                                                                                 providers, plan, db):
    plan("free")
    fakes = providers(Recording("gemini"), Recording("elevenlabs"))
    text = _sentences(30)  # ~2,000 characters: far over a Free request with either provider
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["X-Voice-Truncated"] == "1"
    (read,) = fakes["gemini"].texts
    assert text.startswith(read) and read.endswith(".") and len(read) < len(text)
    assert tcu_bound(tts_cost_bound_usd("gemini", len(read))) <= FREE_CEILING
    (record,) = _charged(db, registered_user["user_id"])
    assert float(record.tcu) <= FREE_CEILING


def test_above_the_limit_with_no_sentence_that_fits_is_refused_before_any_provider(client, auth_headers, registered_user,
                                                                                   providers, plan, db):
    plan("free")
    fakes = providers(Recording("gemini"), Recording("elevenlabs"))
    response = client.post("/voice/synthesize", json={"text": "x" * 2_000}, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "voice_request_too_large"
    assert fakes["gemini"].texts == [] and fakes["elevenlabs"].texts == []
    assert _charged(db, registered_user["user_id"]) == []


def test_a_bigger_plan_reads_the_same_long_answer_whole(client, auth_headers, registered_user, providers, plan):
    plan("max")
    fakes = providers(Recording("gemini"), Recording("elevenlabs"))
    text = _sentences(30)
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200 and "X-Voice-Truncated" not in response.headers
    assert fakes["gemini"].texts == [text]


def test_a_provider_that_reports_more_than_its_bound_is_charged_the_bound(client, auth_headers, registered_user,
                                                                          providers, plan, db):
    plan("free")
    providers(Recording("gemini", tokens=(10, 90_000)), Recording("elevenlabs"))
    text = "Olá."
    response = client.post("/voice/synthesize", json={"text": text}, headers=auth_headers)
    assert response.status_code == 200
    (record,) = _charged(db, registered_user["user_id"])
    assert float(record.tcu) == pytest.approx(tcu_bound(tts_cost_bound_usd("gemini", len(text))))


def test_transcription_too_long_for_the_plan_is_refused_before_any_provider(client, auth_headers, registered_user,
                                                                             providers, plan, db):
    plan("free")
    fakes = providers(Recording("elevenlabs"), Recording("groq"))
    big = io.BytesIO(b"\x00" * (20 * 1024 * 1024))  # 20 MB: hours of audio at the floor bitrate
    response = client.post("/voice/transcribe", files={"file": ("clip.webm", big, "audio/webm")}, headers=auth_headers)
    assert response.status_code == 422
    assert fakes["elevenlabs"].transcribed == 0 and fakes["groq"].transcribed == 0
    assert _charged(db, registered_user["user_id"]) == []


def test_transcription_skips_a_transcriber_that_would_cost_too_much(client, auth_headers, registered_user, providers,
                                                                   plan, db, monkeypatch):
    """A recording ElevenLabs could only bill over the Free ceiling goes to the cheaper transcriber, never to it."""
    plan("free")
    monkeypatch.setattr(settings, "voice_provider_stt", "elevenlabs")
    fakes = providers(Recording("elevenlabs"), Recording("groq"))
    size = 1_000_000  # 500 s at the floor bitrate: ~3.1 TCU on ElevenLabs, ~0.6 on Groq
    assert tcu_bound(stt_cost_bound_usd("elevenlabs", size)) > FREE_CEILING >= tcu_bound(stt_cost_bound_usd("groq", size))
    response = client.post("/voice/transcribe", files={"file": ("clip.webm", io.BytesIO(b"\x00" * size), "audio/webm")},
                           headers=auth_headers)
    assert response.status_code == 200
    assert fakes["elevenlabs"].transcribed == 0 and fakes["groq"].transcribed == 1
    (record,) = _charged(db, registered_user["user_id"])
    assert float(record.tcu) <= FREE_CEILING
