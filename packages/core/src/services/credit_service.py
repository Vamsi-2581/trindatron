"""
CreditService — user credit balance management.

Responsibilities
----------------
* credit()       — add credits after a confirmed payment (commits internally)
* credit_flush() — add credits without committing (caller owns the transaction)
* debit()        — subtract credits for AI / TCU consumption (commits internally)
* debit_flush()  — subtract credits without committing (caller owns the transaction)
* refund()       — return credits (payment reversal)
* balance()      — read current balance (Decimal, never float)

Flush-only variants (credit_flush / debit_flush)
-------------------------------------------------
These methods execute the full business logic (validation, idempotency, optimistic
locking) but call db.flush() instead of db.commit().  The caller is responsible
for the final commit and for calling db.rollback() on any exception.  They are
designed exclusively for use by BillingOrchestrator, which needs to combine the
credit operation and the ledger write in a single atomic transaction.

Key differences from the committing variants:
  - No internal db.rollback() on conflict — the caller's outer rollback handles it.
  - No retry loop on ConcurrentModificationError — the conflict propagates so the
    caller can rollback the outer transaction and let the upstream retry.
  - IntegrityError (concurrent idempotency race) propagates to the caller.

Guarantees
----------
1. Idempotency: every operation requires an idempotency_key. Duplicate keys
   return the original CreditTransaction without creating a second entry.
   Race-safe: concurrent calls that both pass the initial check trigger a
   DB-level UNIQUE violation on commit; the loser rolls back, re-fetches the
   winner's committed transaction, and returns it idempotently.

2. Optimistic concurrency: balance updates use a WHERE version = :expected
   UPDATE. Zero rows → concurrent modification → ConcurrentModificationError.
   The service retries up to _MAX_RETRIES times with exponential back-off
   (commit variants only).

3. Non-negative balances: debit raises InsufficientCreditError before touching
   the DB if the balance would go below zero.

4. Audit trail: every balance change creates a CreditTransaction with
   balance_before / balance_after snapshots.

Currency codes
--------------
Fiat:        USD, EUR, BRL (ISO 4217)
Crypto:      BTC, ETH, SOL
Stablecoin:  USDT, USDC
Internal:    TCU (Trinity Compute Units)

Each (user, currency) pair has an independent CreditBalance row.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.models.ledger import CreditBalance, CreditTransaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InsufficientCreditError(Exception):
    """Raised when a debit would make the balance go negative."""

    def __init__(self, user_id: str, currency: str, balance: Decimal, requested: Decimal) -> None:
        self.user_id = user_id
        self.currency = currency
        self.balance = balance
        self.requested = requested
        super().__init__(
            f"Insufficient {currency} credit for user {user_id}: "
            f"balance={balance}, requested={requested}"
        )


class ConcurrentModificationError(Exception):
    """Raised when an optimistic lock conflict is detected.

    Caller should retry the full read-modify-write cycle. CreditService
    retries internally (_MAX_RETRIES times) before propagating this.
    """

    def __init__(self, balance_id: str) -> None:
        super().__init__(
            f"Concurrent modification detected on CreditBalance {balance_id}. "
            "Retry the operation."
        )


class DuplicateIdempotencyKey(Exception):
    """Raised when an idempotency_key is reused with conflicting parameters.

    The key was previously committed with different amount or transaction_type.
    This indicates a bug in the caller — the same key must always carry the
    same operation parameters.
    """

    def __init__(self, key: str, field: str, existing_value, new_value) -> None:
        super().__init__(
            f"Idempotency key '{key}' was previously committed with "
            f"{field}={existing_value!r}, but now called with {field}={new_value!r}."
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CreditService:
    # Internal retry knobs — tests may set _RETRY_BASE_DELAY_MS = 0.0 to skip sleeps.
    _MAX_RETRIES: int = 3
    _RETRY_BASE_DELAY_MS: float = 20.0

    def __init__(self, db: DBSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API — committing variants (own their transaction)
    # ------------------------------------------------------------------

    def balance(self, user_id: str, currency: str) -> Decimal:
        """Return current balance (Decimal). Returns Decimal("0") if no balance row exists."""
        row = self._get_balance_row(user_id, currency)
        return row.balance if row else Decimal("0")

    def credit(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str = "purchase",
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        """Add credits to a user's balance and commit.

        Idempotent: calling twice with the same idempotency_key returns the
        original CreditTransaction without modifying the balance again.
        Race-safe: if two concurrent calls share the same key, the one that
        loses the DB UNIQUE race rolls back, re-fetches the winner's result,
        and returns it — never double-crediting.

        Raises:
            DuplicateIdempotencyKey: same key reused with different amount or type.
            ConcurrentModificationError: optimistic lock conflict after _MAX_RETRIES.
        """
        return self._credit_impl(
            user_id=user_id,
            currency=currency,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type=transaction_type,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            _commit=True,
        )

    def credit_flush(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str = "purchase",
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        """Add credits and flush — caller owns the transaction.

        Executes identical business logic to credit() (validation, idempotency,
        optimistic locking) but calls db.flush() instead of db.commit().
        The caller MUST call db.commit() on success or db.rollback() on failure.

        Designed for BillingOrchestrator, which combines this call with a
        LedgerService write in a single atomic transaction.

        Raises:
            DuplicateIdempotencyKey: key reused with different params.
            ConcurrentModificationError: optimistic lock failed — propagated
              directly (no retry); caller should rollback and retry the whole op.
            IntegrityError: concurrent idempotency race — propagated; caller
              should rollback.  A retry will find the winning transaction.
        """
        return self._credit_impl(
            user_id=user_id,
            currency=currency,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type=transaction_type,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            _commit=False,
        )

    def debit(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str = "consume",
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        """Subtract credits from a user's balance and commit.

        Idempotent: calling twice with the same idempotency_key returns the
        original CreditTransaction without modifying the balance again.
        Race-safe: same guarantee as credit() against concurrent same-key calls.

        Raises:
            InsufficientCreditError: balance < amount. Balance is unchanged.
            DuplicateIdempotencyKey: same key reused with different amount or type.
            ConcurrentModificationError: optimistic lock conflict after _MAX_RETRIES.
        """
        return self._debit_impl(
            user_id=user_id,
            currency=currency,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type=transaction_type,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            _commit=True,
        )

    def debit_flush(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str = "consume",
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        """Subtract credits and flush — caller owns the transaction.

        Executes identical business logic to debit() but calls db.flush()
        instead of db.commit().  The caller MUST commit on success or rollback
        on failure.

        Raises:
            InsufficientCreditError: balance too low — nothing flushed.
            DuplicateIdempotencyKey: key reused with different params.
            ConcurrentModificationError: propagated; caller should rollback.
        """
        return self._debit_impl(
            user_id=user_id,
            currency=currency,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type=transaction_type,
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
            _commit=False,
        )

    def refund(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        """Return credits to a user (payment reversal, failed charge, etc.).

        Delegates to credit() with transaction_type='refund'.
        """
        return self.credit(
            user_id=user_id,
            currency=currency,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type="refund",
            reference_id=reference_id,
            reference_type=reference_type,
            note=note,
        )

    def get_transactions(
        self,
        user_id: str,
        currency: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CreditTransaction]:
        """Return paginated transaction history for a user."""
        q = (
            self._db.query(CreditTransaction)
            .filter(CreditTransaction.user_id == user_id)
        )
        if currency:
            q = q.filter(CreditTransaction.currency == currency)
        return (
            q.order_by(CreditTransaction.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Core implementations (shared by committing and flush variants)
    # ------------------------------------------------------------------

    def _credit_impl(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str,
        reference_id: str | None,
        reference_type: str | None,
        note: str | None,
        _commit: bool,
    ) -> CreditTransaction:
        self._validate_amount(amount)
        self._validate_tx_type(transaction_type)

        # In commit mode: retry up to _MAX_RETRIES for optimistic lock conflicts.
        # In flush mode: single attempt — conflicts propagate to the caller.
        max_attempts = self._MAX_RETRIES if _commit else 1

        for attempt in range(max_attempts):
            existing = self._find_existing_tx(idempotency_key)
            if existing:
                self._check_idempotency_params(existing, amount, transaction_type, idempotency_key)
                logger.debug("credit idempotent return | key=%s", idempotency_key)
                return existing

            try:
                balance_row = self._get_or_create_balance(user_id, currency)
                balance_before = balance_row.balance
                new_balance = balance_before + amount

                if _commit:
                    self._update_balance_optimistic(balance_row, new_balance)
                else:
                    self._update_balance_no_rollback(balance_row, new_balance)

                tx = CreditTransaction(
                    user_id=user_id,
                    credit_balance_id=balance_row.id,
                    transaction_type=transaction_type,
                    amount=amount,
                    currency=currency,
                    balance_before=balance_before,
                    balance_after=new_balance,
                    idempotency_key=idempotency_key,
                    reference_id=reference_id,
                    reference_type=reference_type,
                    note=note,
                )
                self._db.add(tx)

                if _commit:
                    self._db.commit()
                else:
                    self._db.flush()

                self._db.refresh(tx)

                logger.info(
                    "credit applied | user=%s currency=%s amount=%s balance=%s→%s key=%s commit=%s",
                    user_id, currency, amount, balance_before, new_balance, idempotency_key, _commit,
                )
                return tx

            except IntegrityError:
                if _commit:
                    # Concurrent call committed the same key first.
                    # Roll back, re-fetch the winner's transaction, return it.
                    self._db.rollback()
                    existing = self._find_existing_tx(idempotency_key)
                    if existing is not None:
                        self._check_idempotency_params(
                            existing, amount, transaction_type, idempotency_key
                        )
                        logger.debug(
                            "credit idempotent (race) return | key=%s attempt=%d",
                            idempotency_key, attempt,
                        )
                        return existing
                raise  # Different constraint OR flush mode — propagate to caller

            except ConcurrentModificationError:
                if _commit and attempt < max_attempts - 1:
                    self._sleep_backoff(attempt)
                    continue
                raise

        raise ConcurrentModificationError("unknown")  # pragma: no cover

    def _debit_impl(
        self,
        *,
        user_id: str,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str,
        reference_id: str | None,
        reference_type: str | None,
        note: str | None,
        _commit: bool,
    ) -> CreditTransaction:
        self._validate_amount(amount)
        self._validate_tx_type(transaction_type)

        max_attempts = self._MAX_RETRIES if _commit else 1

        for attempt in range(max_attempts):
            existing = self._find_existing_tx(idempotency_key)
            if existing:
                self._check_idempotency_params(existing, amount, transaction_type, idempotency_key)
                logger.debug("debit idempotent return | key=%s", idempotency_key)
                return existing

            try:
                balance_row = self._get_balance_row(user_id, currency)
                current = balance_row.balance if balance_row else Decimal("0")

                if current < amount:
                    raise InsufficientCreditError(user_id, currency, current, amount)

                new_balance = current - amount

                if _commit:
                    self._update_balance_optimistic(balance_row, new_balance)
                else:
                    self._update_balance_no_rollback(balance_row, new_balance)

                tx = CreditTransaction(
                    user_id=user_id,
                    credit_balance_id=balance_row.id,
                    transaction_type=transaction_type,
                    amount=amount,
                    currency=currency,
                    balance_before=current,
                    balance_after=new_balance,
                    idempotency_key=idempotency_key,
                    reference_id=reference_id,
                    reference_type=reference_type,
                    note=note,
                )
                self._db.add(tx)

                if _commit:
                    self._db.commit()
                else:
                    self._db.flush()

                self._db.refresh(tx)

                logger.info(
                    "debit applied | user=%s currency=%s amount=%s balance=%s→%s key=%s commit=%s",
                    user_id, currency, amount, current, new_balance, idempotency_key, _commit,
                )
                return tx

            except InsufficientCreditError:
                raise  # No retry — business logic constraint, not a race.

            except IntegrityError:
                if _commit:
                    self._db.rollback()
                    existing = self._find_existing_tx(idempotency_key)
                    if existing is not None:
                        self._check_idempotency_params(
                            existing, amount, transaction_type, idempotency_key
                        )
                        logger.debug(
                            "debit idempotent (race) return | key=%s attempt=%d",
                            idempotency_key, attempt,
                        )
                        return existing
                raise

            except ConcurrentModificationError:
                if _commit and attempt < max_attempts - 1:
                    self._sleep_backoff(attempt)
                    continue
                raise

        raise ConcurrentModificationError("unknown")  # pragma: no cover

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_balance_row(self, user_id: str, currency: str) -> CreditBalance | None:
        return (
            self._db.query(CreditBalance)
            .filter(
                CreditBalance.user_id == user_id,
                CreditBalance.currency == currency,
            )
            .first()
        )

    def _get_or_create_balance(self, user_id: str, currency: str) -> CreditBalance:
        row = self._get_balance_row(user_id, currency)
        if row is None:
            row = CreditBalance(
                user_id=user_id,
                currency=currency,
                balance=Decimal("0"),
                version=0,
            )
            self._db.add(row)
            self._db.flush()  # assign id without committing
        return row

    def _update_balance_optimistic(self, balance_row: CreditBalance, new_balance: Decimal) -> None:
        """Update balance using optimistic locking. Calls db.rollback() on conflict.

        Used by commit-mode credit() / debit() which own their transaction.
        Raises ConcurrentModificationError if the row was modified concurrently.
        """
        expected_version = balance_row.version
        now = _utcnow()

        result = self._db.execute(
            text(
                "UPDATE credit_balances "
                "SET balance = :bal, version = version + 1, updated_at = :now "
                "WHERE id = :id AND version = :expected"
            ),
            {
                "bal": str(new_balance),
                "now": now,
                "id": balance_row.id,
                "expected": expected_version,
            },
        )
        if result.rowcount == 0:
            self._db.rollback()
            raise ConcurrentModificationError(balance_row.id)

        balance_row.balance = new_balance
        balance_row.version = expected_version + 1
        balance_row.updated_at = now

    def _update_balance_no_rollback(self, balance_row: CreditBalance, new_balance: Decimal) -> None:
        """Update balance using optimistic locking. Does NOT call db.rollback() on conflict.

        Used by flush-mode credit_flush() / debit_flush() where the caller owns
        the outer transaction. On conflict the caller is responsible for rollback.
        """
        expected_version = balance_row.version
        now = _utcnow()

        result = self._db.execute(
            text(
                "UPDATE credit_balances "
                "SET balance = :bal, version = version + 1, updated_at = :now "
                "WHERE id = :id AND version = :expected"
            ),
            {
                "bal": str(new_balance),
                "now": now,
                "id": balance_row.id,
                "expected": expected_version,
            },
        )
        if result.rowcount == 0:
            # No rollback — the caller (orchestrator) must rollback the outer tx.
            raise ConcurrentModificationError(balance_row.id)

        balance_row.balance = new_balance
        balance_row.version = expected_version + 1
        balance_row.updated_at = now

    def _find_existing_tx(self, idempotency_key: str) -> CreditTransaction | None:
        return (
            self._db.query(CreditTransaction)
            .filter(CreditTransaction.idempotency_key == idempotency_key)
            .first()
        )

    def _check_idempotency_params(
        self,
        existing: CreditTransaction,
        amount: Decimal,
        transaction_type: str,
        idempotency_key: str,
    ) -> None:
        """Raise DuplicateIdempotencyKey if the existing tx has different core params."""
        if existing.amount != amount:
            raise DuplicateIdempotencyKey(
                idempotency_key, "amount", existing.amount, amount
            )
        if existing.transaction_type != transaction_type:
            raise DuplicateIdempotencyKey(
                idempotency_key, "transaction_type", existing.transaction_type, transaction_type
            )

    def _sleep_backoff(self, attempt: int) -> None:
        """Exponential back-off with jitter. Tests may set _RETRY_BASE_DELAY_MS = 0.0."""
        if self._RETRY_BASE_DELAY_MS <= 0:
            return
        ms = self._RETRY_BASE_DELAY_MS * (2 ** attempt) + random.randint(0, 10)
        time.sleep(ms / 1000)

    @staticmethod
    def _validate_amount(amount: Decimal) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError(f"amount must be Decimal, got {type(amount).__name__}")
        if amount <= Decimal("0"):
            raise ValueError(f"amount must be positive, got {amount}")

    @staticmethod
    def _validate_tx_type(tx_type: str) -> None:
        if tx_type not in CreditTransaction.TRANSACTION_TYPES:
            raise ValueError(
                f"Invalid transaction_type '{tx_type}'. "
                f"Must be one of: {sorted(CreditTransaction.TRANSACTION_TYPES)}"
            )
