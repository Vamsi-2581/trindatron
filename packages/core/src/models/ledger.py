"""
Billing-0 — Financial foundation models.

Three entities, each with a distinct role:

  LedgerEntry      — immutable journal entry for every financial event.
                     Append-only: never UPDATE or DELETE after INSERT.

  CreditBalance    — materialized balance per (user, currency).
                     Updated only via CreditTransaction + optimistic lock.

  CreditTransaction — each debit/credit event with idempotency_key UNIQUE.
                      Prevents duplicate credits/debits across retries.

Design principles
-----------------
* Currency-agnostic: "USD", "BRL", "BTC", "USDT", "TCU", etc.
* Numeric(28, 8) covers BTC (8 dec), USDT/USDC (6), fiat (2), TCU (4).
* Payment != Credit != TCU != AI cost — separate concepts.
* No fiscal rules here — TaxLayer is a future separate concern.
* LedgerEntry has NO updated_at — immutable after creation by design.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level constants to build CHECK constraint SQL strings
_LEDGER_ENTRY_TYPES = (
    "revenue",
    "fee",
    "ai_cost",
    "credit_purchase",
    "credit_consumption",
    "refund",
    "reserve",
    "tax",
    "profit",
    "adjustment",
    # Commercial Checkout V1 — a plan-purchase PaymentOrder's confirmation
    # (see payment_order_service.py's confirm_order()). Revenue recognized
    # WITHOUT a corresponding CreditBalance credit — see PaymentOrder.plan's
    # own docstring for why plan purchases never touch CreditBalance.
    "plan_purchase",
)
_LEDGER_ENTRY_TYPE_SQL = ", ".join(f"'{t}'" for t in _LEDGER_ENTRY_TYPES)

_CREDIT_TX_TYPES = ("purchase", "consume", "refund", "expiry", "grant", "adjustment")
_CREDIT_TX_TYPE_SQL = ", ".join(f"'{t}'" for t in _CREDIT_TX_TYPES)


class LedgerEntry(Base):
    """Immutable journal entry for every financial event.

    Each row records money moving from one account to another.
    Once written, this record is permanent — financial history is never
    rewritten, only compensated by new entries.

    Account string conventions (free-form identifiers):
      "user:<uuid>"     — a user's credit balance
      "revenue"         — platform gross revenue
      "ai_cost"         — cost paid to AI providers
      "fee:<provider>"  — processing fee, e.g. "fee:stripe"
      "reserve"         — platform reserve fund
      "tax:<country>"   — tax withheld, e.g. "tax:br"
      "profit"          — net profit

    For true double-entry: call LedgerService.record() which creates
    the matching pair of entries automatically.
    """

    __tablename__ = "ledger_entries"

    # Expose valid types as a class attribute for service-level validation
    ENTRY_TYPES: frozenset[str] = frozenset(_LEDGER_ENTRY_TYPES)

    # Valid direction values — exported for service-level validation
    DIRECTIONS: frozenset[str] = frozenset(("debit", "credit"))

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Canonical double-entry direction: 'debit' or 'credit'.
    # Every record_double_entry() creates exactly one of each with the same amount.
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    # Double-entry: both the debit and credit entries share the same account_from/to.
    # account_from = where value originates; account_to = where value arrives.
    account_from: Mapped[str] = mapped_column(String(100), nullable=False)
    account_to: Mapped[str] = mapped_column(String(100), nullable=False)
    # Numeric(28, 8) — enough for BTC (8), USDT (6), fiat (2), TCU (4)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    # ISO 4217 for fiat (USD, EUR, BRL), crypto symbol (BTC, USDT, USDC), "TCU" for units
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    # Reference to the source event that caused this entry
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Required for "adjustment" entries; optional elsewhere
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Idempotency: NULL = no requirement; non-NULL = unique key, duplicate returns existing.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # No updated_at — immutable by design
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
        CheckConstraint(
            f"entry_type IN ({_LEDGER_ENTRY_TYPE_SQL})",
            name="ck_ledger_entry_type",
        ),
        CheckConstraint(
            "direction IN ('debit', 'credit')",
            name="ck_ledger_direction",
        ),
        UniqueConstraint("idempotency_key", name="uq_ledger_idempotency_key"),
        Index("ix_ledger_reference", "reference_id", "reference_type"),
        Index("ix_ledger_created_at", "created_at"),
        Index("ix_ledger_account_from", "account_from"),
        Index("ix_ledger_account_to", "account_to"),
        Index("ix_ledger_idempotency_key", "idempotency_key"),
    )


class CreditBalance(Base):
    """Materialized current balance per (user, currency).

    Never modified directly — always updated through CreditTransaction
    which provides idempotency, audit trail, and before/after snapshots.

    Optimistic concurrency via `version`:
      UPDATE credit_balances
         SET balance = :new, version = version + 1, updated_at = :now
       WHERE id = :id AND version = :expected
    Zero rows → concurrent modification detected → caller must retry.

    Currency codes:
      Fiat:        USD, EUR, BRL (ISO 4217)
      Crypto:      BTC, ETH, SOL
      Stablecoin:  USDT, USDC
      Internal:    TCU (Trinity Compute Units)
    """

    __tablename__ = "credit_balances"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, default=Decimal("0")
    )
    # Optimistic lock — incremented on every balance update
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="credit_balances")
    transactions: Mapped[list["CreditTransaction"]] = relationship(
        "CreditTransaction", back_populates="credit_balance", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "currency", name="uq_credit_balance_user_currency"),
        CheckConstraint("balance >= 0", name="ck_credit_balance_non_negative"),
        Index("ix_credit_balance_user_id", "user_id"),
    )


class CreditTransaction(Base):
    """Every debit/credit event with an idempotency guarantee.

    idempotency_key UNIQUE: repeating the same logical operation
    (even across retries or network failures) returns the original
    result rather than creating a duplicate.

    balance_before / balance_after: snapshot audit trail — no need to
    reconstruct balance history by scanning all prior transactions.

    Transaction types:
      purchase    — credits issued after a confirmed external payment
      consume     — credits debited for AI / TCU usage
      refund      — credits returned (payment reversal)
      expiry      — credits expired per plan policy
      grant       — administrative credit grant (requires note)
      adjustment  — manual correction (requires note, admin-only)
    """

    __tablename__ = "credit_transactions"

    TRANSACTION_TYPES: frozenset[str] = frozenset(_CREDIT_TX_TYPES)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    credit_balance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("credit_balances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    # Caller-provided idempotency key — UNIQUE prevents duplicate operations
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Optional reference to source event (payment_id, usage_record_id, etc.)
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Required for grant / adjustment types
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Immutable timestamp — no updated_at by design
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="credit_transactions")
    credit_balance: Mapped["CreditBalance"] = relationship(
        "CreditBalance", back_populates="transactions"
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_credit_tx_idempotency_key"),
        CheckConstraint("amount > 0", name="ck_credit_tx_amount_positive"),
        CheckConstraint(
            f"transaction_type IN ({_CREDIT_TX_TYPE_SQL})",
            name="ck_credit_tx_type",
        ),
        CheckConstraint(
            "balance_after >= 0",
            name="ck_credit_tx_balance_after_non_negative",
        ),
        Index("ix_credit_tx_user_id", "user_id"),
        Index("ix_credit_tx_created_at", "created_at"),
        Index("ix_credit_tx_idempotency", "idempotency_key"),
        Index("ix_credit_tx_reference", "reference_id", "reference_type"),
    )
