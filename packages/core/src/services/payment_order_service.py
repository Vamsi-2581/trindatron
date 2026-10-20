"""
PaymentOrderService — PaymentOrder lifecycle management.

Responsibilities
----------------
* create_order()     — create a new PaymentOrder (idempotent via idempotency_key)
* get_order()        — fetch a single order with user ownership enforcement
* list_orders()      — list orders for a user, optionally filtered by status
* mark_processing()  — pending → processing (provider assigned an external_order_id)
* confirm_order()    — pending/processing → confirmed + credit balance atomically
* cancel_order()     — pending/processing → cancelled
* fail_order()       — pending/processing → failed
* expire_order()     — pending → expired (caller: expiry job; no user ownership check)
* refund_order()     — confirmed → refunded/partial_refund + debit balance atomically

Financial invariants preserved
-------------------------------
1. credits_amount is fixed at order creation and NEVER recalculated at confirmation.
2. Credits are applied on confirm ONLY — atomically with a LedgerEntry pair:
     credit_flush + record_double_entry_flush + order.status='confirmed' → ONE commit.
3. Refunds debit the credit balance atomically with a LedgerEntry pair:
     debit_flush + record_double_entry_flush + order status update → ONE commit.
4. On any exception inside a financial operation, db.rollback() is called —
   no partial state possible.
5. All monetary amounts are Decimal(28, 8) — no floats.

Atomicity design
----------------
BillingOrchestrator encapsulates (credit_flush + ledger_flush + commit) for two
entities.  PaymentOrderService extends this pattern to THREE entities by using
the flush variants directly and adding the order status update before the single
commit — achieving true atomicity across balance, ledger, and order state.

Idempotency keys
----------------
Confirmation:  f"po:{order.id}:confirm:credit"    /  f"po:{order.id}:confirm:ledger"
Refund:        f"por:{effective_key}:credit"       /  f"por:{effective_key}:ledger"

Refund keys use a caller-supplied (or auto-generated) effective_key rather than
the order id, enabling multiple independent refund records per order (Billing-2).
Retrying with the same effective_key returns the existing order without creating
a duplicate CreditTransaction/LedgerEntry pair.

Status lifecycle (Billing-2)
-----------------------------
  pending        → processing, confirmed, cancelled, failed, expired
  processing     → confirmed, cancelled, failed
  confirmed      → refunded, partial_refund
  partial_refund → refunded, partial_refund  (non-terminal; further refunds allowed)
  failed         → (terminal)
  cancelled      → (terminal)
  expired        → (terminal)
  refunded       → (terminal)

Plan-purchase orders (Commercial Checkout V1)
--------------------------------------------------
`PaymentOrder.plan` (nullable) opts an order into a DIFFERENT confirm_order()
branch: instead of credit_flush() (which would create a CreditBalance —
a spendable balance with no relationship to plan entitlements, see that
model's own docstring on why that would be a diverging "saldo paralelo"),
confirmation records revenue directly via
LedgerService.record_double_entry_flush(entry_type="plan_purchase") and
sets `order.user.plan = order.plan` — both in the SAME commit as the
order's own status transition, so a plan purchase's activation can never
succeed without the revenue being recorded, or vice versa. A plan-purchase
order can never be refunded through refund_order() — see that method's
guard and PlanPurchaseNotRefundable's own docstring.

Expiration (Commercial Checkout V1)
---------------------------------------
`expire_overdue_orders()` bulk-sweeps every `status='pending'` order whose
`expires_at` has passed, calling the existing single-row `expire_order()`
for each — no new expiry logic, just batching the existing operation.
`run_payment_order_expiry_loop()` is the background task that calls it
periodically, started from main.py's lifespan alongside the other
poll loops (agent schedule, execution worker/recovery) — same
"in-process asyncio task, started/stopped via a stop_event" pattern,
not a new scheduling mechanism.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.billing.providers.base import PaymentIntent, PaymentProvider, PaymentStatus
from src.billing.team_pricing import TEAM_SEATS_PLAN
from src.models.payment_order import PaymentOrder
from src.models.payment_order_refund import PaymentOrderRefund
from src.services.billing_orchestrator import RefundAmountExceedsEligible
from src.services.credit_service import CreditService
from src.services.ledger_service import LedgerService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrderNotFound(Exception):
    """Raised when the requested PaymentOrder does not exist for this user."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"PaymentOrder {order_id!r} not found")


class InvalidStatusTransition(Exception):
    """Raised when an operation requires a status the order does not have."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Cannot transition PaymentOrder from {from_status!r} to {to_status!r}"
        )


class OrderExpired(Exception):
    """Raised when confirm_order is called on an order whose expires_at has passed."""

    def __init__(self, order_id: str, expires_at: datetime) -> None:
        self.order_id = order_id
        self.expires_at = expires_at
        super().__init__(
            f"PaymentOrder {order_id!r} expired at {expires_at.isoformat()}"
        )


class PlanPurchaseNotRefundable(Exception):
    """Raised when refund_order() is called on an order with `plan` set.

    A plan-purchase order never credited CreditBalance (see
    PaymentOrder.plan's docstring) — refund_order()'s debit_flush() would
    have nothing correct to debit. Reversing a plan ACTIVATION also
    requires deciding what plan the user reverts to, a product decision
    explicitly out of scope for this phase (see docs/CHECKOUT.md). Rather
    than expose an incomplete/incorrect refund path, it is refused
    outright — "não expor refund comercial incompleto" per this phase's
    own instruction.
    """

    def __init__(self, order_id: str, plan: str) -> None:
        self.order_id = order_id
        self.plan = plan
        super().__init__(
            f"PaymentOrder {order_id!r} is a plan purchase ({plan!r}) and cannot be refunded via this endpoint"
        )


class ProviderReassignmentUnsafe(Exception):
    """Payment Router etapa — raised by reassign_provider_unsafe_guard()
    (called internally by the router before mutating order.provider) when
    the order ALREADY has an external_order_id set.

    Reassigning the provider at that point could orphan a real external
    commitment (a session/charge that may already exist at the
    CURRENTLY-assigned provider) under a different provider's identity —
    exactly the "cobrança dupla" risk this phase's own failover rules
    exist to prevent. Only ever safe to reassign a provider BEFORE any
    external contact has succeeded.
    """

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(
            f"PaymentOrder {order_id!r} already has an external_order_id — "
            "reassigning its provider now could orphan a real external commitment"
        )


# ---------------------------------------------------------------------------
# Valid status transitions
# ---------------------------------------------------------------------------

_VALID_FROM: dict[str, frozenset[str]] = {
    PaymentStatus.PENDING.value: frozenset({
        PaymentStatus.PROCESSING.value,
        PaymentStatus.CONFIRMED.value,
        PaymentStatus.CANCELLED.value,
        PaymentStatus.FAILED.value,
        PaymentStatus.EXPIRED.value,
    }),
    PaymentStatus.PROCESSING.value: frozenset({
        PaymentStatus.CONFIRMED.value,
        PaymentStatus.CANCELLED.value,
        PaymentStatus.FAILED.value,
    }),
    PaymentStatus.CONFIRMED.value: frozenset({
        PaymentStatus.REFUNDED.value,
        PaymentStatus.PARTIAL_REFUND.value,
    }),
    # Non-terminal in Billing-2: partial_refund → another partial_refund or full refunded
    PaymentStatus.PARTIAL_REFUND.value: frozenset({
        PaymentStatus.REFUNDED.value,
        PaymentStatus.PARTIAL_REFUND.value,
    }),
    PaymentStatus.FAILED.value: frozenset(),
    PaymentStatus.CANCELLED.value: frozenset(),
    PaymentStatus.EXPIRED.value: frozenset(),
    PaymentStatus.REFUNDED.value: frozenset(),
}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaymentOrderService:
    """Manages the complete lifecycle of PaymentOrder records.

    Each financial operation (confirm, refund) is atomic: the balance update,
    ledger pair, and order status change are all flushed and committed together
    in a single transaction.  On any exception, db.rollback() restores the
    previous state with no partial writes.
    """

    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._credit_svc = CreditService(db)
        self._ledger_svc = LedgerService(db)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_order(
        self,
        *,
        user_id: str,
        provider: str,
        amount_fiat: Decimal,
        currency_fiat: str,
        credits_amount: Decimal,
        currency_credits: str,
        idempotency_key: str,
        expires_at: datetime,
        plan: str | None = None,
        payment_method: str | None = None,
        payment_type: str | None = None,
        subscription_id: str | None = None,
    ) -> PaymentOrder:
        """Create a new PaymentOrder in the pending state.

        Idempotent: supplying the same idempotency_key returns the existing
        order without creating a new one.

        Args:
            user_id:          Owner of this order.
            provider:         Payment provider identifier (e.g. "binance_pay").
            amount_fiat:      Gross amount the user pays at the provider.
            currency_fiat:    Currency of amount_fiat (e.g. "USD", "BTC").
            credits_amount:   Credits to apply on confirmation. Fixed — never
                              recalculated after creation.
            currency_credits: Currency of credits_amount (e.g. "USD", "TCU").
            idempotency_key:  Caller-provided unique key. Must be ≤ 128 chars.
            expires_at:       UTC datetime after which the order cannot be confirmed.
            plan:             Commercial Checkout V1 — the plan this order
                              activates on confirmation (e.g. "start"), or
                              None for a plain credit-purchase order (the
                              default — every pre-existing caller is
                              unaffected). Only POST /billing/checkout
                              (never the generic POST /billing/orders)
                              ever passes this.
            payment_method:   Payments Foundation — a PaymentMethod value
                              (e.g. "pix", "crypto"), or None (default —
                              every pre-existing caller is unaffected).
            payment_type:     Payments Foundation — a PaymentType value
                              ("one_time" / "recurring"), or None.
            subscription_id:  Subscription Engine — links this order to
                              the recurring agreement it's a charge for,
                              or None (default — every pre-existing
                              caller, and every one-time/crypto purchase
                              going forward, is unaffected). See
                              Subscription's own module docstring for
                              why PaymentOrder has zero REQUIRED
                              dependency on this.

        Returns:
            New or existing PaymentOrder in 'pending' status.
        """
        existing = self._find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        order = PaymentOrder(
            user_id=user_id,
            provider=provider,
            amount_fiat=amount_fiat,
            currency_fiat=currency_fiat,
            credits_amount=credits_amount,
            currency_credits=currency_credits,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            plan=plan,
            payment_method=payment_method,
            payment_type=payment_type,
            subscription_id=subscription_id,
        )
        self._db.add(order)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
            raise
        self._db.refresh(order)
        return order

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_order(self, order_id: str, user_id: str) -> PaymentOrder | None:
        """Return the order if it belongs to user_id, else None."""
        return (
            self._db.query(PaymentOrder)
            .filter(PaymentOrder.id == order_id, PaymentOrder.user_id == user_id)
            .first()
        )

    def list_orders(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaymentOrder]:
        """Return orders for user_id, newest first.

        Args:
            status: Optional filter (e.g. "pending"). None = all statuses.
            limit:  Max rows returned.
            offset: Pagination offset.
        """
        q = self._db.query(PaymentOrder).filter(PaymentOrder.user_id == user_id)
        if status is not None:
            q = q.filter(PaymentOrder.status == status)
        return (
            q.order_by(PaymentOrder.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Lifecycle transitions (non-financial)
    # ------------------------------------------------------------------

    def mark_processing(
        self,
        order_id: str,
        user_id: str,
        *,
        external_order_id: str | None = None,
    ) -> PaymentOrder:
        """Transition pending → processing.

        Called when the payment provider has started processing the payment
        and optionally has assigned its own order identifier.

        Args:
            external_order_id: Provider's own order/transaction ID. Stored on
                               the order so webhook handlers can look it up.
        """
        order = self._get_or_raise(order_id, user_id)
        self._assert_valid_transition(order.status, PaymentStatus.PROCESSING.value)

        order.status = PaymentStatus.PROCESSING.value
        if external_order_id is not None:
            order.external_order_id = external_order_id
        self._db.commit()
        self._db.refresh(order)
        return order

    def lock_order_for_provider_attempt(self, order_id: str) -> PaymentOrder:
        """Payment Router etapa — acquire (and HOLD, until the caller's
        next commit/rollback on this same session) a row-level lock on
        this PaymentOrder, via SELECT ... FOR UPDATE.

        PostgreSQL genuinely serializes concurrent callers here — a
        second caller's own lock_order_for_provider_attempt() for the
        SAME order_id blocks until the first caller commits or rolls
        back. SQLite silently ignores the lock (dev/test only) — same
        documented limitation as refund_order()'s own SELECT FOR UPDATE
        use; see test_billing_postgres_concurrency.py for real-DB-backed
        concurrency coverage.

        Callers (create_provider_order(), the Payment Router) MUST
        acquire this lock BEFORE inspecting external_order_id or calling
        adapter.create_intent() — this is what guarantees two concurrent
        attempts for the SAME order can never both reach the provider:
        the loser blocks here until the winner commits (having set
        external_order_id), then re-reads a row that already has it set
        and skips the external call entirely.

        No user_id filter — this is an internal orchestration primitive,
        never exposed directly to a user-facing route (same "no
        ownership check" precedent as expire_order(), called only by
        code that already knows/created this order_id in the same call).
        """
        order = (
            self._db.query(PaymentOrder)
            .filter(PaymentOrder.id == order_id)
            .with_for_update()
            .first()
        )
        if order is None:
            raise OrderNotFound(order_id)
        return order

    def assert_order_still_pending(self, order: PaymentOrder) -> None:
        """Hardening (Payment Router etapa follow-up) — raises if `order`
        is no longer 'pending' when a caller (create_provider_order(),
        the Payment Router) is about to call adapter.create_intent().

        Must be called on an order already locked via
        lock_order_for_provider_attempt() in the SAME transaction — see
        that method's own docstring and expire_order()'s updated
        docstring for the full race this closes: without this check, a
        concurrent expire_order() call that won the lock race FIRST
        (committing 'expired' before this caller even acquired the
        lock) would otherwise go undetected, letting create_intent() be
        called anyway — a REAL external session/charge could then
        succeed for an order already dead, with no valid transition
        left to record it (mark_processing() would raise
        InvalidStatusTransition('expired' -> 'processing') AFTER the
        external commitment already exists).

        Raises:
            OrderExpired:            order.status == 'expired'.
            InvalidStatusTransition: any other non-'pending' status
                                      (e.g. 'cancelled'/'failed' via a
                                      directly-called cancel/fail racing
                                      the same order).
        """
        if order.status == PaymentStatus.PENDING.value:
            return
        if order.status == PaymentStatus.EXPIRED.value:
            raise OrderExpired(order.id, order.expires_at)
        raise InvalidStatusTransition(order.status, PaymentStatus.PROCESSING.value)

    def reassign_provider_unsafe_guard(self, order: PaymentOrder) -> None:
        """Payment Router etapa — raises ProviderReassignmentUnsafe if
        `order` already has an external_order_id. Called by the router
        immediately before mutating `order.provider` mid-failover, on an
        order already locked via lock_order_for_provider_attempt() in
        the SAME transaction — see that exception's own docstring for
        why this check exists. Pure guard, no DB write of its own; the
        caller performs the actual mutation + commit."""
        if order.external_order_id is not None:
            raise ProviderReassignmentUnsafe(order.id)

    def cancel_order(self, order_id: str, user_id: str) -> PaymentOrder:
        """Transition pending/processing → cancelled.

        No financial operation — no credits were applied.
        """
        order = self._get_or_raise(order_id, user_id)
        self._assert_valid_transition(order.status, PaymentStatus.CANCELLED.value)

        order.status = PaymentStatus.CANCELLED.value
        self._db.commit()
        self._db.refresh(order)
        return order

    def fail_order(
        self,
        order_id: str,
        user_id: str,
        *,
        webhook_raw: dict | None = None,
    ) -> PaymentOrder:
        """Transition pending/processing → failed.

        No financial operation.  webhook_raw stores the provider payload for audit.
        MUST NOT contain signing keys or credentials — only normalized event data.
        """
        order = self._get_or_raise(order_id, user_id)
        self._assert_valid_transition(order.status, PaymentStatus.FAILED.value)

        order.status = PaymentStatus.FAILED.value
        if webhook_raw is not None:
            order.webhook_raw = json.dumps(webhook_raw)
        self._db.commit()
        self._db.refresh(order)
        return order

    def expire_order(self, order_id: str) -> PaymentOrder:
        """Transition pending → expired.

        Called by the expiry background job — no user ownership check.
        No financial operation.

        Hardening (Payment Router etapa follow-up): re-fetches the order
        under the SAME row lock as lock_order_for_provider_attempt() —
        see that method's own docstring for the full concurrency
        rationale. Without this lock, this method could race a
        concurrent create_provider_order()/route_checkout() call that
        already acquired the lock and is mid-flight calling
        adapter.create_intent(): a REAL external session/charge could
        succeed there while THIS method, using an unlocked plain SELECT,
        still reads status='pending' and commits 'expired' first — the
        later mark_processing() call would then hit
        InvalidStatusTransition('expired' -> 'processing') AFTER the
        external commitment already exists, leaving a real payment with
        no local order to ever confirm it against (external_order_id
        never gets persisted). The lock makes the two operations
        mutually exclusive: whichever acquires it first determines the
        outcome, and the loser's transition is safely rejected by
        _assert_valid_transition() below — see
        create_provider_order()/route_checkout()'s own matching
        post-lock status re-check for the other half of this fix.
        """
        order = self.lock_order_for_provider_attempt(order_id)

        self._assert_valid_transition(order.status, PaymentStatus.EXPIRED.value)
        order.status = PaymentStatus.EXPIRED.value
        self._db.commit()
        self._db.refresh(order)
        return order

    # ------------------------------------------------------------------
    # Financial operations — confirm and refund
    # ------------------------------------------------------------------

    def confirm_order(
        self,
        order_id: str,
        user_id: str,
        *,
        webhook_raw: dict | None = None,
    ) -> PaymentOrder:
        """Confirm a pending/processing order and atomically activate its
        effect — either a credit-balance top-up (order.plan is None, the
        original behavior) or a plan activation (order.plan is set,
        Commercial Checkout V1).

        Atomicity guarantee
        -------------------
        Credit-purchase order (order.plan is None) — all five writes
        commit together or not at all:
          1. CreditBalance updated (credit_flush)
          2. CreditTransaction inserted (credit_flush)
          3. LedgerEntry debit inserted (record_double_entry_flush)
          4. LedgerEntry credit inserted (record_double_entry_flush)
          5. PaymentOrder.status = 'confirmed', confirmed_at = now
          → single db.commit()
          ← db.rollback() on any exception

        Plan-purchase order (order.plan is set) — four writes commit
        together or not at all:
          1. LedgerEntry debit inserted (record_double_entry_flush)
          2. LedgerEntry credit inserted (record_double_entry_flush)
          3. user.plan = order.plan
          4. PaymentOrder.status = 'confirmed', confirmed_at = now
          → single db.commit()
          ← db.rollback() on any exception
        No CreditBalance/CreditTransaction row is ever created for this
        branch — see PaymentOrder.plan's own docstring for why.

        Idempotency
        -----------
        If the order is already confirmed, the existing order is returned
        immediately with no DB writes.

        Concurrent confirmation (e.g. a webhook and a polling job racing)
        --------------------------------------------------------------------
        Two callers can both pass the fast-path/transition checks above
        before either commits. The loser's credit_flush()/record_double_entry_flush()
        then hits a DB-level UNIQUE violation (IntegrityError) on this order's
        idempotency keys, because the winner already committed first. That is
        caught, rolled back, and the order re-fetched — if it now reads
        'confirmed' (the winner's result), it is returned normally, exactly
        like the top-of-method fast path. This never raises for a genuine
        race; it only re-raises if the IntegrityError turns out not to be
        that race (order still not confirmed after refresh).

        Expiry
        ------
        If expires_at is in the past, the order is transitioned to 'expired'
        and OrderExpired is raised. No credits are applied.

        Args:
            webhook_raw: Optional provider webhook payload (no secrets allowed).
        """
        order = self._get_or_raise(order_id, user_id)

        # Fast path: already confirmed — idempotent return
        if order.status == PaymentStatus.CONFIRMED.value:
            return order

        self._assert_valid_transition(order.status, PaymentStatus.CONFIRMED.value)

        now = datetime.now(timezone.utc)
        expires_at = order.expires_at
        # SQLite returns naive datetimes (no tzinfo); PostgreSQL returns aware ones.
        # Normalise to UTC-aware before comparison so the service is DB-agnostic.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            order.status = PaymentStatus.EXPIRED.value
            self._db.commit()
            raise OrderExpired(order_id, order.expires_at)

        try:
            if order.plan is not None:
                # Commercial Checkout V1 — plan-purchase order. Deliberately
                # NO credit_flush(): crediting a spendable CreditBalance for
                # a plan purchase would be a "saldo paralelo" with no
                # relationship to entitlements — see PaymentOrder.plan's own
                # docstring. Revenue is still recorded (real money was
                # received), just via the ledger directly.
                self._ledger_svc.record_double_entry_flush(
                    entry_type="plan_purchase",
                    account_debit=f"user:{order.user_id}",
                    account_credit="revenue",
                    amount=order.amount_fiat,
                    currency=order.currency_fiat,
                    reference_id=order.id,
                    reference_type="payment_order",
                    idempotency_key=f"po:{order.id}:confirm:ledger",
                )
                # The ACTUAL activation — GET /users/me/capacity,
                # /users/me/entitlement, and every adaptive-budget check
                # already read user.plan live (never cached), so this
                # single write is immediately effective for the user's
                # very next request. No second "recalculate capacity" step
                # exists or is needed.
                if order.plan == TEAM_SEATS_PLAN:
                    # A seat purchase activates the TEAM's seats (and, through
                    # them, each seat holder's plan) — never the buyer's own
                    # plan. Same transaction: a failure rolls everything back.
                    from src.services.team_service import activate_from_order

                    activate_from_order(self._db, order, now=now)
                else:
                    order.user.plan = order.plan
            else:
                self._credit_svc.credit_flush(
                    user_id=order.user_id,
                    currency=order.currency_credits,
                    amount=order.credits_amount,
                    idempotency_key=f"po:{order.id}:confirm:credit",
                    transaction_type="purchase",
                    reference_id=order.id,
                    reference_type="payment_order",
                )
                self._ledger_svc.record_double_entry_flush(
                    entry_type="credit_purchase",
                    account_debit=f"user:{order.user_id}",
                    account_credit="revenue",
                    amount=order.credits_amount,
                    currency=order.currency_credits,
                    reference_id=order.id,
                    reference_type="payment_order",
                    idempotency_key=f"po:{order.id}:confirm:ledger",
                )
            order.status = PaymentStatus.CONFIRMED.value
            order.confirmed_at = now
            if webhook_raw is not None:
                order.webhook_raw = json.dumps(webhook_raw)
            self._db.commit()
            self._db.refresh(order)
        except IntegrityError:
            # A concurrent confirm_order() call for this SAME order (e.g. the
            # webhook route and a future polling job racing) already won and
            # committed first — its credit_flush()/record_double_entry_flush()
            # share this order's idempotency keys (f"po:{order.id}:confirm:*"),
            # so our own flush hits the DB's UNIQUE constraint, not a logic
            # bug. CreditService.credit_flush's own docstring documents the
            # expected recovery: "A retry will find the winning transaction."
            # Rolling back and refreshing does exactly that — if the winner's
            # commit is what we collided with, `order` now reads CONFIRMED
            # and this is a normal idempotent return, not a failure.
            self._db.rollback()
            self._db.refresh(order)
            if order.status == PaymentStatus.CONFIRMED.value:
                logger.info(
                    "payment_order confirmed by a concurrent caller | order=%s user=%s "
                    "(this call observed the race via IntegrityError and returned "
                    "the winner's result instead of failing)",
                    order.id, order.user_id,
                )
                return order
            # The order was NOT confirmed by the refresh — the IntegrityError
            # was not the expected idempotency race (e.g. a different, unknown
            # constraint violation). Do not claim success for a state that
            # isn't actually confirmed; surface the original failure.
            raise
        except Exception:
            self._db.rollback()
            raise

        if order.plan is not None:
            logger.info(
                "payment_order confirmed | order=%s user=%s plan=%s amount=%s %s",
                order.id, order.user_id, order.plan, order.amount_fiat, order.currency_fiat,
            )
        else:
            logger.info(
                "payment_order confirmed | order=%s user=%s credits=%s %s",
                order.id, order.user_id, order.credits_amount, order.currency_credits,
            )
        return order

    def refund_order(
        self,
        order_id: str,
        user_id: str,
        amount: Decimal,
        *,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentOrder:
        """Refund a confirmed/partially-refunded order atomically (Billing-2).

        Atomicity guarantee
        -------------------
        All six writes commit together or not at all:
          1. PaymentOrderRefund row inserted
          2. CreditBalance updated (debit_flush)
          3. CreditTransaction(type='refund') inserted (debit_flush)
          4. LedgerEntry debit inserted (record_double_entry_flush)
          5. LedgerEntry credit inserted (record_double_entry_flush)
          6. PaymentOrder.status = 'refunded' or 'partial_refund'
          → single db.commit()
          ← db.rollback() on any exception

        Multiple partial refunds (Billing-2)
        -------------------------------------
        The ceiling is the cumulative sum of all prior PaymentOrderRefund rows.
        SELECT FOR UPDATE on PaymentOrder prevents concurrent calls from exceeding
        the ceiling (PostgreSQL only; SQLite silently ignores the lock in dev/test).

        Idempotency
        -----------
        If idempotency_key is provided and was already used for this order, the
        existing order is returned with no additional DB writes.
        If idempotency_key is None, a fresh UUID is generated — no protection.

        Args:
            amount:          Positive Decimal — credits to remove. Must be ≤ available.
            note:            Optional audit note stored on the CreditTransaction.
            idempotency_key: Optional caller-provided unique key (max 128 chars).

        Raises:
            OrderNotFound:              order not found for this user.
            InvalidStatusTransition:    order is not in confirmed or partial_refund state.
            RefundAmountExceedsEligible: amount > available credits.
            InsufficientCreditError:    user's credit balance is lower than amount.
        """
        # 1. Row-level lock — prevents concurrent refunds from exceeding ceiling.
        #    SQLite ignores FOR UPDATE silently; PostgreSQL serialises here.
        order = (
            self._db.query(PaymentOrder)
            .filter(PaymentOrder.id == order_id, PaymentOrder.user_id == user_id)
            .with_for_update()
            .first()
        )
        if order is None:
            raise OrderNotFound(order_id)

        if order.plan is not None:
            # Commercial Checkout V1 — see PlanPurchaseNotRefundable's own
            # docstring. Checked BEFORE the idempotency lookup below: a
            # plan-purchase order must never be refundable regardless of
            # what idempotency_key is supplied.
            raise PlanPurchaseNotRefundable(order.id, order.plan)

        # 2. Effective idempotency key — auto-generate if not provided.
        effective_key = idempotency_key or str(uuid.uuid4())

        # 3. Idempotency check — return without writes if key already processed for this order.
        existing_refund = (
            self._db.query(PaymentOrderRefund)
            .filter(
                PaymentOrderRefund.idempotency_key == effective_key,
                PaymentOrderRefund.order_id == order_id,
            )
            .first()
        )
        if existing_refund is not None:
            self._db.refresh(order)
            return order

        # 4. Status check — only confirmed and partial_refund are refundable.
        if order.status not in (
            PaymentStatus.CONFIRMED.value,
            PaymentStatus.PARTIAL_REFUND.value,
        ):
            raise InvalidStatusTransition(order.status, PaymentStatus.REFUNDED.value)

        # 5. Cumulative ceiling — sum of all prior refunds for this order.
        raw_sum = (
            self._db.query(func.sum(PaymentOrderRefund.amount))
            .filter(PaymentOrderRefund.order_id == order_id)
            .scalar()
        )
        already_refunded = Decimal(str(raw_sum)) if raw_sum is not None else Decimal("0")

        # 6. Guard: amount must not push cumulative refunds past credits_amount.
        available = order.credits_amount - already_refunded
        if amount > available:
            raise RefundAmountExceedsEligible(amount, available)

        # 7. Determine new order status.
        is_full_refund = (already_refunded + amount) == order.credits_amount
        new_status = (
            PaymentStatus.REFUNDED.value if is_full_refund
            else PaymentStatus.PARTIAL_REFUND.value
        )

        # 8. Atomic block — all 6 writes or none.
        try:
            refund_record = PaymentOrderRefund(
                order_id=order_id,
                user_id=order.user_id,
                amount=amount,
                currency=order.currency_credits,
                idempotency_key=effective_key,
                note=note,
            )
            self._db.add(refund_record)
            self._db.flush()  # assigns id, enforces UNIQUE; no commit yet

            self._credit_svc.debit_flush(
                user_id=order.user_id,
                currency=order.currency_credits,
                amount=amount,
                idempotency_key=f"por:{effective_key}:credit",
                transaction_type="refund",
                reference_id=order.id,
                reference_type="payment_order",
                note=note,
            )
            self._ledger_svc.record_double_entry_flush(
                entry_type="refund",
                account_debit=f"user:{order.user_id}",
                account_credit="revenue",
                amount=amount,
                currency=order.currency_credits,
                reference_id=order.id,
                reference_type="payment_order",
                note=note,
                idempotency_key=f"por:{effective_key}:ledger",
            )
            order.status = new_status
            self._db.commit()
            self._db.refresh(order)
        except Exception:
            self._db.rollback()
            raise

        logger.info(
            "payment_order refunded | order=%s user=%s amount=%s %s status=%s key=%s",
            order.id, order.user_id, amount, order.currency_credits, new_status, effective_key,
        )
        return order

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_raise(self, order_id: str, user_id: str) -> PaymentOrder:
        order = self.get_order(order_id, user_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order

    def _find_by_idempotency_key(self, key: str) -> PaymentOrder | None:
        return (
            self._db.query(PaymentOrder)
            .filter(PaymentOrder.idempotency_key == key)
            .first()
        )

    def _assert_valid_transition(self, from_status: str, to_status: str) -> None:
        allowed = _VALID_FROM.get(from_status, frozenset())
        if to_status not in allowed:
            raise InvalidStatusTransition(from_status, to_status)


# ---------------------------------------------------------------------------
# Provider-order orchestration
# ---------------------------------------------------------------------------


async def create_provider_order(
    db: DBSession,
    adapter: PaymentProvider,
    *,
    user_id: str,
    amount_fiat: Decimal,
    currency_fiat: str,
    credits_amount: Decimal,
    currency_credits: str,
    idempotency_key: str,
    expires_at: datetime,
    plan: str | None = None,
    payment_method: str | None = None,
    payment_type: str | None = None,
) -> tuple[PaymentOrder, PaymentIntent | None]:
    """Create a local PaymentOrder AND a matching order at a real provider
    (e.g. Binance Pay), wired together via external_order_id.

    Generic over any PaymentProvider adapter — not Binance-specific — so a
    future Stripe/PIX/on-chain adapter reuses this unchanged. Called by
    BOTH POST /billing/orders (generic credit purchase, plan=None) and
    POST /billing/checkout (Commercial Checkout V1, plan set) — the exact
    same orchestration either way, only the `plan` value differs.

    Order of operations matters: the local PaymentOrder is created FIRST
    (cheap, no external call, and PaymentOrderService.create_order() is
    already idempotent on idempotency_key) — so a retry after the
    provider call below fails or times out never creates a second local
    row; it just re-attempts create_intent() for the same order.

    Idempotent: if external_order_id is already set (a prior call already
    reached the provider successfully), the existing order is returned
    without calling the provider again — the SECOND tuple element is then
    None (the original PaymentIntent, which may carry a one-time-only
    redirect_url/client_secret for hosted checkout, is not persisted
    anywhere and cannot be reconstructed from a re-fetch — a provider's
    "query order" endpoint typically returns STATUS, not a fresh checkout
    link). Callers needing the checkout link (e.g. POST /billing/checkout)
    must use it on the FIRST, non-idempotent call; a caller retrying
    after that point is expected to already have shown it to the user, or
    the frontend re-requests it via a route reading order.external_order_id
    and calling adapter.get_intent() directly if a provider's own
    semantics support that (Binance Pay's does not — documented gap, see
    docs/CHECKOUT.md).

    Concurrency (Payment Router etapa): the order is re-fetched under a
    row lock (lock_order_for_provider_attempt()) BEFORE the
    external_order_id check / create_intent() call — two concurrent
    calls for the SAME idempotency_key can therefore never both reach
    the provider (PostgreSQL; SQLite ignores the lock in dev/test, same
    documented limitation as refund_order()'s own use of SELECT FOR
    UPDATE).

    Hardening: also re-asserts the order is still 'pending' under that
    SAME lock, via assert_order_still_pending() — closes the race
    against a concurrent expire_order() call (see that method's own
    updated docstring). Raises OrderExpired/InvalidStatusTransition
    instead of ever calling create_intent() for an order a concurrent
    caller already moved past 'pending'.
    """
    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=user_id,
        provider=adapter.provider_name,
        amount_fiat=amount_fiat,
        currency_fiat=currency_fiat,
        credits_amount=credits_amount,
        currency_credits=currency_credits,
        idempotency_key=idempotency_key,
        expires_at=expires_at,
        plan=plan,
        payment_method=payment_method,
        payment_type=payment_type,
    )
    order = svc.lock_order_for_provider_attempt(order.id)
    if order.external_order_id:
        db.commit()  # release the lock — nothing more to do, already succeeded
        return order, None
    try:
        svc.assert_order_still_pending(order)
    except Exception:
        db.commit()  # release the lock — no mutation happened, nothing to roll back
        raise

    # payment_method is forwarded through the ABC's existing, previously-
    # unused `metadata` parameter — NOT a new create_intent() argument
    # (the ABC's signature itself is unchanged; see base.py's own
    # docstring). Adapters that don't inspect this key (NOWPayments'
    # metadata reads only "pay_currency"/"goods_name", Binance Pay reads
    # none at all) are completely unaffected. A Fiat adapter (Mercado
    # Pago) uses it to bias which methods its own hosted checkout page
    # actually shows, when the caller asked for a specific one.
    intent_metadata = {"payment_method": payment_method} if payment_method else None
    intent = await adapter.create_intent(
        amount=amount_fiat,
        currency=currency_fiat,
        user_id=user_id,
        idempotency_key=idempotency_key,
        metadata=intent_metadata,
    )
    order = svc.mark_processing(order.id, user_id, external_order_id=intent.id)
    return order, intent


# ---------------------------------------------------------------------------
# Expiration (Commercial Checkout V1) — see module docstring
# ---------------------------------------------------------------------------


def expire_overdue_orders(db: DBSession, *, now: datetime | None = None) -> int:
    """Bulk-sweep every 'pending' order whose expires_at has passed, transitioning
    each to 'expired' via the EXISTING single-row PaymentOrderService.expire_order()
    — no new expiry logic, just batching it. Returns the count transitioned.

    Only 'pending' orders are swept — a 'processing' order (a provider has
    already been contacted, e.g. Binance Pay's create_intent() succeeded)
    is left alone here; that order still expires the ordinary way the
    NEXT time anyone calls confirm_order() on it (the existing
    expires_at-in-the-past check inside confirm_order() itself), matching
    the pre-existing behavior — this sweep only closes the gap for orders
    that were never even reached by the provider (or the local-only path
    with no provider at all)."""
    now = now or datetime.now(timezone.utc)
    svc = PaymentOrderService(db)
    overdue = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.status == PaymentStatus.PENDING.value, PaymentOrder.expires_at < now)
        .all()
    )
    count = 0
    for order in overdue:
        try:
            svc.expire_order(order.id)
            count += 1
        except (OrderNotFound, InvalidStatusTransition):
            # Another caller (e.g. a concurrent confirm_order() call that
            # itself found the order overdue) already transitioned it —
            # idempotent, not an error.
            pass
    if count:
        logger.info("payment_order: expired %d overdue pending order(s)", count)
    return count


async def run_payment_order_expiry_loop(*, stop_event) -> None:
    """Background task started from main.py's lifespan, stopped via
    `stop_event` on shutdown — same in-process asyncio task pattern as
    agent_schedule_service.run_schedule_poll_loop() and
    execution_worker.run_recovery_loop() (see those modules' own
    docstrings for the shared rationale); not a new scheduling mechanism.
    Waits one interval before the first sweep, same "don't pay a DB
    round-trip on every test's app startup" rationale those two already
    established."""
    import asyncio

    from src.config import settings
    from src.database import SessionLocal

    logger.info(
        "payment_order: expiry loop started | interval=%.0fs",
        settings.payment_order_expiry_poll_interval_seconds,
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.payment_order_expiry_poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        db = SessionLocal()
        try:
            expire_overdue_orders(db)
        except Exception:
            logger.exception("payment_order: expiry sweep failed")
        finally:
            db.close()
    logger.info("payment_order: expiry loop stopped")
