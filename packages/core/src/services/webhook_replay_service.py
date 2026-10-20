"""WebhookReplayService — DB-backed replay protection for payment
provider webhooks. See src/models/webhook_nonce.py's own docstring for
the full design and crash-safety analysis; this module is the thin
service layer around that table.

Usage (src/api/routes/billing.py's receive_webhook())
----------------------------------------------------------
  1. CHECK first: `was_nonce_processed(db, provider, nonce)` — if True,
     this exact delivery was already fully handled; skip re-calling
     PaymentOrderService entirely and return the same ack.
  2. Process normally (mark_processing/confirm_order/fail_order) if not
     already processed.
  3. RECORD only after that processing succeeds:
     `record_nonce(db, provider, nonce)` — never before, never on a
     path that didn't actually complete (see webhook_nonce.py's own
     "Crash-safe ordering" section for why this order is the ONE that
     can never block a legitimate not-yet-processed payment).

No new webhook-processing mechanism — this module never calls
PaymentOrderService, never touches PaymentOrder/CreditBalance/LedgerEntry,
never verifies a signature. It only answers "have we already recorded
successfully processing this exact (provider, nonce) before?" — a small,
additive complement to the existing pipeline, not a parallel one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.models.webhook_nonce import WebhookNonce
from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.webhook_replay")

_replay_detected_total = core_metrics.counter(
    "webhook_replay_detected_total", "Webhook deliveries recognized as an already-processed replay, labeled by provider.",
)
_nonces_recorded_total = core_metrics.counter(
    "webhook_nonces_recorded_total", "New (provider, nonce) pairs recorded after successful processing.",
)
_nonces_cleaned_total = core_metrics.counter(
    "webhook_nonces_cleaned_total", "Expired nonce rows deleted by the retention sweep.",
)


def was_nonce_processed(db: DBSession, provider: str, nonce: str) -> bool:
    """True iff (provider, nonce) was already recorded — i.e. a PRIOR
    delivery of this exact webhook was already fully, successfully
    processed. Read-only, no side effect."""
    exists = (
        db.query(WebhookNonce.id)
        .filter(WebhookNonce.provider == provider, WebhookNonce.nonce == nonce)
        .first()
        is not None
    )
    if exists:
        _replay_detected_total.inc(provider=provider)
        logger.info("webhook_replay: replay detected, skipping reprocessing | provider=%s", provider)
    return exists


def record_nonce(db: DBSession, provider: str, nonce: str) -> None:
    """Record (provider, nonce) as fully processed — call ONLY after the
    corresponding webhook's business logic has already completed
    successfully (see module docstring). Idempotent: if a concurrent
    request already recorded the SAME pair (two processes racing the
    same delivery), the resulting IntegrityError is caught and treated
    as success — the pair is recorded either way, exactly once."""
    row = WebhookNonce(provider=provider, nonce=nonce)
    db.add(row)
    try:
        db.commit()
        _nonces_recorded_total.inc()
    except IntegrityError:
        db.rollback()
        logger.debug("webhook_replay: nonce already recorded by a concurrent request | provider=%s", provider)


def cleanup_expired_nonces(db: DBSession, *, retention_seconds: float, now: datetime | None = None) -> int:
    """Deletes nonce rows older than `retention_seconds` — a nonce only
    needs to be retained slightly longer than the signing provider's own
    webhook clock-skew tolerance window (a webhook older than that is
    already rejected by timestamp validation before nonce-checking is
    ever reached), never forever. Returns the count deleted."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=retention_seconds)
    deleted = (
        db.query(WebhookNonce)
        .filter(WebhookNonce.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()
        _nonces_cleaned_total.inc(deleted)
        logger.info("webhook_replay: cleaned up %d expired nonce(s)", deleted)
    return deleted


async def run_webhook_nonce_cleanup_loop(*, stop_event) -> None:
    """Background task started from main.py's lifespan, stopped via
    `stop_event` on shutdown — same in-process asyncio-task pattern as
    every other background loop in this codebase (agent schedule poll,
    execution worker/recovery, payment order expiry) — not a new
    scheduling mechanism."""
    import asyncio

    from src.config import settings
    from src.database import SessionLocal

    logger.info(
        "webhook_replay: nonce cleanup loop started | interval=%.0fs retention=%.0fs",
        settings.webhook_nonce_cleanup_interval_seconds, settings.webhook_nonce_retention_seconds,
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.webhook_nonce_cleanup_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        db = SessionLocal()
        try:
            cleanup_expired_nonces(db, retention_seconds=settings.webhook_nonce_retention_seconds)
        except Exception:
            logger.exception("webhook_replay: nonce cleanup sweep failed")
        finally:
            db.close()
    logger.info("webhook_replay: nonce cleanup loop stopped")
