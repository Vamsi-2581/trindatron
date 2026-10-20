"""A creation asked for in the conversation becomes a real media job.

"Crie uma imagem de um farol ao entardecer" is not answered by a language model
(which would describe an image, or say it cannot make one). It goes through the
same path as POST /media/images: plan, daily count, engine, storage, per-request
ceiling and TCU reservation, in that order. The conversation receives either a
short sentence plus the job to follow, or an honest sentence saying why not.

A video names its mode in the person's words ("em máxima qualidade", "in HD",
"modo rápido"); premium runs only when the same message confirms it ("premium,
confirmo"). When the router had to shorten a clip, lower its resolution or use
a cheaper mode to fit what the person can spend, the reply says so.

Every sentence here is TRINDATRON's own; none names an engine or a vendor. The
only cost ever shown is a premium clip's TCU, which the person must confirm.
"""
from __future__ import annotations

import logging
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.media import modes
from src.models.message import Message
from src.models.user import User
from src.prompting.identity_guard import detect_language
from src.services import media_service
from src.services.media_service import MediaRefused

logger = logging.getLogger("trinity.chat_media")

_STARTED = {
    "pt": {"image": "Estou criando sua imagem. Ela aparece aqui assim que ficar pronta.",
           "video": "Estou criando seu vídeo. Isso leva alguns minutos; ele aparece aqui quando ficar pronto."},
    "en": {"image": "I'm creating your image. It will appear here as soon as it's ready.",
           "video": "I'm creating your video. It takes a few minutes; it will appear here when it's ready."},
    "es": {"image": "Estoy creando tu imagen. Aparecerá aquí en cuanto esté lista.",
           "video": "Estoy creando tu video. Tarda unos minutos; aparecerá aquí cuando esté listo."},
}

_REFUSED = {
    "pt": {
        "media_unavailable": {"image": "Criar imagens não está disponível agora.",
                              "video": "Criar vídeos não está disponível agora."},
        "plan_not_included": {"image": "Seu plano atual não inclui criar imagens.",
                              "video": "Seu plano atual não inclui criar vídeos."},
        "daily_limit_reached": "Você já usou as criações de hoje do seu plano. Elas renovam amanhã.",
        "budget_exhausted": "Não há capacidade suficiente na sua janela de uso atual para essa criação.",
        "job_in_progress": "Já há uma criação em andamento. Assim que ela terminar, peça a próxima.",
        "invalid_request": "Descreva a criação em até 2.000 caracteres.",
    },
    "en": {
        "media_unavailable": {"image": "Creating images isn't available right now.",
                              "video": "Creating videos isn't available right now."},
        "plan_not_included": {"image": "Your current plan doesn't include creating images.",
                              "video": "Your current plan doesn't include creating videos."},
        "daily_limit_reached": "You've used today's creations for your plan. They renew tomorrow.",
        "budget_exhausted": "There isn't enough capacity left in your current usage window for this creation.",
        "job_in_progress": "A creation is already in progress. Ask for the next one when it finishes.",
        "invalid_request": "Describe the creation in up to 2,000 characters.",
    },
    "es": {
        "media_unavailable": {"image": "Crear imágenes no está disponible ahora.",
                              "video": "Crear videos no está disponible ahora."},
        "plan_not_included": {"image": "Tu plan actual no incluye crear imágenes.",
                              "video": "Tu plan actual no incluye crear videos."},
        "daily_limit_reached": "Ya usaste las creaciones de hoy de tu plan. Se renuevan mañana.",
        "budget_exhausted": "No hay capacidad suficiente en tu ventana de uso actual para esta creación.",
        "job_in_progress": "Ya hay una creación en curso. Pide la siguiente cuando termine.",
        "invalid_request": "Describe la creación en hasta 2.000 caracteres.",
    },
}

_MODE_NAMES = {
    "pt": {"economy": "Econômico (480p)", "quality": "Qualidade (720p)", "maximum": "Máximo (720p)",
           "premium": "Premium (720p)"},
    "en": {"economy": "Economy (480p)", "quality": "Quality (720p)", "maximum": "Maximum (720p)",
           "premium": "Premium (720p)"},
    "es": {"economy": "Económico (480p)", "quality": "Calidad (720p)", "maximum": "Máximo (720p)",
           "premium": "Premium (720p)"},
}

_CONFIRM_PREMIUM = {
    "pt": ("O modo Premium usa capacidade premium: cerca de {tcu} TCU por este vídeo de {seconds} s. "
           "Responda “sim” para confirmar."),
    "en": ("Premium mode uses premium capacity: about {tcu} TCU for this {seconds}-second video. "
           "Reply “yes” to confirm."),
    "es": ("El modo Premium usa capacidad premium: cerca de {tcu} TCU por este video de {seconds} s. "
           "Responde “sí” para confirmarlo."),
}

# A video that has to be heard (speech, a shout, a crowd, music) is made in premium mode, the one whose
# engine records sound; it is offered with its cost and made only on a "yes" (owner's rule, 2026-09-27).
_AUDIO_OFFER = {
    "pt": ("Esse vídeo tem som (fala, música ou efeitos), e vídeo com som é feito no modo Premium: "
           "cerca de {tcu} TCU por este vídeo de {seconds} s. Responda “sim” para confirmar."),
    "en": ("This video has sound (speech, music or effects), and video with sound is made in Premium mode: "
           "about {tcu} TCU for this {seconds}-second video. Reply “yes” to confirm."),
    "es": ("Ese video tiene sonido (voz, música o efectos), y el video con sonido se hace en el modo Premium: "
           "cerca de {tcu} TCU por este video de {seconds} s. Responde “sí” para confirmarlo."),
}
# Plans without premium make the video without sound and say where sound is.
_NO_AUDIO = {
    "pt": "Este vídeo sai sem som: vídeo com som está disponível no plano MAX.",
    "en": "This video has no sound: video with sound is available on the MAX plan.",
    "es": "Este video sale sin sonido: el video con sonido está disponible en el plan MAX.",
}
# What every offer above ends with: how a "yes" in the next message is recognised as accepting it.
_OFFER_MARKERS = ("Responda “sim” para confirmar", "Reply “yes” to confirm", "Responde “sí” para confirmarlo")

_YES = re.compile(
    r"^\s*(?:sim|pode|pode\s+sim|pode\s+fazer|confirmo|confirmado|quero|claro|ok|okay|fechado|manda|bora|"
    r"yes|yeah|yep|sure|confirm(?:ed)?|go\s+ahead|do\s+it|s[íi]|dale|claro\s+que\s+s[íi])"
    r"(?:[\s,!.]+(?:pode|confirmo|por\s+favor|please|faz|fa[çc]a|manda|obrigad[oa]|gracias|dale|claro))*[\s!.]*$",
    re.IGNORECASE,
)


wants_audio = modes.wants_audio


def accepted_offer(db: DBSession, user_id: str, session_id: str, content: str) -> str | None:
    """The request a "sim" answers, when the last thing TRINDATRON said in this conversation offered it.

    Only a short yes counts, and only right after an offer this module made; the request returned is the
    person's own message the offer was about. Anything else is None and the message is read as usual.
    """
    from src.models.session import Session as ChatSession

    if not _YES.match(content or ""):
        return None
    if db.query(ChatSession.id).filter(ChatSession.id == session_id, ChatSession.user_id == user_id).first() is None:
        return None
    # The last turn: the request and the offer answering it, saved together (persist_turn) and so often with
    # the same timestamp — told apart by role, not by order.
    last_turn = (db.query(Message).filter(Message.session_id == session_id)
                 .order_by(Message.created_at.desc()).limit(2).all())
    offer = next((row for row in last_turn if row.role == "assistant"), None)
    request = next((row for row in last_turn if row.role == "user"), None)
    if offer is None or request is None:
        return None
    if not any(marker in (offer.content or "") for marker in _OFFER_MARKERS):
        return None
    return request.content

# What the router changed (media/modes.py adjustments), said plainly.
_ADJUSTED = {
    "pt": {"plan": "Seu plano não inclui o modo pedido; usei o modo {mode}.",
           "fit": "Para caber na sua capacidade atual, {changes}.",
           "duration": "encurtei o vídeo para {seconds} s", "resolution": "usei a resolução 480p",
           "engine": "usei o modo {mode}", "and": " e "},
    "en": {"plan": "Your plan doesn't include the mode you asked for; I used {mode}.",
           "fit": "To fit your current capacity, {changes}.",
           "duration": "I shortened the video to {seconds} s", "resolution": "I used 480p",
           "engine": "I used {mode} mode", "and": " and "},
    "es": {"plan": "Tu plan no incluye el modo pedido; usé el modo {mode}.",
           "fit": "Para caber en tu capacidad actual, {changes}.",
           "duration": "acorté el video a {seconds} s", "resolution": "usé la resolución 480p",
           "engine": "usé el modo {mode}", "and": " y "},
}

_SOURCE_MISSING = {
    "pt": "Anexe a imagem que devo transformar em vídeo e peça de novo.",
    "en": "Attach the image to turn into a video and ask again.",
    "es": "Adjunta la imagen que debo convertir en video y vuelve a pedirlo.",
}

# "usando esta imagem", "anime esta foto", "from the attached image", "anima esta imagen"...
_USES_IMAGE = re.compile(
    r"\b(est[ae]|ess[ae]|nest[ae]|ness[ae]|dest[ae]|dess[ae]|minha|sua|la|mi)\s+(imagem|imagen|foto|figura)\b"
    r"|\b(usando|com|a partir d[aeo]|baseado na|com base na|partindo da|desde|con|from|using|with)\s+(a\s+|la\s+|the\s+)?"
    r"(imagem|imagen|foto|image|photo|picture)\b"
    r"|\b(this|the attached|attached|my)\s+(image|photo|picture)\b"
    r"|\b(anime|animar|animate|anima|dar vida|bring (it|this) to life)\b",
    re.IGNORECASE,
)


def wants_source_image(content: str) -> bool:
    """The request is about an image the person already has ("anime esta imagem", "use this photo")."""
    return bool(_USES_IMAGE.search(content or ""))


def latest_image_artifact(db: DBSession, user: User, session_id: str) -> str | None:
    """The newest image attached to this conversation, as the artifact a media job can read."""
    from src.models.attachment import SessionAttachment

    row = (
        db.query(SessionAttachment)
        .filter(SessionAttachment.session_id == session_id, SessionAttachment.user_id == user.id,
                SessionAttachment.kind == "image", SessionAttachment.artifact_id.isnot(None))
        .order_by(SessionAttachment.created_at.desc())
        .first()
    )
    return row.artifact_id if row is not None else None


_DURATION = re.compile(r"\b(\d{1,2})\s*(?:s\b|seg(?:undos?)?\b|sec(?:onds?)?\b|segundos?\b)", re.IGNORECASE)


# A creation request names what it wants in the person's own language, which is
# a sharper signal than the general detector for short requests like "Crea una imagen de un faro".
_ES_HINT = re.compile(r"\b(imagen|haz(?:me)?|dibuja|crea|genera|anima)\b", re.IGNORECASE)
_PT_HINT = re.compile(r"\b(imagem|v[íi]deo|crie|gere|fa[çc]a|desenhe|anime)\b", re.IGNORECASE)
_EN_HINT = re.compile(r"\b(image|picture|photo|create|generate|make|draw|animate)\b", re.IGNORECASE)


def language_for(content: str, language: str | None = None) -> str:
    """`language` is the resolved answer language (src/language.py); the wording hints are the fallback."""
    if language in _STARTED:
        return language
    if _ES_HINT.search(content):
        return "es"
    if _PT_HINT.search(content):
        return "pt"
    if _EN_HINT.search(content):
        return "en"
    return detect_language(content)


def duration_for(content: str) -> int | None:
    """Seconds asked for ("um vídeo de 5 segundos", "10s", "8 seconds"), or None for the plan's default."""
    match = _DURATION.search(content)
    return int(match.group(1)) if match else None


def _clamped_duration(db: DBSession, user: User, content: str) -> int | None:
    """The length asked for, held to what the plan offers so a longer ask becomes the longest clip, not a refusal."""
    asked = duration_for(content)
    if asked is None:
        return None
    longest = media_service.entitlement_for(db, user).max_video_duration_seconds
    return max(1, min(asked, longest)) if longest else None


def adjustment_note(language: str, params: dict | None) -> str:
    """One or two sentences on what the router changed to fit the request, or "" when it ran as asked."""
    params = params or {}
    changed = list(params.get("adjustments") or [])
    if not changed:
        return ""
    words = _ADJUSTED[language]
    mode = _MODE_NAMES[language].get(params.get("mode") or "", params.get("mode") or "")
    notes = [words["plan"].format(mode=mode)] if "plan" in changed else []
    fit = [words[item].format(mode=mode, seconds=params.get("duration_seconds"))
           for item in ("duration", "resolution", "engine") if item in changed]
    if fit:
        notes.append(words["fit"].format(changes=words["and"].join(fit)))
    return " ".join(notes)


def _refusal(language: str, code: str, kind: str) -> str:
    message = _REFUSED[language].get(code) or _REFUSED[language]["media_unavailable"]
    return message[kind] if isinstance(message, dict) else message


def artifact_event(db: DBSession, job) -> dict:
    """The `artifact` SSE payload clients already understand: the job, never its engine or cost."""
    view = media_service.job_view(db, job)
    return {"id": view["id"], "kind": view["kind"], "status": view["status"], "progress": view["progress"],
            "created_at": view["created_at"], "content_type": None, "preview_url": None, "download_url": None}


async def start_from_chat(db: DBSession, user: User, content: str, kind: str,
                          session_id: str | None = None, language: str | None = None,
                          confirmed: bool = False) -> tuple[str, object | None]:
    """(reply, job | None). Never raises: every outcome is a sentence the person can read.

    A video that names an image the person attached ("anime esta imagem", "use this
    photo") becomes image-to-video from the newest image in the conversation.

    A video that has to be heard goes to premium, the mode whose engine records sound: offered with its
    cost, made on a "yes" (`confirmed`, from accepted_offer()). A plan without premium gets the video
    without sound and is told where sound is.
    """
    language = language_for(content, language)
    source_artifact_id = None
    if kind == "video" and session_id and wants_source_image(content):
        source_artifact_id = latest_image_artifact(db, user, session_id)
        if source_artifact_id is None:
            return _SOURCE_MISSING[language], None
    mode = modes.mode_for_text(content) if kind == "video" else None
    audio = kind == "video" and wants_audio(content)
    silent_note = ""
    if audio and mode != modes.PREMIUM.id:
        if modes.PREMIUM.id in media_service.entitlement_for(db, user).video_modes:
            mode = modes.PREMIUM.id
        else:
            silent_note = _NO_AUDIO[language]
    try:
        # The whole request is the format text: "vertical 1080x1920", "para Instagram Story", "16:9".
        if kind == "image":
            job = await media_service.create_image_job(db, user, prompt=content, format_text=content)
        else:
            job = await media_service.create_video_job(db, user, prompt=content, format_text=content,
                                                       duration_seconds=_clamped_duration(db, user, content),
                                                       source_artifact_id=source_artifact_id,
                                                       mode=mode,
                                                       confirm_premium=confirmed or modes.confirms_premium(content))
    except MediaRefused as exc:
        if exc.code == "confirmation_required":
            offer = _AUDIO_OFFER if audio else _CONFIRM_PREMIUM
            return offer[language].format(tcu=_tcu_text(exc.details.get("estimated_tcu"), language),
                                          seconds=exc.details.get("duration_seconds")), None
        return _refusal(language, exc.code, kind), None
    except HTTPException:
        # The plan's windows refused the reservation.
        return _refusal(language, "budget_exhausted", kind), None
    except Exception:
        logger.exception("chat_media: could not start a %s job", kind)
        return _refusal(language, "media_unavailable", kind), None
    if job.status == "queued":
        media_service.schedule(job.id)
    notes = [adjustment_note(language, job.params) if kind == "video" else "", silent_note]
    return " ".join([_STARTED[language][kind], *[note for note in notes if note]]), job


def _tcu_text(value, language: str) -> str:
    text = f"{float(value or 0):.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if language in ("pt", "es") else text


def persist_turn(db: DBSession, session_id: str, user_content: str, reply: str,
                 media_job_id: str | None) -> tuple[str | None, str | None]:
    """The user's request and TRINDATRON's reply, the reply linked to the job it started."""
    try:
        user_message = Message(session_id=session_id, role="user", content=user_content)
        assistant_message = Message(session_id=session_id, role="assistant", content=reply, media_job_id=media_job_id)
        db.add_all([user_message, assistant_message])
        db.commit()
        return user_message.id, assistant_message.id
    except Exception:
        logger.exception("chat_media: could not persist the turn")
        db.rollback()
        return None, None
