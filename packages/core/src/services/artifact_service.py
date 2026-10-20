"""Artifact delivery — ownership, then a short-lived URL, then bytes.

The rule this module exists to enforce is that access to a generated file is
decided HERE, against the database, and never by possession of a storage key.
Keys are guessable in shape (`presentations/<user>/<job>/presentation.pptx`),
so a key must never be an authorization token. Every path below starts from
the authenticated user id and joins to the artifact through the job that owns
it; an artifact belonging to someone else is reported as missing rather than
forbidden, matching the no-existence-leak rule the execution routes already
follow.

Two delivery shapes, one contract
---------------------------------
  signed URL   R2 can mint a time-limited GET. The bytes then travel
               Cloudflare-to-browser and never through this process, which is
               the point: a 10 MB deck should not occupy an API worker.
  streamed     The local store has no URL to give (see storage/local.py), so
               the caller streams. Correct, slower, and used in development.

download_target() returns whichever is available, and the route renders it.
Callers never ask which backend is in use.

Retention
---------
sweep_expired() removes objects whose expires_at has passed and deletes the
row with them. It is ordinary housekeeping, not a security control — the
signed URL's own expiry is what bounds access, and it is measured in minutes
where retention is measured in days.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.execution import ExecutionArtifact, ExecutionJob
from src.observability.metrics import core_metrics
from src.storage import build_object_store
from src.storage.object_store import ObjectStore, ObjectStoreError

logger = logging.getLogger("trinity.artifacts")

_downloads = core_metrics.counter(
    "artifact_downloads_total", "Artifact download requests, labeled by outcome.",
)


class ArtifactNotFound(Exception):
    """The artifact does not exist, or does not belong to this user. One
    exception for both on purpose — the caller must not be able to tell them
    apart, or it could enumerate other people's jobs."""


class ArtifactExpired(Exception):
    """Past its retention date. Distinct from not-found because the user DID
    own this and is entitled to know it aged out rather than never existed."""


@dataclass(frozen=True)
class DownloadTarget:
    """Either a URL to redirect to, or bytes to stream. Never both."""

    filename: str
    media_type: str
    size_bytes: int
    url: str | None = None
    data: bytes | None = None


def _owned_artifact(db: DBSession, artifact_id: str, user_id: str) -> ExecutionArtifact:
    """The join that IS the authorization. Ownership comes from the job, which
    is the only place it is recorded, so there is no way to satisfy this query
    with a key alone."""
    artifact = (
        db.query(ExecutionArtifact)
        .join(ExecutionJob, ExecutionArtifact.job_id == ExecutionJob.id)
        .filter(ExecutionArtifact.id == artifact_id, ExecutionJob.user_id == user_id)
        .first()
    )
    if artifact is None:
        raise ArtifactNotFound(artifact_id)
    return artifact


def list_job_artifacts(db: DBSession, job_id: str, user_id: str) -> list[ExecutionArtifact]:
    """Artifacts for one job the caller owns. An unowned or unknown job
    returns empty rather than raising — the job routes already answer 404 for
    that case and this should not become a second existence oracle."""
    return (
        db.query(ExecutionArtifact)
        .join(ExecutionJob, ExecutionArtifact.job_id == ExecutionJob.id)
        .filter(ExecutionArtifact.job_id == job_id, ExecutionJob.user_id == user_id)
        .order_by(ExecutionArtifact.created_at.asc())
        .all()
    )


async def download_target(
    db: DBSession, artifact_id: str, user_id: str, *, store: ObjectStore | None = None
) -> DownloadTarget:
    """Authorize, then produce a way to fetch the file."""
    artifact = _owned_artifact(db, artifact_id, user_id)

    if artifact.expires_at is not None and artifact.expires_at <= datetime.now(timezone.utc):
        _downloads.inc(outcome="expired")
        raise ArtifactExpired(artifact_id)

    media_type = artifact.content_type or "application/octet-stream"

    if not artifact.storage_backend or not artifact.storage_key:
        # A workspace-filesystem artifact from a shell/research job. Those
        # live on a directory the worker deletes after the job, so there is
        # nothing to serve; treated as gone rather than pretended into
        # existence.
        _downloads.inc(outcome="not_stored")
        raise ArtifactNotFound(artifact_id)

    store = store or build_object_store()
    url = await store.signed_url(
        key=artifact.storage_key,
        expires_in=settings.artifact_signed_url_ttl_s,
        filename=artifact.filename,
    )
    if url is not None:
        _downloads.inc(outcome="signed_url")
        return DownloadTarget(
            filename=artifact.filename, media_type=media_type,
            size_bytes=artifact.size_bytes, url=url,
        )

    data = await store.get(key=artifact.storage_key)
    _downloads.inc(outcome="streamed")
    return DownloadTarget(
        filename=artifact.filename, media_type=media_type,
        size_bytes=artifact.size_bytes, data=data,
    )


async def sweep_expired(db: DBSession, *, limit: int = 200, store: ObjectStore | None = None) -> int:
    """Delete stored objects past their retention date, and their rows.

    The object goes first. If removing the row succeeded but the object did
    not, storage would keep paying for a file nothing points at — an orphan
    with no way left to find it. The reverse (object gone, row still present)
    is self-correcting: the next sweep sees the row, the delete is idempotent,
    and the row goes.
    """
    now = datetime.now(timezone.utc)
    due = (
        db.query(ExecutionArtifact)
        .filter(
            ExecutionArtifact.expires_at.isnot(None),
            ExecutionArtifact.expires_at <= now,
            ExecutionArtifact.storage_key.isnot(None),
        )
        .limit(limit)
        .all()
    )
    if not due:
        return 0

    store = store or build_object_store()
    removed = 0
    for artifact in due:
        try:
            await store.delete(key=artifact.storage_key)
        except ObjectStoreError:
            # Leave the row. Next sweep retries; dropping it here would
            # abandon a paid-for object with nothing left pointing at it.
            logger.warning("artifacts: could not delete stored object | id=%s", artifact.id)
            continue
        db.delete(artifact)
        removed += 1
    db.commit()
    if removed:
        logger.info("artifacts: retention sweep removed %d artifact(s)", removed)
    return removed


async def run_artifact_retention_loop(*, stop_event) -> None:
    """Background retention sweep, started from main.py's lifespan and stopped
    via `stop_event` on shutdown — the same in-process asyncio-task pattern
    every other sweep in this codebase uses (entitlement_service's reservation
    reclaim, execution_worker's recovery loop, payment_order_service's expiry
    loop), not a new scheduling mechanism.

    Runs daily rather than every few minutes: retention is measured in days,
    so a sweep that fires more often would spend queries to discover nothing
    has aged out. It is also deliberately NOT a security control — the signed
    URL's own expiry, measured in minutes, is what bounds access. This only
    stops storage growing without bound.
    """
    import asyncio

    from src.database import SessionLocal

    interval_s = 24 * 60 * 60
    logger.info(
        "artifacts: retention loop started | interval=%.0fs retention_days=%d",
        interval_s, settings.artifact_retention_days,
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        db = SessionLocal()
        try:
            await sweep_expired(db)
        except Exception:
            logger.exception("artifacts: retention sweep failed")
        finally:
            db.close()
    logger.info("artifacts: retention loop stopped")
