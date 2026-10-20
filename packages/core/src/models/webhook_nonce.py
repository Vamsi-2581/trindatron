"""WebhookNonce — DB-backed replay protection for payment provider webhooks.

Production Payment Activation hardening. Closes the documented gap in
BinancePayAdapter.verify_webhook() ("does NOT track nonce reuse — order-
state idempotency in PaymentOrderService is the actual backstop") with a
real, persisted, multi-process-safe mechanism, additive to (never a
replacement for) that existing backstop.

Why a new table, not a reuse of `idempotency_keys`
--------------------------------------------------------
`IdempotencyKey` (src/models/idempotency_key.py) is scoped to an
AUTHENTICATED user (`user_id` is a NOT NULL FK) — it exists for
POST /chat, /agent, /message, all of which require a JWT. A webhook has
no authenticated user at all (it is provider-initiated, verified by
signature, not a JWT) — forcing it through that table would mean
inventing a fake user_id, a worse hack than a small new table.

Crash-safe ordering (CRITICAL — see api/routes/billing.py's
receive_webhook())
--------------------------------------------------------------------------
A nonce is recorded ONLY AFTER the webhook's business logic (mark_processing/
confirm_order/fail_order) has ALREADY completed successfully — never
before, never instead of. This means:
  - A crash between "event verified" and "nonce recorded" leaves NO row
    here — a legitimate retry (Binance retries up to 6 times per its own
    docs) is correctly reprocessed, safe because confirm_order() etc. are
    already idempotent (this table only ever makes replay detection
    FASTER, never a gate that could block a not-yet-processed payment).
  - Once recorded, a LATER delivery of the exact same (provider, nonce)
    is short-circuited before touching PaymentOrderService again at all —
    "mesma notificação repetida não pode processar pagamento novamente"
    is satisfied literally, not just via idempotent-no-op.
  - Multi-process safe by construction: UNIQUE(provider, nonce) is the
    arbiter, not application-level locking — the same "let the DB decide"
    principle this codebase already uses for AgentSchedule/ExecutionJob
    claims. Two processes racing to record the SAME nonce: exactly one
    INSERT succeeds, the other hits IntegrityError and is caught,
    matching the established `IntegrityError`-recovery convention
    already used throughout PaymentOrderService.

TTL / cleanup
-------------
See src/services/webhook_replay_service.py's cleanup_expired_nonces() and
run_webhook_nonce_cleanup_loop() — a nonce only needs to be retained
slightly longer than the signing provider's own clock-skew tolerance
window (a webhook older than that is already rejected by timestamp
validation before nonce-checking is ever reached), never forever.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebhookNonce(Base):
    """One row per (provider, nonce) pair whose webhook has been fully,
    successfully processed. Presence = "do not reprocess this delivery."
    Absence = either never seen, or seen but not yet successfully
    processed (safe to process now)."""

    __tablename__ = "webhook_nonces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "nonce", name="uq_webhook_nonce_provider_nonce"),
        Index("ix_webhook_nonce_created_at", "created_at"),
    )
