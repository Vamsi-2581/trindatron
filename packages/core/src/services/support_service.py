"""Support: the assistant answers what it can prove, a ticket takes the rest.

The assistant is deterministic on purpose. It matches the question to help
center articles and, for the problems people most often have (a limit, a
creation, a team seat, a sign-in), it reads the person's own account state and
states the facts — plan, what is left today, when it renews. It does not call a
language model: it costs nothing, cannot invent an answer, and cannot be talked
into describing how TRINDATRON is built. When nothing it knows fits, it says so
and offers a ticket.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.models.support import TICKET_CATEGORIES, SupportMessage, SupportTicket
from src.models.user import User
from src.support import articles

CONTEXT_KEYS = ("screen", "request_id", "job_id", "session_id", "network", "locale")
_PLATFORMS = ("android", "ios", "web", "desktop")
_SAFE_VALUE = re.compile(r"^[\w.\-:/ ]{1,120}$")

FACTS = {
    "plan": {"pt": "Seu plano atual: {plan}.", "en": "Your current plan: {plan}.", "es": "Tu plan actual: {plan}."},
    "images_left": {"pt": "Imagens disponíveis hoje: {remaining} de {limit}.", "en": "Images available today: {remaining} of {limit}.",
                    "es": "Imágenes disponibles hoy: {remaining} de {limit}."},
    "videos_left": {"pt": "Vídeos disponíveis hoje: {remaining} de {limit}.", "en": "Videos available today: {remaining} of {limit}.",
                    "es": "Videos disponibles hoy: {remaining} de {limit}."},
    "no_video": {"pt": "Seu plano atual não inclui vídeos.", "en": "Your current plan does not include videos.",
                 "es": "Tu plan actual no incluye videos."},
    "team_active": {"pt": "Seu time está ativo e seu assento é {seat}.", "en": "Your team is active and your seat is {seat}.",
                    "es": "Tu equipo está activo y tu asiento es {seat}."},
    "team_pending": {"pt": "Os assentos do seu time ainda não estão ativos: o pagamento não foi confirmado.",
                     "en": "Your team's seats are not active yet: the payment has not been confirmed.",
                     "es": "Los asientos de tu equipo aún no están activos: el pago no se ha confirmado."},
}
INTRO = {
    "found": {"pt": "Isto deve ajudar:", "en": "This should help:", "es": "Esto debería ayudar:"},
    "not_found": {"pt": "Não encontrei uma resposta para isso. Abra um chamado e a equipe responde por aqui.",
                  "en": "I couldn't find an answer to that. Open a ticket and the team will reply here.",
                  "es": "No encontré una respuesta para eso. Abre un ticket y el equipo responderá aquí."},
}


def _plan_label(plan: str) -> str:
    return (plan or "free").upper()


def _facts(db: DBSession, user: User, lang: str, matched: list[articles.Article]) -> list[str]:
    categories = {article.category for article in matched}
    facts: list[str] = []
    if categories & {"plans", "billing", "creations", "team"}:
        facts.append(FACTS["plan"][lang].format(plan=_plan_label(user.plan)))
    if "creations" in categories:
        from src.services import media_service

        entitlement = media_service.entitlement_for(db, user)
        images = media_service.daily_usage(db, user, "image", entitlement)
        if images.limit:
            facts.append(FACTS["images_left"][lang].format(remaining=images.remaining, limit=images.limit))
        videos = media_service.daily_usage(db, user, "video", entitlement)
        facts.append(FACTS["videos_left"][lang].format(remaining=videos.remaining, limit=videos.limit)
                     if videos.limit else FACTS["no_video"][lang])
    if "team" in categories:
        from src.services import team_service

        try:
            team, member = team_service.team_for(db, user)
        except team_service.TeamRefused:
            team = None
        if team is not None:
            facts.append(FACTS["team_active"][lang].format(seat=member.seat_type.capitalize())
                         if team.status == "active" else FACTS["team_pending"][lang])
    return facts


def assist(db: DBSession, user: User, *, message: str, lang: str | None) -> dict:
    lang = articles.language(lang)
    matched = articles.search(message or "", lang, limit=3) if (message or "").strip() else []
    return {
        "answer": INTRO["found" if matched else "not_found"][lang],
        "facts": _facts(db, user, lang, matched) if matched else [],
        "articles": [articles.view(article, lang) for article in matched],
        "suggest_ticket": not matched,
        "suggested_category": matched[0].category if matched else "other",
    }


# ── Tickets ──────────────────────────────────────────────────────────────────

class SupportRefused(Exception):
    STATUS = {"not_found": 404, "invalid": 422, "closed": 409}

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]


def _clean_context(context: dict | None) -> dict:
    """Only known, short, plain values travel with a ticket — never free-form blobs."""
    clean: dict[str, str] = {}
    for key in CONTEXT_KEYS:
        value = (context or {}).get(key)
        if isinstance(value, (str, int, float)) and _SAFE_VALUE.match(str(value)):
            clean[key] = str(value)
    return clean


def open_ticket(db: DBSession, user: User, *, category: str, subject: str, description: str, platform: str | None,
                app_version: str | None, error_code: str | None, context: dict | None) -> SupportTicket:
    subject = " ".join((subject or "").split())
    description = (description or "").strip()
    if category not in TICKET_CATEGORIES or not subject or len(subject) > 160 or not description or len(description) > 8000:
        raise SupportRefused("invalid")
    ticket = SupportTicket(
        user_id=user.id, category=category, subject=subject, description=description,
        platform=platform if platform in _PLATFORMS else None,
        app_version=(app_version or "")[:40] or None,
        error_code=error_code if error_code and _SAFE_VALUE.match(error_code) else None,
        context=_clean_context(context), status="open",
    )
    db.add(ticket)
    db.flush()
    db.add(SupportMessage(ticket_id=ticket.id, author="user", body=description))
    db.commit()
    db.refresh(ticket)
    return ticket


def owned_ticket(db: DBSession, user: User, ticket_id: str) -> SupportTicket:
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id, SupportTicket.user_id == user.id).first()
    if ticket is None:
        raise SupportRefused("not_found")
    return ticket


def add_message(db: DBSession, ticket: SupportTicket, *, author: str, body: str) -> SupportTicket:
    body = (body or "").strip()
    if not body or len(body) > 8000:
        raise SupportRefused("invalid")
    if ticket.status == "resolved" and author == "support":
        raise SupportRefused("closed")
    db.add(SupportMessage(ticket_id=ticket.id, author=author, body=body))
    # A person writing reopens the conversation for review; support replying answers it.
    ticket.status = "answered" if author == "support" else "in_review"
    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return ticket


def set_status(db: DBSession, ticket: SupportTicket, status: str) -> SupportTicket:
    if status not in ("open", "in_review", "answered", "resolved"):
        raise SupportRefused("invalid")
    ticket.status = status
    db.commit()
    db.refresh(ticket)
    return ticket


def ticket_view(db: DBSession, ticket: SupportTicket, *, with_messages: bool = False) -> dict:
    view = {
        "id": ticket.id, "category": ticket.category, "subject": ticket.subject, "status": ticket.status,
        "platform": ticket.platform, "app_version": ticket.app_version, "error_code": ticket.error_code,
        "created_at": ticket.created_at.isoformat(), "updated_at": ticket.updated_at.isoformat(),
    }
    if with_messages:
        rows = db.query(SupportMessage).filter(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.created_at.asc()).all()
        view["messages"] = [{"id": row.id, "author": row.author, "body": row.body, "created_at": row.created_at.isoformat()}
                            for row in rows]
    return view
