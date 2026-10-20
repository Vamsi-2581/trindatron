"""Media HTTP surface: create, follow, list, open, download, delete.

Creation answers 202 with the job; the work runs in the background. Every read
is scoped to the authenticated user, and anything belonging to someone else is
reported as not found — never as forbidden — so ids cannot be probed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.config import settings
from src.database import get_db
from src.media.operations import OperationInput, TRANSFORM_OPERATIONS
from src.models.user import User
from src.services import media_service
from src.services.media_service import MediaRefused, refusal_http
from src.storage import build_object_store

router = APIRouter(prefix="/media", tags=["media"])


class OperationInputBody(BaseModel):
    role: str = Field(min_length=1, max_length=40)
    artifact_id: str


class OperationRequestBody(BaseModel):
    operation: str
    inputs: list[OperationInputBody] = Field(min_length=1, max_length=8)
    parameters: dict = Field(default_factory=dict)
    quality_tier: str = "standard"
    project_id: str | None = None


class ImageRequestBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=media_service.MAX_PROMPT_CHARS)
    # Any format in the person's words ("1080x1920", "9:16", "Instagram Story"), or exact pixels.
    # `aspect` is kept for older clients that send one of three shape names.
    format: str | None = Field(default=None, max_length=120)
    width: int | None = Field(default=None, ge=64, le=8192)
    height: int | None = Field(default=None, ge=64, le=8192)
    aspect: str = "square"
    project_id: str | None = None
    source_artifact_id: str | None = None


class VideoRequestBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=media_service.MAX_PROMPT_CHARS)
    format: str | None = Field(default=None, max_length=120)
    width: int | None = Field(default=None, ge=64, le=8192)
    height: int | None = Field(default=None, ge=64, le=8192)
    aspect: str = "landscape"
    duration_seconds: int | None = Field(default=None, ge=1, le=30)
    project_id: str | None = None
    source_artifact_id: str | None = None
    # economy | quality | maximum | premium (media/modes.py); None = the plan's default mode.
    mode: Literal["economy", "quality", "maximum", "premium"] | None = None
    # Premium runs only when this is true; otherwise it is answered 409 confirmation_required with its TCU.
    confirm_premium: bool = False


def _key(value: str | None) -> str | None:
    value = (value or "").strip()
    return value[:64] if value else None


@router.post("/images", status_code=status.HTTP_202_ACCEPTED)
async def create_image(body: ImageRequestBody, idempotency_key: str | None = Header(default=None),
                       db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        job = await media_service.create_image_job(
            db, current_user, prompt=body.prompt, aspect=body.aspect, format_text=body.format,
            width=body.width, height=body.height, project_id=body.project_id,
            source_artifact_id=body.source_artifact_id, idempotency_key=_key(idempotency_key),
        )
    except MediaRefused as exc:
        raise refusal_http(exc)
    if job.status == "queued":
        media_service.schedule(job.id)
    return media_service.job_view(db, job)


@router.post("/videos", status_code=status.HTTP_202_ACCEPTED)
async def create_video(body: VideoRequestBody, idempotency_key: str | None = Header(default=None),
                       db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        job = await media_service.create_video_job(
            db, current_user, prompt=body.prompt, aspect=body.aspect, format_text=body.format,
            width=body.width, height=body.height, duration_seconds=body.duration_seconds,
            project_id=body.project_id, source_artifact_id=body.source_artifact_id,
            idempotency_key=_key(idempotency_key), mode=body.mode, confirm_premium=body.confirm_premium,
        )
    except MediaRefused as exc:
        raise refusal_http(exc)
    if job.status == "queued":
        media_service.schedule(job.id)
    return media_service.job_view(db, job)


@router.post("/operations", status_code=status.HTTP_202_ACCEPTED)
async def create_operation(body: OperationRequestBody, idempotency_key: str | None = Header(default=None),
                           db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """CP19: the permanent TRANSFORM/COMPOSE entry point (media/operations.py) — background_remove today,
    every future multi-input operation the same way. Status/cancel reuse the existing job endpoints below:
    an operation job is a MediaJob like any other, so nothing new was needed for either."""
    if body.operation not in TRANSFORM_OPERATIONS:
        raise refusal_http(MediaRefused("invalid_request"))
    try:
        job = await media_service.create_operation_job(
            db, current_user, operation=body.operation,
            inputs=[OperationInput(role=i.role, artifact_id=i.artifact_id) for i in body.inputs],
            parameters=body.parameters, quality_tier=body.quality_tier, project_id=body.project_id,
            idempotency_key=_key(idempotency_key),
        )
    except MediaRefused as exc:
        raise refusal_http(exc)
    if job.status == "queued":
        media_service.schedule(job.id)
    return media_service.job_view(db, job)


@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def upload_source(file: UploadFile = File(...), db: DBSession = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """An image to edit or turn into a video."""
    data = await file.read(media_service.media_settings().media_max_source_bytes + 1)
    try:
        artifact = await media_service.store_source_image(db, current_user, data=data,
                                                          content_type=file.content_type or "")
    except MediaRefused as exc:
        raise refusal_http(exc)
    return media_service.artifact_view(artifact)


@router.post("/sources/video", status_code=status.HTTP_201_CREATED)
async def upload_video_source(file: UploadFile = File(...), db: DBSession = Depends(get_db),
                              current_user: User = Depends(get_current_user)):
    """CP22: a driving/reference video for a multi-input operation (e.g. Wan2.2-Animate's
    driving_video) — a separate endpoint from /sources on purpose, since it has its own,
    larger size limit and its own allow-list (video, not image)."""
    data = await file.read(media_service.media_settings().media_max_video_source_bytes + 1)
    try:
        artifact = await media_service.store_source_media(db, current_user, data=data,
                                                           content_type=file.content_type or "", kind="video")
    except MediaRefused as exc:
        raise refusal_http(exc)
    return media_service.artifact_view(artifact)


@router.post("/vectorize", status_code=status.HTTP_201_CREATED)
async def vectorize(file: UploadFile = File(...), db: DBSession = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Convert a raster image (PNG/JPEG/WebP) to an SVG artifact. Deterministic, local, $0 — no job to poll."""
    data = await file.read(media_service.media_settings().media_max_source_bytes + 1)
    try:
        artifact = await media_service.vectorize_source_image(db, current_user, data=data,
                                                               content_type=file.content_type or "")
    except MediaRefused as exc:
        raise refusal_http(exc)
    return media_service.artifact_view(artifact)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    media_service.reclaim_stale_jobs(db)
    try:
        return media_service.job_view(db, media_service.owned_job(db, current_user, job_id))
    except MediaRefused as exc:
        raise refusal_http(exc)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Cancel a creation that has not started generating. Nothing is charged for a cancelled creation."""
    try:
        job = await media_service.cancel_job(db, current_user, job_id)
    except MediaRefused as exc:
        raise refusal_http(exc)
    return media_service.job_view(db, job)


@router.get("/creations")
def list_creations(
    kind: str | None = Query(default=None, pattern="^(image|video|avatar)$"),
    project_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before: datetime | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    media_service.reclaim_stale_jobs(db)
    jobs, next_before = media_service.list_creations(db, current_user, kind=kind, project_id=project_id,
                                                     since=since, until=until, before=before, limit=limit)
    return {
        "creations": [media_service.job_view(db, job) for job in jobs],
        "next_before": next_before.isoformat() if next_before else None,
    }


@router.delete("/creations/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_creation(job_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        await media_service.delete_creation(db, current_user, job_id)
    except MediaRefused as exc:
        raise refusal_http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _live_artifact(db: DBSession, user: User, artifact_id: str):
    """(artifact, store) for a file this person owns and can still open, or the HTTP refusal."""
    try:
        artifact = media_service.owned_artifact(db, user, artifact_id)
    except MediaRefused as exc:
        raise refusal_http(exc)
    expires_at = media_service._aware(artifact.expires_at)
    if expires_at is not None and expires_at <= media_service._now():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This file is no longer available.")
    store = build_object_store()
    if store.backend_name != artifact.storage_backend:
        raise refusal_http(MediaRefused("not_found"))
    return artifact, store


async def _deliver(db: DBSession, user: User, artifact_id: str, *, attachment: bool):
    artifact, store = _live_artifact(db, user, artifact_id)
    url = await store.signed_url(key=artifact.storage_key, expires_in=settings.artifact_signed_url_ttl_s,
                                 filename=artifact.filename if attachment else None)
    if url is not None:
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    data = await store.get(key=artifact.storage_key)
    disposition = "attachment" if attachment else "inline"
    return Response(content=data, media_type=artifact.content_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{artifact.filename}"',
                             "Cache-Control": "private, max-age=300"})


@router.get("/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await _deliver(db, current_user, artifact_id, attachment=False)


@router.get("/artifacts/{artifact_id}/download")
async def artifact_download(artifact_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await _deliver(db, current_user, artifact_id, attachment=True)


@router.get("/artifacts/{artifact_id}/link")
async def artifact_link(artifact_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Short-lived links a page can put straight into <video>/<img> and a download button.

    Reading /content with fetch() follows the redirect to object storage from the page's origin, which the
    browser refuses unless the bucket allows that origin (CORS) — the first video in production was created
    and stored, then "could not be opened". A media element or a link loads the file without CORS.
    Both are None where the storage has no signed URLs (local development): read /content instead.
    """
    artifact, store = _live_artifact(db, current_user, artifact_id)
    ttl = settings.artifact_signed_url_ttl_s
    view = await store.signed_url(key=artifact.storage_key, expires_in=ttl)
    download = await store.signed_url(key=artifact.storage_key, expires_in=ttl, filename=artifact.filename)
    return {"url": view, "download_url": download, "expires_in": ttl if view else None,
            "content_type": artifact.content_type, "filename": artifact.filename}
