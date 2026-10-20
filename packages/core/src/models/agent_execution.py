import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentExecution(Base):
    """Etapa 5I — persisted lifecycle record for one stream_agent() run.

    Created for EVERY Agent execution (unlike IdempotencyKey, which only
    exists when the client opts in via the Idempotency-Key header) — this
    is unconditional diagnostic/recovery state, not a dedup mechanism.
    Answers exactly the three questions Etapa 5I's spec requires: (1) an
    execution existed, (2) what its state was, (3) it never stays
    ambiguously "in progress" forever (see the reconciliation functions in
    src/services/agent_execution_service.py).

    States (all five explicitly required by the spec):
      admitted    — Request Control + kill switches + Agent Scheduler +
                    TCU reservation all succeeded; about to call
                    stream_agent(). Set in sessions.py, BEFORE the
                    StreamingResponse is constructed.
      running     — stream_agent() itself started executing. Set as the
                    first statement inside its own try block.
      completed   — reached its own success point (mirrors the existing
                    `_cancel_on_exit = False` marker exactly).
      failed      — a handled exception path (BudgetExceededError /
                    ProviderUnavailableError / generic Exception) — the
                    client already received a clean SSE "error" event.
      interrupted — GeneratorExit / asyncio.CancelledError (client
                    disconnect, or with_execution_timeout() cutting it
                    off) OR detected stale at reconciliation (a row still
                    "admitted"/"running" from a process that no longer
                    exists) — covers the spec's "estado recuperável/
                    ambíguo" case without inventing a sixth status.

    NO NEW FINANCIAL ACCOUNTING: `reservation_id`/`continuity_execution_id`
    are reference-only pointers into the EXISTING UsageRecord/Continuity
    machinery — this table never computes or stores cost/TCU itself, and
    reconciliation (agent_execution_service.py) only ever calls the
    ALREADY-EXISTING cancel_reservation()/true_up_continuity() primitives,
    never invents new refund/charge logic.
    """

    __tablename__ = "agent_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("usage_records.id", ondelete="SET NULL"), nullable=True)
    continuity_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("idempotency_keys.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="admitted")
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    iterations_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # UTCDateTime — compared in Python for staleness detection, same
    # rationale as IdempotencyKey.updated_at.
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
