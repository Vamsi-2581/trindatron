"""
BillingOrchestrator — atomic credit + ledger in a single transaction.

Architecture (Billing-0.5b)
----------------------------
All operations execute inside ONE SQLAlchemy transaction:

  BEGIN (implicit autobegin)
    credit_flush() / debit_flush()       — flush balance + CreditTransaction
    record_double_entry_flush()          — flush debit + credit LedgerEntries
    db.commit()                          — single commit for ALL four writes
  ROLLBACK on any exception              — zero partial state possible

This eliminates the risk identified in Billing-0.5 where credit() could commit
but the subsequent ledger write could fail, leaving the balance without a journal
entry.

Idempotency
-----------
Both credit_flush() and record_double_entry_flush() check for existing records
before creating new ones.  A complete retry of the same operation with the same
idempotency keys returns the existing records without duplicating balance or
journal entries.

Invariants
----------
1. credit/debit/refund flush + ledger flush + ONE commit — atomically or not at all.
2. Any exception triggers db.rollback() — no CreditTransaction without LedgerEntry,
   no LedgerEntry without CreditTransaction.
3. All monetary values are Decimal(28, 8) — no floats.
4. No external payment providers, no webhooks, no public endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session as DBSession

from src.services.credit_service import CreditService
from src.services.ledger_service import LedgerService
from src.models.ledger import LedgerEntry, CreditTransaction


class RefundAmountExceedsEligible(Exception):
    """Raised when a refund amount exceeds the caller-specified max_refundable_amount.

    This is a business-level guard, not a DB constraint. The caller (e.g.
    PaymentOrderService) is responsible for computing max_refundable_amount
    from the original payment. The Orchestrator enforces the ceiling atomically
    before touching any DB state.
    """

    def __init__(self, amount: Decimal, max_refundable: Decimal) -> None:
        self.amount = amount
        self.max_refundable = max_refundable
        super().__init__(
            f"Refund amount {amount} exceeds eligible ceiling {max_refundable}"
        )


@dataclass(frozen=True)
class BillingResult:
    """Outcome of a single BillingOrchestrator operation.

    All three objects are from the same committed transaction.

    Attributes:
        transaction:   The CreditTransaction created or returned (idempotent).
        ledger_debit:  The 'debit' LedgerEntry.
        ledger_credit: The 'credit' LedgerEntry.
    """

    transaction: CreditTransaction
    ledger_debit: LedgerEntry
    ledger_credit: LedgerEntry


class BillingOrchestrator:
    """Coordinates CreditService and LedgerService in a single DB transaction.

    Usage:
        orchestrator = BillingOrchestrator(db)
        result = orchestrator.credit_and_record(
            user_id=user_id,
            currency="USD",
            amount=Decimal("10.00"),
            transaction_type="purchase",
            ledger_entry_type="credit_purchase",
            account_debit=f"user:{user_id}",
            account_credit="revenue",
            credit_idempotency_key="pay-abc123",
            ledger_idempotency_key="pay-abc123:ledger",
        )
    """

    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._credit_svc = CreditService(db)
        self._ledger_svc = LedgerService(db)

    def credit_and_record(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        transaction_type: str,
        ledger_entry_type: str,
        account_debit: str,
        account_credit: str,
        credit_idempotency_key: str,
        ledger_idempotency_key: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> BillingResult:
        """Credit a user's balance and record a canonical double-entry pair atomically.

        Transaction flow:
          1. credit_flush()              — flush balance update + CreditTransaction
          2. record_double_entry_flush() — flush debit + credit LedgerEntries
          3. db.commit()                 — ONE commit persists all four writes

        On any exception: db.rollback() → zero rows persisted.

        Idempotent: supplying the same keys on retry returns existing records.
        """
        try:
            tx = self._credit_svc.credit_flush(
                user_id=user_id,
                currency=currency,
                amount=amount,
                idempotency_key=credit_idempotency_key,
                transaction_type=transaction_type,
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
            )
            debit_entry, credit_entry = self._ledger_svc.record_double_entry_flush(
                entry_type=ledger_entry_type,
                account_debit=account_debit,
                account_credit=account_credit,
                amount=amount,
                currency=currency,
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
                idempotency_key=ledger_idempotency_key,
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return BillingResult(
            transaction=tx,
            ledger_debit=debit_entry,
            ledger_credit=credit_entry,
        )

    def debit_and_record(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        transaction_type: str,
        ledger_entry_type: str,
        account_debit: str,
        account_credit: str,
        credit_idempotency_key: str,
        ledger_idempotency_key: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> BillingResult:
        """Debit a user's balance and record a canonical double-entry pair atomically.

        Transaction flow:
          1. debit_flush()               — flush balance update + CreditTransaction
          2. record_double_entry_flush() — flush debit + credit LedgerEntries
          3. db.commit()                 — ONE commit persists all four writes

        On any exception (including InsufficientCreditError): db.rollback()
        → zero rows persisted.  No CreditTransaction without LedgerEntry,
        no LedgerEntry without CreditTransaction.

        Raises:
            InsufficientCreditError: balance too low — nothing persisted.
        """
        try:
            tx = self._credit_svc.debit_flush(
                user_id=user_id,
                currency=currency,
                amount=amount,
                idempotency_key=credit_idempotency_key,
                transaction_type=transaction_type,
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
            )
            debit_entry, credit_entry = self._ledger_svc.record_double_entry_flush(
                entry_type=ledger_entry_type,
                account_debit=account_debit,
                account_credit=account_credit,
                amount=amount,
                currency=currency,
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
                idempotency_key=ledger_idempotency_key,
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return BillingResult(
            transaction=tx,
            ledger_debit=debit_entry,
            ledger_credit=credit_entry,
        )

    def refund_and_record(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        ledger_entry_type: str,
        account_debit: str,
        account_credit: str,
        credit_idempotency_key: str,
        ledger_idempotency_key: str,
        max_refundable_amount: Decimal | None = None,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> BillingResult:
        """Remove credits from a user and record a canonical double-entry pair atomically.

        A PaymentOrder refund is the financial reverse of a purchase: it debits the
        user's credit balance (removing credits) and appends two immutable LedgerEntries
        to the journal.  All four writes are committed in a single transaction —
        atomically or not at all.

        Transaction flow:
          1. Guard: amount <= max_refundable_amount (before any DB access)
          2. debit_flush(transaction_type='refund') — flush balance + CreditTransaction
          3. record_double_entry_flush()             — flush debit + credit LedgerEntries
          4. db.commit()                             — ONE commit persists all four writes

        On any exception: db.rollback() → zero rows persisted.

        Canonical account direction for a PaymentOrder refund:
          account_debit  = "user:<uuid>"     (credits leave the user's balance)
          account_credit = "revenue"         (counterpart account — the purchase being reversed)

        Idempotent: supplying the same keys on retry returns existing records.

        Args:
            user_id:                  User whose credit balance is debited.
            currency:                 Credit currency (e.g. "USD", "TCU").
            amount:                   Strictly positive refund amount (Decimal).
            ledger_entry_type:        LedgerEntry type, typically "refund".
            account_debit:            Account whose credits are debited (e.g. "user:<uuid>").
            account_credit:           Counterpart account in the double-entry (e.g. "revenue").
            credit_idempotency_key:   Idempotency key for the CreditTransaction.
            ledger_idempotency_key:   Idempotency key for the LedgerEntry pair.
            max_refundable_amount:    Optional ceiling. If provided, amount must be <=
                                      this value. The caller (PaymentOrderService) is
                                      responsible for computing it from the original payment.
                                      None = no ceiling check applied.
            reference_id:             Optional source event ID (e.g. PaymentOrder.id).
            reference_type:           Optional source event type (e.g. "payment_order").
            note:                     Optional audit note.

        Raises:
            RefundAmountExceedsEligible: amount > max_refundable_amount.
            ValueError:                 amount <= 0 or invalid ledger_entry_type.
            TypeError:                  amount is not Decimal.
        """
        if max_refundable_amount is not None and amount > max_refundable_amount:
            raise RefundAmountExceedsEligible(amount, max_refundable_amount)

        try:
            tx = self._credit_svc.debit_flush(
                user_id=user_id,
                currency=currency,
                amount=amount,
                idempotency_key=credit_idempotency_key,
                transaction_type="refund",
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
            )
            debit_entry, credit_entry = self._ledger_svc.record_double_entry_flush(
                entry_type=ledger_entry_type,
                account_debit=account_debit,
                account_credit=account_credit,
                amount=amount,
                currency=currency,
                reference_id=reference_id,
                reference_type=reference_type,
                note=note,
                idempotency_key=ledger_idempotency_key,
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return BillingResult(
            transaction=tx,
            ledger_debit=debit_entry,
            ledger_credit=credit_entry,
        )
