from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeSetting(Base):
    """Etapa 5H — the ONE key-value store for server-side operational
    kill switches (src/services/runtime_settings.py owns all read/write
    access and the fail-safe defaults; nothing else touches this table
    directly). `key` is one of the small, fixed set of known keys defined
    there — this model itself doesn't restrict which keys can exist so
    that a corrupted/unexpected key found at read time can still be
    reported cleanly, but runtime_settings.py's `set_setting()` refuses to
    ever write an unknown key.

    ONE ROW PER KEY (not a single-row singleton blob) — each switch is
    independently readable/writable, and a missing row for a specific key
    is a normal, expected state (see runtime_settings.py's fail-safe
    defaults), not a migration/backfill concern.
    """
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    updated_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
