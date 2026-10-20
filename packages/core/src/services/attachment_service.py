"""Attaching files to a conversation, and what the model sees of them.

Context strategy
----------------
Text is extracted ONCE, when the file is attached, and stored bounded
(STORED_CHARS). Every later turn reads that stored text — the file is never
re-parsed or re-sent whole. What reaches the prompt is bounded again per
attachment (CONTEXT_CHARS_PER_FILE) and across the conversation
(CONTEXT_CHARS_TOTAL), newest attachments first, so a conversation with many
files cannot push the policy or the question out of the model's attention.

Images
------
An attached image is read once by the vision engine (services/vision_service.py),
within the plan's image-reading allowance, and its description enters the
context like a document's text. When it cannot be read — no engine, allowance
used, engine failure — it is listed to the model by name with an explicit
statement that its content is not visible, so TRINDATRON says so instead of
inventing a description.

Prompt injection
----------------
File content (and an image description, which may transcribe text found in the
image) is user data. It is rendered inside an <attachments> block that tells the
model to treat it as material to work on, never as instructions.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.media.settings import media_settings
from src.models.attachment import SessionAttachment, _utcnow
from src.models.media import Artifact
from src.models.project import Project, ProjectFile
from src.models.session import Session
from src.models.user import User
from src.storage import build_object_store
from src.storage.object_store import ObjectStore

logger = logging.getLogger("trinity.attachments")

MAX_ATTACHMENTS_PER_SESSION = 10
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 60
STORED_CHARS = 60_000
CONTEXT_CHARS_PER_FILE = 6_000
CONTEXT_CHARS_TOTAL = 16_000

READABLE_STATUSES = ("text", "vision")

_TEXT_TYPES = {"application/json", "application/xml", "application/x-yaml", "application/yaml",
               "application/csv", "application/javascript", "application/x-sh"}
_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm",
                    ".py", ".js", ".ts", ".tsx", ".sql", ".log", ".ini", ".toml")
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class AttachmentRefused(Exception):
    STATUS = {"not_found": 404, "too_large": 413, "too_many": 409, "invalid": 422, "unavailable": 503}
    MESSAGES = {
        "not_found": "Not found.",
        "too_large": "The file is larger than the allowed limit.",
        "too_many": "This conversation already has the maximum number of attached files.",
        "invalid": "This file cannot be attached.",
        "unavailable": "Attaching files is not available right now.",
    }

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]
        self.message = self.MESSAGES[code]


def _base_type(mime_type: str | None) -> str:
    return (mime_type or "application/octet-stream").split(";")[0].strip().lower()


def extract_text(data: bytes, mime_type: str, name: str) -> tuple[str | None, str]:
    """(text, status). Never raises; a file it cannot read is 'unsupported'."""
    base = _base_type(mime_type)
    lower = (name or "").lower()
    try:
        if base.startswith("text/") or base in _TEXT_TYPES or lower.endswith(_TEXT_EXTENSIONS):
            text = data.decode("utf-8", errors="replace")
        elif base == "application/pdf" or lower.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data), strict=False)
            pages = [page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES]]
            text = "\n\n".join(pages)
        else:
            return None, "unsupported"
    except Exception as exc:
        logger.info("attachments: extraction failed | type=%s error=%s", base, type(exc).__name__)
        return None, "unsupported"
    text = text.replace("\x00", "").strip()
    if not text:
        return None, "empty"
    return text[:STORED_CHARS], "text"


def owned_session(db: DBSession, user: User, session_id: str) -> Session:
    session = (
        db.query(Session)
        .filter(Session.id == session_id, Session.user_id == user.id, Session.deleted_at.is_(None))
        .first()
    )
    if session is None:
        raise AttachmentRefused("not_found")
    return session


def _check_room(db: DBSession, session_id: str) -> None:
    if db.query(SessionAttachment).filter(SessionAttachment.session_id == session_id).count() >= MAX_ATTACHMENTS_PER_SESSION:
        raise AttachmentRefused("too_many")


def _kind(mime_type: str) -> str:
    return "image" if _base_type(mime_type) in _IMAGE_TYPES else "document"


def _next_created_at(db: DBSession, session_id: str) -> datetime:
    """A created_at strictly after this session's most recent attachment, so
    context_for_session's "newest first" (ORDER BY created_at DESC) is never
    ambiguous. datetime.now() alone is not enough to guarantee that: on this
    codebase's Windows dev/CI target, datetime.now(timezone.utc) can return the
    identical value for hundreds of consecutive calls in a tight loop (confirmed
    locally: 999 of 1000) — a real hazard for two files attached back to back,
    not a hypothetical one, and SQLite/Postgres both leave same-value ties in an
    unspecified order."""
    latest = db.query(func.max(SessionAttachment.created_at)).filter(SessionAttachment.session_id == session_id).scalar()
    if latest is not None and latest.tzinfo is None:
        # SQLite silently drops tzinfo on read even for DateTime(timezone=True) columns
        # (see database.py's UTCDateTime docstring); this codebase only ever stores UTC.
        latest = latest.replace(tzinfo=timezone.utc)
    now = _utcnow()
    return latest + timedelta(microseconds=1) if latest is not None and latest >= now else now


async def attach_upload(db: DBSession, user: User, session_id: str, *, name: str, mime_type: str, data: bytes,
                        store: ObjectStore | None = None) -> SessionAttachment:
    owned_session(db, user, session_id)
    _check_room(db, session_id)
    if not data:
        raise AttachmentRefused("invalid")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AttachmentRefused("too_large")

    from src.services.media_service import storage_ready

    store = store or build_object_store()
    if not await storage_ready(store):
        raise AttachmentRefused("unavailable")

    base = _base_type(mime_type)
    kind = _kind(base)
    safe_name = (name or "file").replace("/", "_").replace("\\", "_")[:200]

    from src.services.media_service import _now

    artifact = Artifact(user_id=user.id, kind=kind if kind == "image" else "document", source="upload",
                        filename=safe_name, content_type=base, size_bytes=len(data),
                        storage_backend=store.backend_name, storage_key="pending",
                        expires_at=_now() + timedelta(days=media_settings().media_retention_days))
    db.add(artifact)
    db.flush()
    artifact.storage_key = f"attachments/{user.id}/{session_id}/{artifact.id}"
    # Stored first: an image is only read (a paid call) once the file it describes is safely kept.
    await store.put(key=artifact.storage_key, data=data, content_type=base)

    if kind == "image":
        from src.services.vision_service import read_image

        text, status = await read_image(db, user, data, base)
        text = text[:STORED_CHARS] if text else None
    else:
        text, status = extract_text(data, base, name)

    attachment = SessionAttachment(user_id=user.id, session_id=session_id, name=safe_name, mime_type=base,
                                   size_bytes=len(data), kind=kind, artifact_id=artifact.id,
                                   extracted_text=text, extraction_status=status,
                                   created_at=_next_created_at(db, session_id))
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def attach_project_file(db: DBSession, user: User, session_id: str, project_file_id: str) -> SessionAttachment:
    owned_session(db, user, session_id)
    _check_room(db, session_id)
    project_file = (
        db.query(ProjectFile)
        .join(Project, ProjectFile.project_id == Project.id)
        .filter(ProjectFile.id == project_file_id, Project.user_id == user.id)
        .first()
    )
    if project_file is None:
        raise AttachmentRefused("not_found")
    kind = _kind(project_file.mime_type)
    text, status = (None, "unsupported") if kind == "image" else extract_text(
        project_file.content, project_file.mime_type, project_file.name
    )
    attachment = SessionAttachment(user_id=user.id, session_id=session_id, name=project_file.name,
                                   mime_type=_base_type(project_file.mime_type), size_bytes=project_file.size_bytes,
                                   kind=kind, project_file_id=project_file.id, extracted_text=text,
                                   extraction_status=status, created_at=_next_created_at(db, session_id))
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def list_attachments(db: DBSession, user: User, session_id: str) -> list[SessionAttachment]:
    owned_session(db, user, session_id)
    return (
        db.query(SessionAttachment)
        .filter(SessionAttachment.session_id == session_id, SessionAttachment.user_id == user.id)
        .order_by(SessionAttachment.created_at.asc())
        .all()
    )


def owned_attachment(db: DBSession, user: User, session_id: str, attachment_id: str) -> SessionAttachment:
    owned_session(db, user, session_id)
    attachment = (
        db.query(SessionAttachment)
        .filter(SessionAttachment.id == attachment_id, SessionAttachment.session_id == session_id,
                SessionAttachment.user_id == user.id)
        .first()
    )
    if attachment is None:
        raise AttachmentRefused("not_found")
    return attachment


async def attachment_bytes(db: DBSession, attachment: SessionAttachment, store: ObjectStore | None = None) -> bytes:
    if attachment.project_file_id:
        project_file = db.query(ProjectFile).filter(ProjectFile.id == attachment.project_file_id).first()
        if project_file is None:
            raise AttachmentRefused("not_found")
        return project_file.content
    artifact = db.query(Artifact).filter(Artifact.id == attachment.artifact_id).first()
    if artifact is None:
        raise AttachmentRefused("not_found")
    return await (store or build_object_store()).get(key=artifact.storage_key)


async def delete_attachment(db: DBSession, user: User, session_id: str, attachment_id: str,
                            store: ObjectStore | None = None) -> None:
    attachment = owned_attachment(db, user, session_id, attachment_id)
    if attachment.artifact_id:
        artifact = db.query(Artifact).filter(Artifact.id == attachment.artifact_id).first()
        if artifact is not None:
            await (store or build_object_store()).delete(key=artifact.storage_key)
            attachment.artifact_id = None
            db.delete(artifact)
    db.delete(attachment)
    db.commit()


def attachment_view(attachment: SessionAttachment) -> dict:
    return {
        "id": attachment.id,
        "session_id": attachment.session_id,
        "name": attachment.name,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "kind": attachment.kind,
        "project_file_id": attachment.project_file_id,
        # Whether TRINDATRON can read this file's content in the conversation.
        "readable": attachment.extraction_status in READABLE_STATUSES,
        "created_at": attachment.created_at.isoformat(),
        "content_path": f"/sessions/{attachment.session_id}/attachments/{attachment.id}/content",
    }


def context_for_session(db: DBSession, session_id: str) -> tuple[tuple[str, str, str], ...]:
    """(name, kind, text) for the prompt: newest first, bounded per file and in total."""
    rows = (
        db.query(SessionAttachment)
        .filter(SessionAttachment.session_id == session_id)
        .order_by(SessionAttachment.created_at.desc())
        .all()
    )
    budget = CONTEXT_CHARS_TOTAL
    items: list[tuple[str, str, str]] = []
    for row in rows:
        if row.extraction_status in READABLE_STATUSES and row.extracted_text and budget > 0:
            excerpt = row.extracted_text[: min(CONTEXT_CHARS_PER_FILE, budget)]
            budget -= len(excerpt)
            items.append((row.name, row.kind, excerpt))
        else:
            items.append((row.name, row.kind, ""))
    return tuple(items)
