import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Etapa 5H — the ONLY authorization bit for admin/operational endpoints
    # (runtime settings / kill switches). Deliberately a plain boolean, not
    # a roles/permissions system: reuses the exact same JWT/get_current_user
    # mechanism already in place (see src/api/deps.py::get_current_admin_user),
    # never a second auth system. Default False — granting admin access is
    # an explicit, out-of-band action (direct DB write / future admin
    # tooling), never a side effect of registration.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # "free", never a paid plan: a paid plan is granted only by a confirmed
    # payment (PaymentOrderService.confirm_order). The default used to be
    # "start", which handed every new account a paid entitlement.
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    # TEAM plan only: the grouping key a shared usage pool is summed over
    # (src/services/entitlement_service.py::_sum_team_active_tcu). NULL for
    # every individual plan, and NULL on a TEAM account that has no other
    # members yet — which correctly degrades to "the pool is just me".
    #
    # A plain grouping key rather than a full teams/roles/invites feature:
    # the pool needs to know WHO shares a budget, and nothing more. Assigned
    # out of band today (admin route), so nothing here presumes a team
    # management UI that does not exist.
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, default=None)
    # IANA timezone (e.g. "America/Sao_Paulo"), detected client-side via
    # Intl.DateTimeFormat().resolvedOptions().timeZone and persisted via
    # PATCH /users/me/timezone. NULL means "not yet detected" — NEVER
    # defaulted to "UTC" here, since that would misrepresent a guess as the
    # user's real, detected timezone. See src/timezones.py: computations
    # that need a timezone before detection happens use FALLBACK_TIMEZONE
    # explicitly, as a documented computational default, not stored data.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Account security (src/services/account_security_service.py). When the
    # owner proved they receive mail at `email` — a confirmation link, a
    # password reset, or a Google sign-in (Google verifies the address). NULL
    # means not yet proven; nothing is blocked on it today.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    # Last password set by reset or change. An access token issued before it no
    # longer authenticates (src/api/deps.py), so a stolen token dies with the
    # old password instead of living out its lifetime.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    # Wrong passwords in a row, and when the latest one was. Enough of them
    # inside the lockout window lock password sign-in until `locked_until`.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failed_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    sessions: Mapped[list["Session"]] = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    usage_records: Mapped[list["UsageRecord"]] = relationship("UsageRecord", back_populates="user")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="user", cascade="all, delete-orphan")
    skills: Mapped[list["Skill"]] = relationship("Skill", back_populates="user", cascade="all, delete-orphan")
    agent_definitions: Mapped[list["AgentDefinition"]] = relationship("AgentDefinition", back_populates="user", cascade="all, delete-orphan")
    memories: Mapped[list["Memory"]] = relationship("Memory", back_populates="user", cascade="all, delete-orphan")
    credit_balances: Mapped[list["CreditBalance"]] = relationship("CreditBalance", back_populates="user", cascade="all, delete-orphan")
    credit_transactions: Mapped[list["CreditTransaction"]] = relationship("CreditTransaction", back_populates="user", cascade="all, delete-orphan")
    # No cascade delete — RESTRICT on the FK already blocks user deletion
    # while a Subscription row exists (financial audit safety, same
    # convention as PaymentOrder's own user_id FK).
    subscriptions: Mapped[list["Subscription"]] = relationship("Subscription", back_populates="user")
