"""
Voice route — STT/TTS for the mic -> STT -> TRINITY CORE -> TTS flow.

Endpoints
---------
  POST /voice/transcribe  — audio (multipart) -> text
  POST /voice/synthesize  — text (JSON) -> audio bytes

Provider selection
-------------------
TTS and STT resolve INDEPENDENTLY (settings.voice_provider /
settings.voice_provider_stt) — not one shared pointer.

TTS additionally FAILS OVER. The configured provider is tried first and, if
it cannot deliver audio, every other registered TTS-capable adapter is tried
in turn. Observed in production 2026-09-10: Gemini TTS started returning
`429 You exceeded your current quota`, which surfaced to the user as a dead
"Ouvir" button even though a second, fully configured adapter was sitting in
the registry unused. A voice abstraction that cannot switch providers is
just a slower way to be locked to one.

The order is deliberately economic, not alphabetical: the configured default
is the cheap one and stays first, so a fallback only ever costs more when the
alternative is delivering nothing. Billing follows the provider that actually
produced the audio — the reservation is made once, and finalized against
whichever adapter succeeded (see synthesize() below). STT does not fail over:
only one registered adapter can transcribe today, so there is nowhere to go. Gemini 2.5 Flash
Preview TTS is a text-to-speech-ONLY model (confirmed against Google's own
docs; see voice/adapters/gemini.py) — it cannot serve /voice/transcribe.
Defaults: TTS -> gemini (cost-efficient default), STT -> elevenlabs (the
only registered adapter that can actually transcribe). Both adapters are
always registered (src/main.py lifespan); either route can be explicitly
pointed at either adapter via env var, as long as that adapter actually
supports the operation.

Security invariants
--------------------
* Both endpoints require an authenticated user (get_current_user).
* The provider API key lives only in backend settings — never accepted
  from the request, never echoed back in a response.
* Errors from the provider never leak the API key.

Billing
-------
Every call reserves TCU BEFORE touching a provider, then finalizes on
success or cancels on any failure — the exact check_and_reserve /
finalize_usage / cancel_reservation pattern already used by chat/agent
(src/services/entitlement_service.py). Cost is computed per the provider's
real billing unit (voice/cost.py) — never a flat/guessed number:
  Gemini TTS        -> real input/output token counts (usageMetadata)
  ElevenLabs TTS     -> characters of input text sent
  ElevenLabs STT     -> real audio duration processed (audio_duration_secs)
UsageRecord.model encodes "{provider}:{operation}:{model_id}" so usage is
attributable without a schema migration.

Error mapping
-------------
  VoiceProviderNotFound        -> 503 Service Unavailable (no adapter registered)
  VoiceProviderNotConfigured   -> 424 Failed Dependency (names the exact missing env var)
  VoiceSynthesisFailed         -> 502 Bad Gateway
  VoiceTranscriptionFailed     -> 502 Bad Gateway
  Entitlement limit exceeded   -> 429 Too Many Requests (raised by check_and_reserve)

Scope
-----
This does not touch the session/chat pipeline. The frontend is expected to
call /voice/transcribe to get text, then send that text through the normal
POST /sessions/{id}/message flow like any typed message, and optionally
call /voice/synthesize on the assistant's final answer to play it back.
That keeps the existing chat pipeline (SSE, capability routing, its own
billing reservation) completely unchanged.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user, get_voice_registry
from src.capability.multimodal import (
    VoiceProviderNotConfigured,
    VoiceSynthesisFailed,
    VoiceTranscriptionFailed,
)
from src.config import settings
from src.database import get_db
from src.models.user import User
from src.schemas.voice import SynthesizeRequest, TranscribeResponse
from src.services.audio_router import AudioRouter
from src.services.entitlement_service import cancel_reservation, check_and_reserve, finalize_usage
from src.voice.budget import affordable, longest_affordable_text, settle_tcu, stt_cost_bound_usd, tcu_bound, tts_cost_bound_usd
from src.voice.cost import calculate_voice_cost_usd
from src.voice.registry import VoiceProviderNotFound, VoiceProviderRegistry
from trinity_router.tcu import calculate_tcu

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

# Accepted audio containers from the browser MediaRecorder. Keeps the upload
# surface explicit instead of trusting an arbitrary client-supplied mimetype.
_ALLOWED_CONTENT_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
}
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — generous for a short voice turn


def _normalize_media_type(raw: str | None) -> str:
    """Reduce a Content-Type header to its bare media type.

    A media type may carry parameters — `audio/webm;codecs=opus` is the same
    type as `audio/webm`, per RFC 9110 — and browsers always send them here:
    MediaRecorder reports its mimeType with the codec attached, and the
    frontend passes that through as the blob's type.

    The check used to compare the whole header against the allowlist, so
    every real browser recording was rejected as an unsupported type while
    the same audio with a hand-written `audio/webm` header sailed through.
    Splitting the parameters off is the fix; widening the allowlist with
    codec permutations would only have postponed it.
    """
    if not raw:
        return "audio/webm"
    return raw.split(";")[0].strip().lower()


def _tts_provider_chain(registry: VoiceProviderRegistry) -> list[AudioRouter]:
    """TTS adapters to try, in order: configured provider, then the rest.

    Returns routers rather than raw adapters so the call site stays identical
    to the single-provider path it replaces. Order is stable (the registry
    sorts its names), so the fallback a request gets is predictable.
    """
    preferred = settings.voice_provider
    if not registry.is_registered(preferred):
        # A configured provider that was never registered is an operator
        # mistake, not a provider outage. Failing over would quietly serve a
        # different vendor than the deployment asked for, so this keeps the
        # explicit 503 the single-provider path always returned.
        raise VoiceProviderNotFound(preferred)
    names = [preferred] + [n for n in registry.names() if n != preferred]
    return [AudioRouter(registry.get(n)) for n in names]


#: Adapters that can transcribe. Gemini TTS is synthesis-only and is never tried for STT.
#: whisper_local is last on purpose (CP18): it only ever runs when registered (STT_LOCAL_ENABLED=true),
#: never becomes the preferred/first provider through this tuple's order alone, and costs $0.
STT_CAPABLE = ("elevenlabs", "groq", "whisper_local")


def _stt_provider_chain(registry: VoiceProviderRegistry) -> list[AudioRouter]:
    """STT adapters to try, in order: the configured transcriber, then every other one that can transcribe."""
    preferred = settings.voice_provider_stt
    if not registry.is_registered(preferred):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice is not available right now. Please try again later.",
        )
    names = [preferred] + [n for n in STT_CAPABLE if n != preferred and registry.is_registered(n)]
    return [AudioRouter(registry.get(n)) for n in names]


def _request_ceiling_tcu(plan: str) -> float:
    from src.entitlements import get_plan_entitlement

    return float(get_plan_entitlement(plan).max_tcu_per_request)


def _reserved_tcu(db: DBSession, reservation_id: str) -> float:
    """What check_and_reserve actually reserved — it clamps the estimate to the balance that remains."""
    from src.models.usage import UsageRecord

    return float(db.query(UsageRecord.tcu).filter(UsageRecord.id == reservation_id).scalar() or 0.0)


def _too_large(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                         detail={"error": "voice_request_too_large", "message": message})


def _no_balance(db: DBSession, reservation_id: str) -> HTTPException:
    cancel_reservation(db, reservation_id)
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                         detail={"error": "limit_exceeded",
                                 "message": "Não há saldo suficiente na sua janela de uso atual para este áudio."})


def _model_id_for(provider_name: str, operation: str) -> str:
    """Static model id per (provider, operation), from settings — no adapter round-trip."""
    if provider_name == "groq" and operation == "stt":
        return "whisper-large-v3-turbo"
    if provider_name == "whisper_local" and operation == "stt":
        return "whisper-local"
    if provider_name == "gemini" and operation == "tts":
        return settings.gemini_tts_model_id
    if provider_name == "elevenlabs" and operation == "tts":
        return settings.elevenlabs_tts_model_id
    if provider_name == "elevenlabs" and operation == "stt":
        return settings.elevenlabs_stt_model_id
    return "unknown"


def _map_voice_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VoiceProviderNotConfigured):
        return HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="Voice is not available right now. Please try again later.",
        )
    if isinstance(exc, (VoiceSynthesisFailed, VoiceTranscriptionFailed)):
        logger.error("voice provider call failed: %s", exc)
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice provider request failed",
        )
    raise exc  # unexpected — propagates as 500


@router.post(
    "/transcribe",
    response_model=TranscribeResponse,
    summary="Transcribe recorded audio to text",
)
async def transcribe(
    file: UploadFile,
    current_user: User = Depends(get_current_user),
    registry: VoiceProviderRegistry = Depends(get_voice_registry),
    db: DBSession = Depends(get_db),
) -> TranscribeResponse:
    """Transcribe a short recorded utterance (mic input) to text.

    The returned text is not persisted here — the frontend sends it through
    the normal chat endpoint like any other message. Billed by real audio
    duration (voice/cost.py); TCU reserved before the provider call and
    finalized/cancelled after, exactly like chat/agent requests.
    """
    content_type = _normalize_media_type(file.content_type)
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio content type: {content_type}",
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio upload")
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio upload exceeds the 25 MB limit",
        )

    # Only transcribers whose worst case for this upload fits the plan's per-request ceiling are tried, and the
    # reservation is that worst case (src/voice/budget.py) — the duration is known only afterwards.
    candidates = affordable(_stt_provider_chain(registry),
                            lambda router_: tcu_bound(stt_cost_bound_usd(router_.provider_name, len(audio_bytes))),
                            _request_ceiling_tcu(current_user.plan))
    if not candidates:
        raise _too_large("Este áudio é longo demais para uma transcrição no seu plano. Grave um trecho mais curto.")

    # One reservation, finalized against whichever transcriber delivered — a fallback never bills twice.
    reservation_id = check_and_reserve(
        db, user_id=current_user.id, session_id=None, plan=current_user.plan,
        user_timezone=current_user.timezone, estimated_tcu=max(bound for _, bound in candidates),
    )
    reserved = _reserved_tcu(db, reservation_id)
    chain = [router_ for router_, bound in candidates if bound <= reserved]
    if not chain:
        raise _no_balance(db, reservation_id)
    start = time.monotonic()
    result = None
    provider_name = ""
    last_exc: Exception | None = None
    for attempt, audio_router in enumerate(chain):
        try:
            result = await audio_router.transcribe(audio_bytes, content_type=content_type)
            provider_name = audio_router.provider_name
            if attempt > 0:
                logger.info("voice stt failover succeeded | provider=%s attempt=%d/%d",
                            provider_name, attempt + 1, len(chain))
            break
        except (VoiceProviderNotConfigured, VoiceTranscriptionFailed) as exc:
            last_exc = exc
            logger.warning("voice stt provider unavailable | provider=%s attempt=%d/%d",
                           audio_router.provider_name, attempt + 1, len(chain))
            continue
        except Exception:
            cancel_reservation(db, reservation_id)
            raise
    if result is None:
        cancel_reservation(db, reservation_id)
        raise _map_voice_error(last_exc) if last_exc else HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Voice provider request failed",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    cost_usd = calculate_voice_cost_usd(
        provider_name=provider_name,
        operation="stt",
        duration_seconds=result.duration_seconds,
    )
    tcu = settle_tcu(calculate_tcu(cost_usd), reserved, what=f"{provider_name} stt")
    model_id = _model_id_for(provider_name, "stt")
    finalize_usage(
        db, reservation_id,
        message_id=None,
        model=f"{provider_name}:stt:{model_id}",
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        cost_usd=cost_usd,
        tcu=tcu,
        duration_ms=duration_ms,
    )

    signals = result.signals
    return TranscribeResponse(
        text=result.text,
        language=result.language,
        duration_seconds=result.duration_seconds,
        emotion=signals.emotion if signals else None,
        confidence=signals.confidence if signals else None,
        pace=signals.pace if signals else None,
        urgency=signals.urgency if signals else None,
        prosody=signals.prosody if signals else None,
    )


@router.post(
    "/synthesize",
    summary="Synthesize text to speech",
    response_class=Response,
)
async def synthesize(
    body: SynthesizeRequest,
    current_user: User = Depends(get_current_user),
    registry: VoiceProviderRegistry = Depends(get_voice_registry),
    db: DBSession = Depends(get_db),
) -> Response:
    """Synthesize text (typically the assistant's final answer) to audio bytes.

    Billed by the provider's real unit (voice/cost.py) — characters for
    ElevenLabs, real input/output tokens for Gemini. TCU reserved before the
    provider call and finalized/cancelled after, exactly like chat/agent.
    """
    try:
        chain = _tts_provider_chain(registry)
    except VoiceProviderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice is not available right now. Please try again later.",
        )

    # One reservation for the whole request, finalized against whichever
    # adapter delivers — a fallback must never bill the user twice.
    # Every provider's worst case is known before anything runs (src/voice/budget.py), and none may cost more than
    # the plan allows one request — the fallback included, which is where a Free request once spent 4.34 TCU against
    # a 2.0 ceiling. Each provider gets the longest part of the text it can read within that ceiling, from the start
    # and ending on a whole sentence: the whole text when it fits, otherwise the start, and the response says so. A
    # text whose first sentence fits no provider is refused before anything runs.
    ceiling = _request_ceiling_tcu(current_user.plan)
    plan_items: list[tuple[AudioRouter, str, float]] = []
    for router_ in chain:
        def bound_of(characters: int, provider: str = router_.provider_name) -> float | None:
            return tcu_bound(tts_cost_bound_usd(provider, characters))

        def fits(characters: int, bound_of=bound_of) -> bool:
            bound = bound_of(characters)
            return bound is not None and bound <= ceiling

        readable = body.text if fits(len(body.text)) else longest_affordable_text(body.text, fits)
        if readable:
            plan_items.append((router_, readable, bound_of(len(readable))))
    if not plan_items:
        raise _too_large("Este texto é longo demais para ouvir no seu plano.")

    reservation_id = check_and_reserve(
        db, user_id=current_user.id, session_id=None, plan=current_user.plan,
        user_timezone=current_user.timezone, estimated_tcu=max(bound for _, _, bound in plan_items),
    )
    reserved = _reserved_tcu(db, reservation_id)
    plan_items = [item for item in plan_items if item[2] <= reserved]
    if not plan_items:
        raise _no_balance(db, reservation_id)
    chain = [router_ for router_, _, _ in plan_items]
    texts = {id(router_): readable for router_, readable, _ in plan_items}
    start = time.monotonic()

    result = None
    provider_name = ""
    last_exc: Exception | None = None
    for attempt, audio_router in enumerate(chain):
        try:
            text = texts[id(audio_router)]
            result = await audio_router.synthesize(text, voice_id=body.voice_id)
            provider_name = audio_router.provider_name
            if attempt > 0:
                logger.info(
                    "voice tts failover succeeded | provider=%s attempt=%d/%d",
                    provider_name, attempt + 1, len(chain),
                )
            break
        except (VoiceProviderNotConfigured, VoiceSynthesisFailed) as exc:
            # A provider that is unconfigured, out of quota or erroring is a
            # reason to try the next one — not a reason to fail the request
            # while another adapter is available. The exception text never
            # carries the API key (see the adapters' own guarantees).
            last_exc = exc
            logger.warning(
                "voice tts provider unavailable | provider=%s attempt=%d/%d reason=%s",
                audio_router.provider_name, attempt + 1, len(chain), exc,
            )
            continue
        except Exception:
            # Anything else is not a provider-availability problem; do not
            # burn the rest of the chain on it.
            cancel_reservation(db, reservation_id)
            raise

    if result is None:
        # Every adapter refused. Surface the last provider's error exactly as
        # the single-provider path always did (424 unconfigured / 502 failed).
        cancel_reservation(db, reservation_id)
        raise _map_voice_error(last_exc) if last_exc else HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice provider request failed",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    input_tokens = int(result.metadata.get("input_tokens", 0) or 0)
    output_tokens = int(result.metadata.get("output_tokens", 0) or 0)
    cost_usd = calculate_voice_cost_usd(
        provider_name=provider_name,
        operation="tts",
        characters=len(text),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    tcu = settle_tcu(calculate_tcu(cost_usd), reserved, what=f"{provider_name} tts")
    model_id = _model_id_for(provider_name, "tts")
    # Only Gemini's tokens are meaningful (billed unit); ElevenLabs is
    # character-billed, so its token fields stay 0 per the UsageRecord
    # convention ("input/output tokens = 0 quando não aplicável").
    record_input_tokens = input_tokens if provider_name == "gemini" else 0
    record_output_tokens = output_tokens if provider_name == "gemini" else 0
    finalize_usage(
        db, reservation_id,
        message_id=None,
        model=f"{provider_name}:tts:{model_id}",
        input_tokens=record_input_tokens,
        output_tokens=record_output_tokens,
        cached_tokens=0,
        cost_usd=cost_usd,
        tcu=tcu,
        duration_ms=duration_ms,
    )

    # Shortened to what the plan allows: said, so the client can tell the person the reading stops early.
    headers = {"X-Voice-Truncated": "1"} if text != body.text else None
    return Response(content=result.audio, media_type=result.content_type, headers=headers)
