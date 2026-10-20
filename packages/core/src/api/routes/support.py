"""Help center, support assistant and tickets. Operators answer through /support/admin."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_admin_user, get_current_user
from src.database import get_db
from src.models.support import SupportTicket
from src.models.user import User
from src.services import support_service
from src.services.support_service import SupportRefused
from src.support import articles

router = APIRouter(prefix="/support", tags=["support"])


def _http(exc: SupportRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code})


class AssistantBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    lang: str | None = None


class TicketBody(BaseModel):
    category: str
    subject: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=8000)
    platform: str | None = None
    app_version: str | None = Field(default=None, max_length=40)
    error_code: str | None = Field(default=None, max_length=80)
    context: dict | None = None


class MessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class StatusBody(BaseModel):
    status: str


@router.get("/articles")
def list_articles(q: str = Query(default="", max_length=200), lang: str | None = None, category: str | None = None):
    language = articles.language(lang)
    return {
        "categories": list(articles.CATEGORIES),
        "articles": [articles.view(a, language) for a in articles.search(q, language, category=category, limit=20)],
    }


@router.get("/articles/{article_id}")
def get_article(article_id: str, lang: str | None = None):
    article = next((a for a in articles.ARTICLES if a.id == article_id), None)
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    return articles.view(article, articles.language(lang))


@router.post("/assistant")
def assistant(body: AssistantBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return support_service.assist(db, current_user, message=body.message, lang=body.lang)


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
def open_ticket(body: TicketBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        ticket = support_service.open_ticket(db, current_user, **body.model_dump())
    except SupportRefused as exc:
        raise _http(exc)
    return support_service.ticket_view(db, ticket, with_messages=True)


@router.get("/tickets")
def my_tickets(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (db.query(SupportTicket).filter(SupportTicket.user_id == current_user.id)
            .order_by(SupportTicket.updated_at.desc()).limit(100).all())
    return {"tickets": [support_service.ticket_view(db, row) for row in rows]}


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        return support_service.ticket_view(db, support_service.owned_ticket(db, current_user, ticket_id), with_messages=True)
    except SupportRefused as exc:
        raise _http(exc)


@router.post("/tickets/{ticket_id}/messages")
def reply(ticket_id: str, body: MessageBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        ticket = support_service.add_message(db, support_service.owned_ticket(db, current_user, ticket_id), author="user", body=body.body)
    except SupportRefused as exc:
        raise _http(exc)
    return support_service.ticket_view(db, ticket, with_messages=True)


@router.post("/tickets/{ticket_id}/resolve")
def resolve(ticket_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        ticket = support_service.set_status(db, support_service.owned_ticket(db, current_user, ticket_id), "resolved")
    except SupportRefused as exc:
        raise _http(exc)
    return support_service.ticket_view(db, ticket)


# ── Operators ────────────────────────────────────────────────────────────────

@router.get("/admin/tickets")
def all_tickets(status_filter: str | None = Query(default=None, alias="status"), db: DBSession = Depends(get_db),
                _admin: User = Depends(get_current_admin_user)):
    query = db.query(SupportTicket)
    if status_filter:
        query = query.filter(SupportTicket.status == status_filter)
    rows = query.order_by(SupportTicket.updated_at.desc()).limit(200).all()
    return {"tickets": [support_service.ticket_view(db, row) for row in rows]}


@router.post("/admin/tickets/{ticket_id}/messages")
def admin_reply(ticket_id: str, body: MessageBody, db: DBSession = Depends(get_db), _admin: User = Depends(get_current_admin_user)):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    try:
        ticket = support_service.add_message(db, ticket, author="support", body=body.body)
    except SupportRefused as exc:
        raise _http(exc)
    return support_service.ticket_view(db, ticket, with_messages=True)


@router.post("/admin/tickets/{ticket_id}/status")
def admin_status(ticket_id: str, body: StatusBody, db: DBSession = Depends(get_db), _admin: User = Depends(get_current_admin_user)):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    try:
        return support_service.ticket_view(db, support_service.set_status(db, ticket, body.status))
    except SupportRefused as exc:
        raise _http(exc)
