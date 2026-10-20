import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IdempotencyKey(Base):
    """Etapa 5I — HTTP-level request idempotency for POST /chat, /agent,
    /message. Opt-in: only created when the client sends an
    `Idempotency-Key` header — absence of the header leaves every existing
    request byte-for-byte unaffected (see src/services/idempotency_service.py's
    module docstring for the full contract).

    UNIQUE(user_id, key) is the ONLY thing that makes concurrent-request
    protection correct — two requests racing to claim the same key can
    never both succeed; the DB constraint decides, not application code
    (same principle as CreditTransaction.idempotency_key in
    src/models/ledger.py, reused here rather than re-invented).

    `fingerprint` stores a hash of what makes this a specific, distinct
    logical operation (endpoint + session_id + content) — comparing it on
    every reuse of the same key is what detects "same key, different
    operation" (see IncompatibleIdempotencyKeyError in idempotency_service.py).

    `status`: "in_progress" (claimed, not yet resolved) -> "completed" (a
    real result exists, safe to replay by reference) or "failed" (nothing
    useful was produced, safe to retry as a brand-new attempt under the
    SAME key). Never a fourth value — a row that's still "in_progress" long
    after `updated_at` is either a live concurrent request (still recent)
    or an orphan from a crashed/restarted process (stale) — see
    idempotency_service.py's reconciliation functions for how that
    distinction is made.

    `reservation_id`/`response_session_id`/`response_message_id` are
    reference-only (nullable, no cascade requirement beyond SET NULL) —
    this table is NEVER a second source of financial truth; TCU/billing
    state lives exclusively in UsageRecord/CreditTransaction/LedgerEntry,
    exactly as before this etapa.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(20), nullable=False)  # "chat" | "agent" | "message"
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")

    reservation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("usage_records.id", ondelete="SET NULL"), nullable=True)
    response_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # UTCDateTime — compared in Python against an aware "now" for
    # staleness detection (see database.py::UTCDateTime for why that's
    # required on SQLite).
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
