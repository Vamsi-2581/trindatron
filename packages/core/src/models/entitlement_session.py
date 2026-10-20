import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EntitlementSession(Base):
    """5-hour rolling entitlement session window.

    NOT the chat conversation Session (models/session.py) and NOT tied to
    billing/credits (src/billing/) — this table has no FK to UsageRecord or
    any financial table. It exists solely to answer "does this user have an
    active 5-hour activity window right now, and when did/does it start/end".

    One row per user (see UniqueConstraint below) — this is current STATE,
    not a history log. A new 5-hour window overwrites session_started_at/
    session_expires_at in place; see src/services/session_window_service.py
    for the resolve-or-start logic and the exact product rule it implements.
    """

    __tablename__ = "entitlement_sessions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_entitlement_sessions_user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # UTCDateTime (not plain DateTime(timezone=True)): these two are
    # compared in Python against an aware "now" in session_window_service —
    # see database.py::UTCDateTime for why that requires it on SQLite.
    session_started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    session_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
