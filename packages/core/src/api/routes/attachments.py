"""Files attached to a conversation: attach (upload or project file), list, open, remove.

Attached documents become part of the conversation's context on the next turn
(see services/attachment_service.py for how, and how much). Everything is
scoped to the authenticated owner; anything else answers 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.models.user import User
from src.services import attachment_service
from src.services.attachment_service import AttachmentRefused

router = APIRouter(prefix="/sessions", tags=["attachments"])


def _http(exc: AttachmentRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message})


class ProjectFileAttachment(BaseModel):
    project_file_id: str = Field(min_length=1, max_length=36)


@router.post("/{session_id}/attachments", status_code=status.HTTP_201_CREATED)
async def attach_file(session_id: str, file: UploadFile = File(...), db: DBSession = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    data = await file.read(attachment_service.MAX_UPLOAD_BYTES + 1)
    try:
        attachment = await attachment_service.attach_upload(
            db, current_user, session_id, name=file.filename or "file",
            mime_type=file.content_type or "application/octet-stream", data=data,
        )
    except AttachmentRefused as exc:
        raise _http(exc)
    return attachment_service.attachment_view(attachment)


@router.post("/{session_id}/attachments/project-file", status_code=status.HTTP_201_CREATED)
def attach_project_file(session_id: str, body: ProjectFileAttachment, db: DBSession = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    try:
        attachment = attachment_service.attach_project_file(db, current_user, session_id, body.project_file_id)
    except AttachmentRefused as exc:
        raise _http(exc)
    return attachment_service.attachment_view(attachment)


@router.get("/{session_id}/attachments")
def list_attachments(session_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        rows = attachment_service.list_attachments(db, current_user, session_id)
    except AttachmentRefused as exc:
        raise _http(exc)
    return {"attachments": [attachment_service.attachment_view(row) for row in rows]}


@router.get("/{session_id}/attachments/{attachment_id}/content")
async def attachment_content(session_id: str, attachment_id: str, download: bool = False,
                             db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        attachment = attachment_service.owned_attachment(db, current_user, session_id, attachment_id)
        data = await attachment_service.attachment_bytes(db, attachment)
    except AttachmentRefused as exc:
        raise _http(exc)
    disposition = "attachment" if download else "inline"
    return Response(content=data, media_type=attachment.mime_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{attachment.name}"',
                             "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@router.delete("/{session_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_attachment(session_id: str, attachment_id: str, db: DBSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    try:
        await attachment_service.delete_attachment(db, current_user, session_id, attachment_id)
    except AttachmentRefused as exc:
        raise _http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
