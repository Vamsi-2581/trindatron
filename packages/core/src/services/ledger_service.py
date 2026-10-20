"""
LedgerService — append-only financial journal.

The ledger is the permanent audit trail of all money movement in TRINITY.
Once a LedgerEntry is written, it is NEVER updated or deleted.
Financial history is never rewritten; corrections are new entries.

Canonical double-entry accounting (Billing-0.5)
-----------------------------------------------
Every economic event is represented as exactly two entries sharing the same
account_from / account_to pair but carrying opposite directions:

  direction='debit'   — the debit side  (value leaves account_from)
  direction='credit'  — the credit side (value arrives at account_to)

Both entries carry the same amount. This satisfies the fundamental ledger
invariant: Σ(debits) == Σ(credits) across the entire journal.

The OLD non-canonical approach (mirrored A→B + B→A) produced net-zero per
account but not a real double-entry — replaced entirely in Billing-0.5.

Idempotency
-----------
record() and record_double_entry() accept an optional idempotency_key.
If a key is supplied and a matching entry already exists:
  - same amount/currency/direction/entry_type → return the existing entry
  - conflicting params                        → raise DuplicateLedgerKey

Account naming conventions
--------------------------
  "user:<uuid>"     — a specific user's credit balance
  "revenue"         — platform gross revenue
  "ai_cost"         — cost paid to upstream AI providers
  "fee:<provider>"  — processing fee, e.g. "fee:stripe"
  "reserve"         — platform reserve fund
  "tax:<country>"   — tax withheld, e.g. "tax:br"
  "profit"          — net profit after costs and fees
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.models.ledger import LedgerEntry

logger = logging.getLogger(__name__)


class DuplicateLedgerKey(Exception):
    """Raised when an idempotency_key already exists with conflicting parameters.

    Attributes:
        key:            The conflicting idempotency_key.
        existing_entry: The entry that is already stored.
        field:          The field whose value conflicts.
        existing_value: The value stored in the existing entry.
        new_value:      The value the caller attempted to write.
    """

    def __init__(
        self,
        key: str,
        existing_entry: LedgerEntry,
        field: str,
        existing_value: object,
        new_value: object,
    ) -> None:
        self.key = key
        self.existing_entry = existing_entry
        self.field = field
        self.existing_value = existing_value
        self.new_value = new_value
        super().__init__(
            f"Idempotency conflict for ledger key '{key}': "
            f"field '{field}' stored as {existing_value!r}, "
            f"caller passed {new_value!r}"
        )


class LedgerService:
    def __init__(self, db: DBSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        entry_type: str,
        direction: str,
        account_from: str,
        account_to: str,
        amount: Decimal,
        currency: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> LedgerEntry:
        """Create a single immutable journal entry.

        This method flushes the entry to the current transaction but does NOT
        commit. The caller owns the transaction and must call db.commit() when
        all related operations (balance update, payment record, etc.) are done.
        This ensures that ledger writes and balance writes are committed atomically.

        To create an atomic canonical double-entry pair in one commit, use
        record_double_entry() instead.

        Args:
            direction:        'debit' or 'credit' — canonical direction flag.
            idempotency_key:  Optional caller-provided key. If supplied and a
                              matching entry exists with the same params, the
                              existing entry is returned. Conflicting params
                              raise DuplicateLedgerKey.

        Raises:
            ValueError:         if entry_type or direction is invalid, or amount <= 0.
            TypeError:          if amount is not Decimal.
            DuplicateLedgerKey: if idempotency_key already exists with different params.
        """
        self._validate_entry_type(entry_type)
        self._validate_direction(direction)
        self._validate_amount(amount)

        # Check idempotency before writing
        if idempotency_key is not None:
            existing = self._find_existing_entry(idempotency_key)
            if existing is not None:
                self._check_idempotency_params(
                    existing, entry_type, direction, amount, currency, idempotency_key
                )
                return existing

        entry = LedgerEntry(
            entry_type=entry_type,
            direction=direction,
            account_from=account_from,
            account_to=account_to,
            amount=amount,
            currency=currency,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            idempotency_key=idempotency_key,
        )
        self._db.add(entry)
        try:
            self._db.flush()  # assign id within current transaction — caller must commit
        except IntegrityError:
            self._db.rollback()
            if idempotency_key is not None:
                existing = self._find_existing_entry(idempotency_key)
                if existing is not None:
                    self._check_idempotency_params(
                        existing, entry_type, direction, amount, currency, idempotency_key
                    )
                    return existing
            raise
        self._db.refresh(entry)

        logger.debug(
            "ledger entry (flushed) | type=%s dir=%s from=%s to=%s amount=%s %s ref=%s/%s",
            entry_type, direction, account_from, account_to, amount, currency,
            reference_type, reference_id,
        )
        return entry

    def record_double_entry(
        self,
        *,
        entry_type: str,
        account_debit: str,
        account_credit: str,
        amount: Decimal,
        currency: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[LedgerEntry, LedgerEntry]:
        """Create a canonical double-entry pair and commit.

        Canonical structure (Billing-0.5):
          debit_entry  — direction='debit',  account_from=account_debit, account_to=account_credit
          credit_entry — direction='credit', account_from=account_debit, account_to=account_credit

        Both entries share the same account_from/account_to pair and the same amount.
        The invariant Σdebits == Σcredits holds across the journal by construction.

        If idempotency_key is supplied, the key is suffixed with ':debit' and ':credit'
        for the two entries. If both already exist, the existing pair is returned.

        Returns (debit_entry, credit_entry).

        Raises:
            ValueError:         if entry_type is invalid or amount <= 0.
            TypeError:          if amount is not Decimal.
            DuplicateLedgerKey: if either idempotency suffix already exists with conflicting params.
        """
        return self._double_entry_impl(
            entry_type=entry_type,
            account_debit=account_debit,
            account_credit=account_credit,
            amount=amount,
            currency=currency,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            idempotency_key=idempotency_key,
            _commit=True,
        )

    def record_double_entry_flush(
        self,
        *,
        entry_type: str,
        account_debit: str,
        account_credit: str,
        amount: Decimal,
        currency: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[LedgerEntry, LedgerEntry]:
        """Create a canonical double-entry pair and flush — caller owns the transaction.

        Identical to record_double_entry() but calls db.flush() instead of
        db.commit().  The caller MUST call db.commit() on success or db.rollback()
        on failure.  Designed for BillingOrchestrator's single-transaction flow.

        Raises:
            ValueError, TypeError, DuplicateLedgerKey: same as record_double_entry().
            IntegrityError: concurrent idempotency race — propagated; caller rollbacks.
        """
        return self._double_entry_impl(
            entry_type=entry_type,
            account_debit=account_debit,
            account_credit=account_credit,
            amount=amount,
            currency=currency,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            idempotency_key=idempotency_key,
            _commit=False,
        )

    def _double_entry_impl(
        self,
        *,
        entry_type: str,
        account_debit: str,
        account_credit: str,
        amount: Decimal,
        currency: str,
        reference_id: str | None,
        reference_type: str | None,
        note: str | None,
        idempotency_key: str | None,
        _commit: bool,
    ) -> tuple[LedgerEntry, LedgerEntry]:
        """Shared implementation for record_double_entry and record_double_entry_flush."""
        self._validate_entry_type(entry_type)
        self._validate_amount(amount)

        debit_key = f"{idempotency_key}:debit" if idempotency_key else None
        credit_key = f"{idempotency_key}:credit" if idempotency_key else None

        # Idempotency check: if both entries already exist, return them.
        if debit_key is not None:
            existing_debit = self._find_existing_entry(debit_key)
            existing_credit = self._find_existing_entry(credit_key)
            if existing_debit is not None and existing_credit is not None:
                self._check_idempotency_params(
                    existing_debit, entry_type, "debit", amount, currency, debit_key
                )
                self._check_idempotency_params(
                    existing_credit, entry_type, "credit", amount, currency, credit_key
                )
                return existing_debit, existing_credit

        debit_entry = LedgerEntry(
            entry_type=entry_type,
            direction="debit",
            account_from=account_debit,
            account_to=account_credit,
            amount=amount,
            currency=currency,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            idempotency_key=debit_key,
        )
        credit_entry = LedgerEntry(
            entry_type=entry_type,
            direction="credit",
            account_from=account_debit,
            account_to=account_credit,
            amount=amount,
            currency=currency,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            idempotency_key=credit_key,
        )

        self._db.add(debit_entry)
        self._db.add(credit_entry)

        if _commit:
            self._db.commit()
        else:
            self._db.flush()

        self._db.refresh(debit_entry)
        self._db.refresh(credit_entry)

        logger.debug(
            "double-entry | type=%s debit=%s credit=%s amount=%s %s ref=%s/%s commit=%s",
            entry_type, account_debit, account_credit, amount, currency,
            reference_type, reference_id, _commit,
        )
        return debit_entry, credit_entry

    def get_entries(
        self,
        *,
        reference_id: str | None = None,
        reference_type: str | None = None,
        account: str | None = None,
        entry_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LedgerEntry]:
        """Query ledger entries. Filters are ANDed together."""
        q = self._db.query(LedgerEntry)
        if reference_id:
            q = q.filter(LedgerEntry.reference_id == reference_id)
        if reference_type:
            q = q.filter(LedgerEntry.reference_type == reference_type)
        if entry_type:
            q = q.filter(LedgerEntry.entry_type == entry_type)
        if account:
            q = q.filter(
                (LedgerEntry.account_from == account)
                | (LedgerEntry.account_to == account)
            )
        return (
            q.order_by(LedgerEntry.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_entry_type(entry_type: str) -> None:
        if entry_type not in LedgerEntry.ENTRY_TYPES:
            raise ValueError(
                f"Invalid entry_type '{entry_type}'. "
                f"Must be one of: {sorted(LedgerEntry.ENTRY_TYPES)}"
            )

    @staticmethod
    def _validate_direction(direction: str) -> None:
        if direction not in LedgerEntry.DIRECTIONS:
            raise ValueError(
                f"Invalid direction '{direction}'. Must be 'debit' or 'credit'."
            )

    @staticmethod
    def _validate_amount(amount: Decimal) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError(f"amount must be Decimal, got {type(amount).__name__}")
        if amount <= Decimal("0"):
            raise ValueError(f"amount must be positive, got {amount}")

    def _find_existing_entry(self, idempotency_key: str) -> LedgerEntry | None:
        return (
            self._db.query(LedgerEntry)
            .filter(LedgerEntry.idempotency_key == idempotency_key)
            .first()
        )

    @staticmethod
    def _check_idempotency_params(
        existing: LedgerEntry,
        entry_type: str,
        direction: str,
        amount: Decimal,
        currency: str,
        key: str,
    ) -> None:
        """Raise DuplicateLedgerKey if the stored entry conflicts with caller params."""
        checks = [
            ("entry_type", existing.entry_type, entry_type),
            ("direction", existing.direction, direction),
            ("amount", existing.amount, amount),
            ("currency", existing.currency, currency),
        ]
        for field, stored, new in checks:
            if stored != new:
                raise DuplicateLedgerKey(
                    key=key,
                    existing_entry=existing,
                    field=field,
                    existing_value=stored,
                    new_value=new,
                )
