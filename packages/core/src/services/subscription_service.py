"""
SubscriptionService — recurring billing lifecycle orchestration
(Subscription Engine etapa, 2026-09-08).

Architectural audit this etapa was built on (summarized here so the
design is traceable to real code, not assumption):

  Plan               — src/billing/pricing.py's PLAN_PRICES catalog is
                       the single source of truth for what a plan
                       costs; Subscription.plan/amount/currency are
                       validated against it at creation time, same
                       convention PaymentOrder.plan already uses.
  PaymentOrder        — already subscription-aware (Payments Foundation
                       etapa): payment_method/payment_type/
                       subscription_id columns, subscription_id nullable
                       with NO required dependency either direction (see
                       PaymentOrder's own module docstring).
  Entitlement         — src/entitlements.py's PLAN_ENTITLEMENTS +
                       user.plan are read live by every capacity check;
                       PaymentOrderService.confirm_order()'s existing
                       plan-purchase branch already sets user.plan
                       atomically with the ledger write. This module
                       NEVER touches user.plan directly — it reuses
                       confirm_order() unchanged for that effect on a
                       successful renewal, and deliberately does NOT
                       auto-downgrade/change user.plan on cancel/expire
                       (see "Downgrade policy" below).
  Ledger              — LedgerEntry's entry_type="plan_purchase" path
                       (confirm_order()) is reused as-is; no new ledger
                       entry_type or write path is introduced. A
                       renewal charge IS, structurally, just another
                       plan-purchase PaymentOrder — the only difference
                       is that it's linked to a Subscription via
                       subscription_id and carries payment_type=
                       "recurring".
  Users               — no schema change; User.subscriptions
                       relationship already exists (Payments Foundation).
  Provider abstraction — PaymentProvider.supported_payment_types is the
                       ONLY gate for whether a provider may be selected
                       for a renewal: NONE of the four real adapters
                       (Mercado Pago/Stripe/NOWPayments/Binance Pay)
                       declare PaymentType.RECURRING today (all declare
                       {ONE_TIME} only — see each adapter's own module
                       docstring for why), so route_checkout() with
                       payment_type=RECURRING correctly raises
                       NoEligibleProvider against the REAL registry —
                       this module never hardcodes that fact, it falls
                       out naturally from the SAME capability-filter
                       eligible_providers() already uses for checkout.
                       Tests use a FakeProvider that DOES declare
                       RECURRING to exercise the machinery end-to-end.
  Webhook processing   — routes/billing.py's receive_webhook() is
                       UNCHANGED and NOT extended this etapa (see
                       "Webhook abstraction" below) — a renewal
                       PaymentOrder is confirmed the exact same way any
                       other order is: a provider webhook eventually
                       calling PaymentOrderService.confirm_order()
                       (today: only reachable via this module's own
                       confirm_renewal(), which calls confirm_order()
                       directly — no adapter produces a real recurring
                       webhook event yet).
  Refund              — PlanPurchaseNotRefundable already blocks refund
                       on ANY plan-purchase order, subscription-linked
                       or not — no change needed; a renewal PaymentOrder
                       is refused a refund by the EXISTING guard.
  Billing status       — PaymentStatus (PaymentOrder) and
                       SubscriptionStatus (this module) are DELIBERATELY
                       separate enums: an individual charge's status
                       (pending/processing/confirmed/failed/...) is not
                       the same axis as the AGREEMENT's status (active/
                       past_due/grace_period/cancelled/expired) — see
                       Subscription's own module docstring for the
                       one-agreement-many-charges relationship this
                       preserves.

Reused, NOT duplicated
-----------------------
This module is a thin orchestration layer:
  * Provider selection / failover / concurrency-safety -> reuses
    src.billing.payment_router.route_checkout() UNCHANGED (only new
    parameter: subscription_id, threaded through to
    PaymentOrderService.create_order(), which already had the column).
  * Ledger + entitlement activation on a successful charge -> reuses
    PaymentOrderService.confirm_order() UNCHANGED.
  * Row-locking pattern -> mirrors
    PaymentOrderService.lock_order_for_provider_attempt() exactly (new
    lock_subscription_for_update() below, same SELECT ... FOR UPDATE
    idiom, same SQLite-ignores-the-lock/PostgreSQL-serializes caveat).
No adapter (Stripe/Mercado Pago/NOWPayments/Binance Pay) is modified by
this etapa, and none of their own code is imported here beyond the
generic PaymentProvider/ProviderRegistry/PaymentMethod/PaymentType
abstractions every other billing module already depends on.

Renewal identity — deterministic per billing cycle
--------------------------------------------------
idempotency_key = f"sub:{subscription_id}:cycle:{cycle_start.date().isoformat()}"
(see _cycle_idempotency_key()). Retrying attempt_renewal() for the SAME
(subscription_id, cycle_start) ALWAYS resolves to the exact same
PaymentOrder row (create_order()'s own idempotency, unchanged) — a
retry, or two concurrent renewal attempts for the same cycle, can never
create two external charges. This is the SAME guarantee route_checkout()
already gives an ordinary checkout, applied here to a recurring cycle
instead of a one-time purchase.

Failure taxonomy (mirrors the Payment Router's own — see that module's
docstring for the base rationale)
--------------------------------------------------------------------
  Explicit rejection   — route_checkout() raises NoEligibleProvider or
                         AllProvidersRejected -> record_renewal_failure()
                         is called: a REAL "no" was received (or every
                         eligible provider said no) with no ambiguity
                         about whether a charge exists.
  Provider unavailable /
  unknown result        — route_checkout() raises ProviderUnavailable ->
                         subscription status is NEVER changed. The
                         PaymentOrder is left 'pending' (see
                         route_checkout()'s own hardening) for a later
                         reconciliation job to resolve (not built this
                         etapa) — timeout is NEVER silently treated as
                         "payment not completed".
  Payment confirmed     — confirm_renewal() -> PaymentOrderService.
                         confirm_order() (ledger + user.plan, atomic,
                         unchanged) -> Subscription moves to ACTIVE,
                         counters reset, period fields advance.

Grace period
------------
Duration is settings.subscription_grace_period_days (config, NOT a
business decision — see config.py's own comment). During grace_period:
  * NO additional benefit is granted — this module never touches
    entitlement/user.plan while in grace_period.
  * Entitlement behavior is whatever the domain ALREADY does: user.plan
    stays at whatever confirm_order() last set it to (this module never
    downgrades it) — that is the explicit, current entitlement behavior
    for this phase, stated so a future decision has a documented
    starting point rather than an implicit one.
  * enter_grace_period() (via record_renewal_failure()) and
    expire_subscription() both lock the row first — a subscription
    cannot be renewed AND expired at the same instant; see "Concurrency"
    below.

Downgrade / cancellation policy — NOT implemented, by design
--------------------------------------------------------------------
cancel_subscription() and expire_subscription() never touch user.plan.
What plan a user falls back to after cancellation/expiration is an
explicit, UNMADE commercial decision (see mission constraints) — wiring
that in now would be inventing policy this codebase has no authority
over. A future phase adds that as an isolated, configurable rule reading
Subscription's final status, not a change to this module.

cancel_subscription() (post-commit hardening, 2026-09-08) now also asks
the PROVIDER to actually stop billing (adapter.cancel_subscription())
before writing status='cancelled' locally — see that method's own
docstring for the full safe-against-failure design. expire_subscription()
does NOT do this — see the KNOWN GAP note below.

KNOWN GAP — expire_subscription()/expire_overdue_subscriptions() do not
reconcile with the provider. Local GRACE_PERIOD -> EXPIRED is a purely
LOCAL, TIME-based decision (settings.subscription_grace_period_days);
it never calls adapter.cancel_subscription(). For a PUSH-model provider
(Stripe), this means: (a) Stripe may keep attempting to charge a
subscription this codebase has already marked EXPIRED locally, and (b)
if Stripe's own dunning eventually succeeds where the local grace
period already gave up, apply_subscription_webhook_event()'s
RENEWAL_SUCCEEDED branch will correctly REFUSE to apply it
(_assert_renewable() excludes EXPIRED) — the real payment is detected
but rejected, not silently dropped (it is caught and logged at the
routes/billing.py webhook handler as an inapplicable-transition
warning), but no PaymentOrder/ledger/entitlement is ever created for
that real charge. This is NOT reconciled in this codebase — there is
not currently enough information (no retrieve_subscription()-based
polling loop, no scheduler) to safely decide which side should win.
Do not build automatic reconciliation without a real product decision
on: should local expiry also force-cancel at the provider? Should a
provider-side success after local expiry reactivate the subscription?
Both are legitimate answers with different UX/financial implications
that this codebase has no authority to pick.

Webhook abstraction — NOW WIRED for Stripe (Stripe Recurring etapa)
--------------------------------------------------------------------
SubscriptionEventKind/SubscriptionWebhookEvent (src.billing.providers.
base) name the FIVE internal event kinds a per-provider webhook
translation layer produces: SUBSCRIPTION_CREATED, RENEWAL_SUCCEEDED,
RENEWAL_FAILED, SUBSCRIPTION_CANCELED, SUBSCRIPTION_UPDATED.
StripeAdapter.verify_subscription_webhook() is the first REAL
translation (confirmed event-name mapping — see that adapter's own
module docstring). apply_subscription_webhook_event() below is the
SINGLE consumer — it dispatches on `event.kind` only, NEVER inspects
`event.raw_payload`'s provider-specific shape, so a future Mercado Pago/
Asaas/Adyen translation layer plugs in with zero changes here.
SUBSCRIPTION_UPDATED still has no real consumer (no adapter reports
amount/method changes with a confirmed contract yet) — apply_
subscription_webhook_event() logs and no-ops for it, deliberately, per
"não invente eventos específicos sem confirmar o contrato existente".
NOT wired to the shared HTTP webhook route (routes/billing.py's
receive_webhook()) this etapa — see this module's own "Stripe Recurring
etapa additions" section below for why (a payment-order-centric route,
heavily tested across 4 providers, needs a dedicated, focused pass to
safely also dispatch subscription-lifecycle events; every piece here is
exercised directly, by adapter+service calls, in
test_stripe_subscription.py).

Concurrency — reviewed explicitly for every pair the mission named
--------------------------------------------------------------------
* Two simultaneous renewal requests (same cycle): both call
  attempt_renewal() -> create_order() is idempotent (existing DB-level
  guarantee) -> lock_subscription_for_update() serializes the rest;
  at most one create_intent() call reaches a provider (same mechanism
  route_checkout() already proves for ordinary checkouts).
* Retry of the same cycle: same idempotency_key -> same PaymentOrder
  row -> route_checkout()'s own idempotent-return path (no new intent).
* Duplicate webhook (future): confirm_renewal() calls confirm_order(),
  already idempotent (fast-path return on an already-confirmed order).
* Webhook before/after the renewal job: whichever reaches
  confirm_order()/record_renewal_failure() first determines the
  Subscription's resulting state under the SAME row lock; the other is
  a safely-absorbed no-op (mirrors the exact pattern
  test_billing_postgres_concurrency.py's PGC03 already proves for
  PaymentOrder confirm-vs-expire).
* Cancellation concurrent with renewal: cancel_subscription() and
  attempt_renewal()/confirm_renewal() both acquire
  lock_subscription_for_update() first — whichever commits first wins;
  the loser's next status-check (assert_subscription_renewable() /
  the CANCELLED-guard in cancel_subscription()) safely rejects instead
  of corrupting state.
* Expiration job concurrent with renewal: same lock; expire_subscription()
  requires status == GRACE_PERIOD AND grace_period_ends_at elapsed —
  a subscription a concurrent renewal just reactivated to ACTIVE fails
  that check and is left untouched.

Stripe Recurring etapa additions — external subscription identity/ordering
--------------------------------------------------------------------------
* Two identities, never conflated: Subscription.id (internal) vs.
  Subscription.provider_subscription_id (external, e.g. Stripe's real
  "sub_..." — set ONLY by confirm_external_subscription(), never by
  initiate_external_subscription() itself, which only ever learns a
  SETUP intent id — see provider_setup_intent_id's own docstring on the
  model for why a THIRD field was needed to make creation itself
  idempotent across retries/concurrent calls).
* Two simultaneous "create this Subscription's external mandate"
  requests: route_subscription_creation() row-locks the Subscription
  and checks provider_setup_intent_id/provider_subscription_id BEFORE
  calling adapter.create_subscription() — at most one external call
  reaches Stripe, exactly like route_checkout()'s own PaymentOrder-level
  guarantee (see that function's own docstring in payment_router.py).
* Timeout/ambiguous outcome during creation: ProviderUnavailable
  propagates un-retried; provider_setup_intent_id/provider_subscription_id
  stay NULL — a human/future reconciliation job (not built) would call
  retrieve_subscription() out of band, never an automatic second attempt.
* Webhook ordering: apply_subscription_webhook_event() compares the
  incoming event's occurred_at against the LAST APPLIED EVENT OF THE
  SAME STREAM (see _EVENT_STREAM below and the Subscription model's
  own "webhook ORDERING guard, PER STREAM" docstring) under the SAME
  row lock — an older-or-duplicate event of that stream is absorbed
  without mutating state, regardless of HTTP arrival order, but a
  genuinely new event of the OTHER stream is never blocked by it (see
  migration 0028 for the post-commit hardening this required — a
  single shared timestamp across both streams was found to silently
  drop legitimate invoice/payment events).
* Wired to routes/billing.py's shared webhook route (Stripe Recurring
  hardening pass, 2026-09-08): PaymentProvider.is_subscription_event()
  (base.py) lets that route pick verify_subscription_webhook() over
  verify_webhook() for a subscription-lifecycle delivery, then calls
  apply_subscription_webhook_event() below — same endpoint, same
  webhook_nonce replay table, no parallel webhook system.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.billing.payment_router import SubscriptionRowNotFound, route_checkout, route_subscription_creation
from src.billing.pricing import purchasable_plans
from src.billing.providers.base import (
    PaymentCreationRejected,
    PaymentIntent,
    PaymentMethod,
    PaymentType,
    ProviderUnavailable,
    SubscriptionEventKind,
    SubscriptionIntent,
    SubscriptionWebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.config import settings
from src.models.payment_order import PaymentOrder
from src.models.subscription import Subscription, SubscriptionStatus
from src.services.payment_order_service import PaymentOrderService

logger = logging.getLogger(__name__)

# Webhook abstraction (SubscriptionEventKind/SubscriptionWebhookEvent) now
# lives in src.billing.providers.base — see that module's own "Subscription
# Provider Contract" section. Imported above, not redefined here; adapters
# (StripeAdapter.verify_subscription_webhook()) produce these directly.


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SubscriptionNotFound(Exception):
    def __init__(self, subscription_id: str) -> None:
        self.subscription_id = subscription_id
        super().__init__(f"Subscription {subscription_id!r} not found")


class InvalidSubscriptionTransition(Exception):
    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Cannot transition Subscription from {from_status!r} to {to_status!r}")


class SubscriptionNotRenewable(Exception):
    """Raised by attempt_renewal()/confirm_renewal() when the
    subscription is in a terminal state (cancelled/expired) — no charge
    may ever be attempted or confirmed against it again."""

    def __init__(self, subscription_id: str, status: str) -> None:
        self.subscription_id = subscription_id
        self.status = status
        super().__init__(f"Subscription {subscription_id!r} is {status!r} and cannot be renewed")


class GracePeriodNotElapsed(Exception):
    """Raised by expire_subscription() when called before
    grace_period_ends_at — mirrors OrderExpired's own real-time-check
    pattern (confirm_order()/expire_order() never trust a caller's
    claim that time has passed; they check it themselves)."""

    def __init__(self, subscription_id: str, grace_period_ends_at: datetime) -> None:
        self.subscription_id = subscription_id
        self.grace_period_ends_at = grace_period_ends_at
        super().__init__(
            f"Subscription {subscription_id!r} grace period does not end until "
            f"{grace_period_ends_at.isoformat()}"
        )


# ---------------------------------------------------------------------------
# Status transition table
# ---------------------------------------------------------------------------

_VALID_FROM: dict[str, frozenset[str]] = {
    SubscriptionStatus.ACTIVE.value: frozenset({
        SubscriptionStatus.ACTIVE.value,      # renewal confirmed while already active
        SubscriptionStatus.PAST_DUE.value,
        # settings.subscription_max_past_due_cycles=0 means "zero
        # tolerance" — the FIRST failed cycle already exceeds the
        # threshold, skipping PAST_DUE entirely. Still a real, valid
        # transition (not a bypass of the state machine).
        SubscriptionStatus.GRACE_PERIOD.value,
        SubscriptionStatus.CANCELLED.value,
    }),
    SubscriptionStatus.PAST_DUE.value: frozenset({
        SubscriptionStatus.ACTIVE.value,       # retry succeeded
        SubscriptionStatus.PAST_DUE.value,     # another failed cycle, still under threshold
        SubscriptionStatus.GRACE_PERIOD.value,
        SubscriptionStatus.CANCELLED.value,
    }),
    SubscriptionStatus.GRACE_PERIOD.value: frozenset({
        SubscriptionStatus.ACTIVE.value,        # payment succeeded during grace
        SubscriptionStatus.GRACE_PERIOD.value,  # another failed cycle, still within the window
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.CANCELLED.value,
    }),
    SubscriptionStatus.CANCELLED.value: frozenset(),  # terminal
    SubscriptionStatus.EXPIRED.value: frozenset(),    # terminal
}

# Statuses from which a renewal attempt/confirmation may ever proceed —
# CANCELLED/EXPIRED are excluded (terminal, see SubscriptionNotRenewable).
_RENEWABLE_STATUSES = frozenset({
    SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PAST_DUE.value, SubscriptionStatus.GRACE_PERIOD.value,
})


def _assert_valid_transition(from_status: str, to_status: str) -> None:
    if to_status not in _VALID_FROM.get(from_status, frozenset()):
        raise InvalidSubscriptionTransition(from_status, to_status)


def _cycle_idempotency_key(subscription_id: str, cycle_start: datetime) -> str:
    """Deterministic per (subscription, billing cycle) — see module
    docstring's "Renewal identity" section. Date-granularity (not full
    timestamp) so a retry naming "the same" cycle a few seconds/minutes
    apart still resolves to the identical key."""
    return f"sub:{subscription_id}:cycle:{cycle_start.date().isoformat()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Webhook ordering guard — WHICH independent event stream a given
# SubscriptionEventKind belongs to (see Subscription model's own
# "webhook ORDERING guard, PER STREAM" docstring and migration 0028 for
# the full rationale: Stripe delivers events for two unrelated objects
# per subscription — the Subscription object itself, and the Invoice
# object — with no guaranteed relative ordering of timestamps BETWEEN
# them, so each stream must be guarded independently or a legitimate
# event in one stream can be silently dropped by a coincidentally
# later-processed event in the OTHER stream).
_EVENT_STREAM: dict[SubscriptionEventKind, str] = {
    SubscriptionEventKind.SUBSCRIPTION_CREATED: "subscription",
    SubscriptionEventKind.SUBSCRIPTION_UPDATED: "subscription",
    SubscriptionEventKind.SUBSCRIPTION_CANCELED: "subscription",
    SubscriptionEventKind.RENEWAL_SUCCEEDED: "invoice",
    SubscriptionEventKind.RENEWAL_FAILED: "invoice",
}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SubscriptionService:
    """Manages the Subscription lifecycle. See module docstring for the
    full architecture, reuse strategy, and concurrency analysis."""

    def __init__(self, db: DBSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_subscription(
        self,
        *,
        user_id: str,
        plan: str,
        provider: str,
        payment_method: PaymentMethod,
        currency: str,
        amount: Decimal,
        current_period_start: datetime | None = None,
        current_period_end: datetime | None = None,
        next_billing_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Subscription:
        """Create a new Subscription in 'active' status.

        Deliberately does NOT contact any provider — provider-side
        activation (a real recurring mandate) is a separate, future
        concern this etapa does not implement (see module docstring's
        "Provider abstraction" section: no adapter declares
        PaymentType.RECURRING yet). `plan` is validated against
        src/billing/pricing.py's catalog, same convention PaymentOrder.
        plan already uses.

        Final-review hardening: `idempotency_key` (optional — None
        preserves every pre-existing caller's behavior unchanged) makes
        creation itself idempotent, mirroring
        PaymentOrderService.create_order() exactly (same
        find-first/IntegrityError-recovery shape) — a double-click or
        retried "create subscription" request with the SAME key can
        never produce two Subscription rows for the same logical
        intent. Without a key, no such protection exists — the caller
        is responsible for not calling this twice for the same intent.
        """
        if plan not in purchasable_plans():
            raise ValueError(f"Plan {plan!r} is not purchasable (unknown plan, or has no price)")

        if idempotency_key is not None:
            existing = (
                self._db.query(Subscription)
                .filter(Subscription.idempotency_key == idempotency_key)
                .first()
            )
            if existing is not None:
                return existing

        subscription = Subscription(
            user_id=user_id,
            plan=plan,
            provider=provider,
            payment_method=payment_method.value,
            payment_type=PaymentType.RECURRING.value,
            currency=currency.upper(),
            amount=amount,
            status=SubscriptionStatus.ACTIVE.value,
            current_period_start=current_period_start,
            current_period_end=current_period_end,
            next_billing_at=next_billing_at,
            idempotency_key=idempotency_key,
        )
        self._db.add(subscription)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            if idempotency_key is not None:
                existing = (
                    self._db.query(Subscription)
                    .filter(Subscription.idempotency_key == idempotency_key)
                    .first()
                )
                if existing is not None:
                    return existing
            raise
        self._db.refresh(subscription)
        return subscription

    # ------------------------------------------------------------------
    # Read / lock
    # ------------------------------------------------------------------

    def get_subscription(self, subscription_id: str, user_id: str) -> Subscription | None:
        return (
            self._db.query(Subscription)
            .filter(Subscription.id == subscription_id, Subscription.user_id == user_id)
            .first()
        )

    def lock_subscription_for_update(self, subscription_id: str) -> Subscription:
        """Row-level lock — see module docstring's "Concurrency"
        section and PaymentOrderService.lock_order_for_provider_attempt()'s
        own docstring for the identical idiom/caveats (PostgreSQL
        serializes; SQLite ignores the lock in dev/test)."""
        subscription = (
            self._db.query(Subscription)
            .filter(Subscription.id == subscription_id)
            .with_for_update()
            .first()
        )
        if subscription is None:
            raise SubscriptionNotFound(subscription_id)
        return subscription

    def _assert_renewable(self, subscription: Subscription) -> None:
        if subscription.status not in _RENEWABLE_STATUSES:
            raise SubscriptionNotRenewable(subscription.id, subscription.status)

    # ------------------------------------------------------------------
    # Renewal
    # ------------------------------------------------------------------

    async def attempt_renewal(
        self,
        registry: ProviderRegistry,
        subscription_id: str,
        *,
        cycle_start: datetime,
        cycle_end: datetime,
        expires_at: datetime | None = None,
    ) -> tuple[Subscription, PaymentOrder, PaymentIntent | None]:
        """Attempt to charge one billing cycle for `subscription_id`.

        Idempotent per (subscription_id, cycle_start) — see module
        docstring's "Renewal identity" section. Reuses route_checkout()
        for provider selection/failover/concurrency-safety UNCHANGED
        (see module docstring's "Reused, NOT duplicated" section).

        On a deterministic rejection (NoEligibleProvider/
        AllProvidersRejected), calls record_renewal_failure() and
        RE-RAISES the original exception (caller sees both the concrete
        reason AND the resulting Subscription state via a fresh read).
        On ProviderUnavailable, the Subscription is left COMPLETELY
        untouched — propagates as-is; never treated as a failure.
        """
        subscription = self.lock_subscription_for_update(subscription_id)
        self._assert_renewable(subscription)
        # Release the Subscription row lock BEFORE the (possibly slow)
        # provider call — it is NOT what prevents a duplicate charge.
        # That guarantee comes entirely from route_checkout()'s own
        # PaymentOrder-level lock (lock_order_for_provider_attempt()),
        # keyed by the SAME deterministic idempotency_key computed
        # below: two concurrent attempt_renewal() calls for the same
        # cycle both resolve to the identical PaymentOrder row, and
        # AT MOST ONE of them ever reaches adapter.create_intent() —
        # exactly the guarantee route_checkout() already proves for an
        # ordinary checkout, reused here unchanged. Holding THIS lock
        # across the external call would only add unnecessary DB
        # contention for no additional safety.
        self._db.commit()

        idempotency_key = _cycle_idempotency_key(subscription_id, cycle_start)
        payment_method = PaymentMethod(subscription.payment_method)

        try:
            order, intent = await route_checkout(
                self._db, registry,
                user_id=subscription.user_id,
                plan=subscription.plan,
                amount_fiat=subscription.amount,
                currency_fiat=subscription.currency,
                credits_amount=subscription.amount,
                currency_credits=subscription.currency,
                idempotency_key=idempotency_key,
                expires_at=expires_at or (_utcnow() + timedelta(hours=1)),
                payment_method=payment_method,
                payment_type=PaymentType.RECURRING,
                subscription_id=subscription.id,
            )
        except Exception as exc:
            from src.billing.payment_router import AllProvidersRejected, NoEligibleProvider
            from src.billing.providers.base import ProviderUnavailable

            if isinstance(exc, (NoEligibleProvider, AllProvidersRejected)):
                logger.warning(
                    "subscription: renewal explicitly rejected | subscription_id=%s cycle_start=%s reason=%s",
                    subscription_id, cycle_start.date().isoformat(), exc,
                )
                self.record_renewal_failure(subscription_id)
            elif isinstance(exc, ProviderUnavailable):
                logger.error(
                    "subscription: renewal outcome AMBIGUOUS (provider unavailable) | subscription_id=%s "
                    "cycle_start=%s — status left UNCHANGED, requires reconciliation",
                    subscription_id, cycle_start.date().isoformat(),
                )
            raise

        logger.info(
            "subscription: renewal intent created | subscription_id=%s cycle_start=%s provider=%s order_id=%s",
            subscription_id, cycle_start.date().isoformat(), order.provider, order.id,
        )
        return subscription, order, intent

    def confirm_renewal(self, subscription_id: str, order_id: str) -> Subscription:
        """Confirm a renewal charge — reuses
        PaymentOrderService.confirm_order() UNCHANGED for the atomic
        ledger + user.plan effect (see module docstring). Advances the
        Subscription's period fields and clears any past-due/grace
        state. Idempotent: PaymentOrderService.confirm_order()'s own
        fast-path handles a duplicate call (e.g. a duplicate webhook, in
        a future phase) transparently."""
        subscription = self.lock_subscription_for_update(subscription_id)
        self._assert_renewable(subscription)

        order_svc = PaymentOrderService(self._db)
        order = order_svc.get_order(order_id, subscription.user_id)
        if order is None or order.subscription_id != subscription_id:
            raise SubscriptionNotFound(subscription_id)  # defensive — mismatched order/subscription pair

        order = order_svc.confirm_order(order_id, subscription.user_id)

        _assert_valid_transition(subscription.status, SubscriptionStatus.ACTIVE.value)
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.consecutive_failed_cycles = 0
        subscription.grace_period_started_at = None
        subscription.grace_period_ends_at = None
        if order.confirmed_at is not None:
            subscription.current_period_start = order.confirmed_at
        self._db.commit()
        self._db.refresh(subscription)
        logger.info(
            "subscription: renewal confirmed | subscription_id=%s order_id=%s new_status=%s",
            subscription_id, order_id, subscription.status,
        )
        return subscription

    def record_renewal_failure(self, subscription_id: str) -> Subscription:
        """Records an EXPLICIT rejection (never an ambiguous/
        ProviderUnavailable outcome — see attempt_renewal()'s own
        docstring) against a billing cycle.

        The counter is incremented FIRST, then compared against
        settings.subscription_max_past_due_cycles REGARDLESS of the
        current status (ACTIVE or PAST_DUE) — this correctly handles
        max_past_due_cycles=0 ("zero tolerance": the very first failure
        already exceeds the threshold and jumps straight to
        GRACE_PERIOD, skipping PAST_DUE) as well as the ordinary
        max_past_due_cycles>=1 case (N tolerated PAST_DUE cycles, then
        GRACE_PERIOD on the next one). GRACE_PERIOD itself stays
        GRACE_PERIOD regardless of further failures — expiry from there
        is time-based, via expire_subscription(), never cycle-count-based.
        """
        subscription = self.lock_subscription_for_update(subscription_id)
        self._assert_renewable(subscription)

        subscription.consecutive_failed_cycles += 1

        if subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PAST_DUE.value):
            entering_grace = subscription.consecutive_failed_cycles > settings.subscription_max_past_due_cycles
            target = SubscriptionStatus.GRACE_PERIOD.value if entering_grace else SubscriptionStatus.PAST_DUE.value
            _assert_valid_transition(subscription.status, target)
            subscription.status = target
            if entering_grace:
                subscription.grace_period_started_at = _utcnow()
                subscription.grace_period_ends_at = _utcnow() + timedelta(
                    days=settings.subscription_grace_period_days
                )
        # else: already GRACE_PERIOD — counter incremented, status unchanged;
        # expiry is decided by expire_subscription()'s own time check, not here.

        self._db.commit()
        self._db.refresh(subscription)
        logger.info(
            "subscription: renewal failure recorded | subscription_id=%s new_status=%s consecutive_failed_cycles=%s",
            subscription_id, subscription.status, subscription.consecutive_failed_cycles,
        )
        return subscription

    # ------------------------------------------------------------------
    # Expiration / cancellation
    # ------------------------------------------------------------------

    def expire_subscription(self, subscription_id: str, *, now: datetime | None = None) -> Subscription:
        """GRACE_PERIOD -> EXPIRED, only once grace_period_ends_at has
        actually passed (checked here, never trusted from the caller —
        same real-time-check discipline as PaymentOrderService's own
        confirm_order()/expire_order()). Does NOT touch user.plan — see
        module docstring's "Downgrade / cancellation policy" section.

        KNOWN GAP (documented, not fixed): does NOT call
        adapter.cancel_subscription() — a provider-side mandate is left
        untouched by a local expiry. See module docstring's "KNOWN GAP"
        note for the full reconciliation tradeoffs this codebase does
        not have the authority to decide yet."""
        subscription = self.lock_subscription_for_update(subscription_id)
        now = now or _utcnow()
        # SQLite returns naive datetimes (no tzinfo); PostgreSQL returns
        # aware ones. Normalise BOTH sides to UTC-aware before comparing
        # — same DB-agnostic discipline as PaymentOrderService.confirm_order().
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        _assert_valid_transition(subscription.status, SubscriptionStatus.EXPIRED.value)

        ends_at = subscription.grace_period_ends_at
        if ends_at is not None and ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at is None or now < ends_at:
            self._db.rollback()  # release the lock — no mutation
            raise GracePeriodNotElapsed(subscription_id, subscription.grace_period_ends_at)

        subscription.status = SubscriptionStatus.EXPIRED.value
        self._db.commit()
        self._db.refresh(subscription)
        logger.info("subscription: expired | subscription_id=%s", subscription_id)
        return subscription

    async def cancel_subscription(
        self, registry: ProviderRegistry, subscription_id: str, *, user_id: str,
    ) -> Subscription:
        """ACTIVE/PAST_DUE/GRACE_PERIOD -> CANCELLED.

        Post-commit hardening (2026-09-08, functional audit finding):
        this method previously changed ONLY local status, never asking
        the provider to actually stop billing — a user "cancelling" in
        TRINDATRON left a real Stripe subscription active, so Stripe
        kept charging the real card every cycle. Fixed here by calling
        adapter.cancel_subscription() (Subscription Provider Contract,
        base.py) BEFORE ever writing status='cancelled', whenever a
        provider_subscription_id exists:

          1. If provider_subscription_id is None — nothing was ever
             confirmed externally (still just a setup attempt, or
             cancelled before initiate_external_subscription() was ever
             called): there is nothing to cancel at the provider, so
             the local transition happens immediately.
          2. Otherwise — the row lock is released BEFORE the external
             call (same rationale as attempt_renewal()'s own: the lock
             isn't what prevents a duplicate/unsafe cancel; a retry
             naturally re-enters this same idempotent method), then
             adapter.cancel_subscription(provider_subscription_id) is
             awaited:
               - success (no exception) -> re-lock and write
                 status='cancelled' — the provider's own 2xx response
                 to an immediate-cancel DELETE IS the confirmation (see
                 StripeAdapter.cancel_subscription()'s own docstring:
                 it cancels synchronously, not via a later webhook).
               - ProviderUnavailable (ambiguous — timeout/network) ->
                 propagates UN-CAUGHT, local status is left COMPLETELY
                 unchanged. NEVER "fake" a cancellation the provider
                 never confirmed — the caller must retry.
               - PaymentCreationRejected (explicit rejection — a known,
                 non-ambiguous "no" from the provider's own API, e.g. a
                 4xx) -> also propagates un-caught, local status
                 unchanged, so the caller sees the real reason instead
                 of a silently "successful" cancel.

        Idempotent: already-cancelled returns unchanged (fast path, no
        re-transition, no second call to the provider — cancelling an
        already-cancelled mandate is a no-op that would otherwise waste
        an external call for nothing). A LATER webhook confirming the
        same cancellation (SUBSCRIPTION_CANCELED, handled by
        apply_subscription_webhook_event()) is separately idempotent —
        see that method's own fast path — so this method and that one
        can safely race without corrupting state.

        Does NOT touch user.plan — see module docstring's "Downgrade /
        cancellation policy" section.
        """
        subscription = self.lock_subscription_for_update(subscription_id)
        if subscription.user_id != user_id:
            self._db.rollback()
            raise SubscriptionNotFound(subscription_id)  # ownership check — no cross-user info leak

        if subscription.status == SubscriptionStatus.CANCELLED.value:
            self._db.commit()  # release the lock — idempotent no-op
            return subscription

        _assert_valid_transition(subscription.status, SubscriptionStatus.CANCELLED.value)

        provider_subscription_id = subscription.provider_subscription_id
        if provider_subscription_id is None:
            # Never confirmed externally — nothing exists at the
            # provider to cancel.
            subscription.status = SubscriptionStatus.CANCELLED.value
            subscription.cancelled_at = _utcnow()
            self._db.commit()
            self._db.refresh(subscription)
            logger.info(
                "subscription: cancelled locally (no external mandate existed) | subscription_id=%s",
                subscription_id,
            )
            return subscription

        provider_name = subscription.provider
        self._db.commit()  # release the lock BEFORE the external call

        try:
            adapter = registry.get(provider_name)
            await adapter.cancel_subscription(provider_subscription_id)
        except ProviderUnavailable:
            logger.error(
                "subscription: cancel outcome AMBIGUOUS (provider unavailable) | subscription_id=%s "
                "provider=%s — status left UNCHANGED, safe to retry",
                subscription_id, provider_name,
            )
            raise
        except PaymentCreationRejected:
            logger.warning(
                "subscription: provider explicitly rejected cancellation | subscription_id=%s provider=%s",
                subscription_id, provider_name,
            )
            raise

        # Re-lock — a concurrent cancel_subscription() retry or an
        # already-arrived SUBSCRIPTION_CANCELED webhook may have applied
        # this exact transition while the external call was in flight.
        subscription = self.lock_subscription_for_update(subscription_id)
        if subscription.status != SubscriptionStatus.CANCELLED.value:
            _assert_valid_transition(subscription.status, SubscriptionStatus.CANCELLED.value)
            subscription.status = SubscriptionStatus.CANCELLED.value
            subscription.cancelled_at = _utcnow()
        self._db.commit()
        self._db.refresh(subscription)
        logger.info(
            "subscription: cancelled (provider confirmed) | subscription_id=%s provider=%s",
            subscription_id, provider_name,
        )
        return subscription

    # ------------------------------------------------------------------
    # Stripe Recurring etapa — external subscription creation + webhooks
    # ------------------------------------------------------------------

    async def initiate_external_subscription(
        self,
        registry: ProviderRegistry,
        subscription_id: str,
        *,
        idempotency_key: str,
    ) -> tuple[Subscription, SubscriptionIntent]:
        """Establish (or idempotently resume) `subscription_id`'s external
        mandate. Thin wrapper around route_subscription_creation() — see
        that function's own docstring for the eligibility/idempotency/
        concurrency guarantees (all provided there, not duplicated here).
        Only responsibility added at this layer: translate the router's
        LOCAL SubscriptionRowNotFound into this module's own
        SubscriptionNotFound, the same exception every other method here
        raises for an unknown subscription_id.

        NoEligibleProvider / AllSubscriptionProvidersRejected /
        ProviderUnavailable propagate unchanged — see
        route_subscription_creation()'s own docstring for their exact
        meaning; this method has no additional handling for them (the
        caller decides retry/surface policy, same precedent as
        attempt_renewal() re-raising after only logging).
        """
        try:
            return await route_subscription_creation(
                self._db, registry, subscription_id=subscription_id, idempotency_key=idempotency_key,
            )
        except SubscriptionRowNotFound as exc:
            raise SubscriptionNotFound(exc.subscription_id) from exc

    def _lock_subscription_for_event(self, event: SubscriptionWebhookEvent) -> Subscription | None:
        """Correlate an incoming SubscriptionWebhookEvent back to ITS
        internal Subscription row and row-lock it. SUBSCRIPTION_CREATED
        matches on provider_setup_intent_id (event.provider_ref — the
        setup Checkout Session's own id, the only identity known before
        confirmation); every other kind matches on
        provider_subscription_id (event.provider_subscription_id — the
        real, confirmed mandate id). Returns None (never raises) when no
        row matches or the event lacks the field it would need — an
        unmatched webhook is a safely-absorbed no-op, not a caller error
        (see apply_subscription_webhook_event()'s own docstring)."""
        if event.kind == SubscriptionEventKind.SUBSCRIPTION_CREATED:
            if not event.provider_ref:
                return None
            query = self._db.query(Subscription).filter(Subscription.provider_setup_intent_id == event.provider_ref)
        else:
            if not event.provider_subscription_id:
                return None
            query = self._db.query(Subscription).filter(
                Subscription.provider_subscription_id == event.provider_subscription_id
            )
        return query.with_for_update().first()

    @staticmethod
    def _is_duplicate_or_stale_event(subscription: Subscription, event: SubscriptionWebhookEvent) -> bool:
        """Webhook ordering guard — PER STREAM (see module docstring's
        "Stripe Recurring etapa additions" section and _EVENT_STREAM's
        own docstring for why: the Subscription-object stream and the
        Invoice-object stream are compared against INDEPENDENT
        last-applied markers, so a more-recently-processed event in one
        stream can never make a genuinely new event in the OTHER stream
        look stale). Must be called under the SAME row lock
        _lock_subscription_for_event() just acquired, and BEFORE any
        state mutation, so the check-then-set is atomic against a
        concurrent delivery of the same or an older event."""
        stream = _EVENT_STREAM[event.kind]
        last_id = subscription.last_subscription_event_id if stream == "subscription" else subscription.last_invoice_event_id
        last_at = subscription.last_subscription_event_at if stream == "subscription" else subscription.last_invoice_event_at

        if event.event_id and last_id == event.event_id:
            return True  # exact duplicate delivery (same stream)
        if last_at is not None:
            occurred_at = event.occurred_at
            # SQLite returns naive datetimes; PostgreSQL returns aware
            # ones — normalise to UTC-aware before comparing, same
            # DB-agnostic discipline used throughout this module.
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            if occurred_at < last_at:
                return True  # genuinely older event of the SAME stream, arrived late
        return False

    @staticmethod
    def _mark_webhook_applied(subscription: Subscription, event: SubscriptionWebhookEvent) -> None:
        stream = _EVENT_STREAM[event.kind]
        if stream == "subscription":
            subscription.last_subscription_event_id = event.event_id
            subscription.last_subscription_event_at = event.occurred_at
        else:
            subscription.last_invoice_event_id = event.event_id
            subscription.last_invoice_event_at = event.occurred_at

    def confirm_external_subscription(self, event: SubscriptionWebhookEvent) -> Subscription | None:
        """Consume a SUBSCRIPTION_CREATED SubscriptionWebhookEvent,
        setting Subscription.provider_subscription_id — the ONLY place
        that field is ever written (see its own docstring on the model).
        Idempotent: an already-set value is never overwritten (a
        duplicate/replayed delivery is a safe no-op), and the webhook
        ordering guard absorbs a stale/duplicate event before any write.
        Returns None if no Subscription's provider_setup_intent_id
        matches this event's provider_ref (unmatched — logged, not
        raised; see _lock_subscription_for_event()'s own docstring)."""
        if event.kind != SubscriptionEventKind.SUBSCRIPTION_CREATED:
            raise ValueError(
                f"confirm_external_subscription() only accepts SUBSCRIPTION_CREATED events, got {event.kind!r}"
            )
        subscription = self._lock_subscription_for_event(event)
        if subscription is None:
            self._db.rollback()
            logger.warning(
                "subscription: SUBSCRIPTION_CREATED webhook unmatched | provider_ref=%s event_id=%s",
                event.provider_ref, event.event_id,
            )
            return None

        if self._is_duplicate_or_stale_event(subscription, event):
            self._db.commit()  # release the lock — no mutation
            logger.info(
                "subscription: SUBSCRIPTION_CREATED webhook absorbed (duplicate/out-of-order) | "
                "subscription_id=%s event_id=%s",
                subscription.id, event.event_id,
            )
            return subscription

        self._mark_webhook_applied(subscription, event)
        if subscription.provider_subscription_id is None:
            subscription.provider_subscription_id = event.provider_subscription_id
        self._db.commit()
        self._db.refresh(subscription)
        logger.info(
            "subscription: external subscription confirmed | subscription_id=%s provider_subscription_id=%s",
            subscription.id, subscription.provider_subscription_id,
        )
        return subscription

    def apply_subscription_webhook_event(self, event: SubscriptionWebhookEvent) -> Subscription | None:
        """SINGLE consumer of SubscriptionWebhookEvent — see module
        docstring's "Webhook abstraction" section for why every
        per-provider translation layer (StripeAdapter.
        verify_subscription_webhook(), and any future one) funnels here
        instead of SubscriptionService having provider-specific branches.

        Dispatches on `event.kind` only:
          SUBSCRIPTION_CREATED   -> confirm_external_subscription()
          RENEWAL_SUCCEEDED      -> reactively create+confirm this
                                    cycle's PaymentOrder (idempotency_key
                                    derived from the provider's own
                                    invoice reference, event.provider_ref
                                    — see _cycle... note below), then
                                    confirm_renewal() UNCHANGED for the
                                    ledger+entitlement+status effect.
          RENEWAL_FAILED         -> record_renewal_failure() UNCHANGED.
          SUBSCRIPTION_CANCELED  -> system-initiated cancellation (no
                                    user_id is available from a webhook,
                                    so this does NOT call the user-facing
                                    cancel_subscription() — inlined here,
                                    same target state).
          SUBSCRIPTION_UPDATED   -> logged, no-op (no confirmed consumer
                                    contract yet — see module docstring).

        Returns None for an event that matches no known Subscription
        (logged, never raised — a webhook handler must never 500 on an
        event it simply can't correlate, e.g. a stale/foreign delivery).
        """
        if event.kind == SubscriptionEventKind.SUBSCRIPTION_CREATED:
            # Delegates its OWN lock/ordering-guard/mark cycle — no need
            # to acquire one here first (avoids a redundant lock/rollback
            # round trip for the one kind that has a dedicated method).
            return self.confirm_external_subscription(event)

        subscription = self._lock_subscription_for_event(event)
        if subscription is None:
            self._db.rollback()
            logger.warning(
                "subscription: webhook event unmatched | kind=%s provider_subscription_id=%s provider_ref=%s "
                "event_id=%s",
                event.kind.value, event.provider_subscription_id, event.provider_ref, event.event_id,
            )
            return None

        if self._is_duplicate_or_stale_event(subscription, event):
            self._db.commit()  # release the lock — no mutation
            logger.info(
                "subscription: webhook event absorbed (duplicate/out-of-order) | subscription_id=%s kind=%s "
                "event_id=%s",
                subscription.id, event.kind.value, event.event_id,
            )
            return subscription

        self._mark_webhook_applied(subscription, event)

        if event.kind == SubscriptionEventKind.SUBSCRIPTION_UPDATED:
            self._db.commit()
            self._db.refresh(subscription)
            logger.info(
                "subscription: SUBSCRIPTION_UPDATED webhook received, no-op (no confirmed consumer contract) | "
                "subscription_id=%s",
                subscription.id,
            )
            return subscription

        if event.kind == SubscriptionEventKind.SUBSCRIPTION_CANCELED:
            if subscription.status != SubscriptionStatus.CANCELLED.value:
                _assert_valid_transition(subscription.status, SubscriptionStatus.CANCELLED.value)
                subscription.status = SubscriptionStatus.CANCELLED.value
                subscription.cancelled_at = _utcnow()
            self._db.commit()
            self._db.refresh(subscription)
            logger.info("subscription: cancelled via webhook | subscription_id=%s", subscription.id)
            return subscription

        # RENEWAL_SUCCEEDED / RENEWAL_FAILED — persist the webhook-ordering
        # markers now (release the lock) BEFORE calling out to
        # record_renewal_failure()/confirm_renewal(), which re-acquire
        # their OWN lock on this same row (safe: it's a fresh lock in a
        # NEW transaction, not a re-entrant lock within this one).
        subscription_id = subscription.id
        self._db.commit()

        if event.kind == SubscriptionEventKind.RENEWAL_FAILED:
            return self.record_renewal_failure(subscription_id)

        # RENEWAL_SUCCEEDED
        subscription = self._db.query(Subscription).filter(Subscription.id == subscription_id).first()
        # Check renewability BEFORE creating any PaymentOrder — a
        # terminal (cancelled/expired) subscription must never get a
        # dangling 'pending' order it can never confirm (confirm_renewal()
        # below raises SubscriptionNotRenewable too, but only AFTER
        # create_order() would already have committed a row).
        self._assert_renewable(subscription)
        cycle_ref = event.provider_ref or event.event_id
        idempotency_key = f"sub:{subscription_id}:invoice:{cycle_ref}"
        amount = event.amount if event.amount is not None else subscription.amount
        currency = (event.currency or subscription.currency).upper()

        order_svc = PaymentOrderService(self._db)
        order = order_svc.create_order(
            user_id=subscription.user_id,
            provider=subscription.provider,
            amount_fiat=amount,
            currency_fiat=currency,
            credits_amount=amount,
            currency_credits=currency,
            idempotency_key=idempotency_key,
            expires_at=_utcnow() + timedelta(hours=1),
            plan=subscription.plan,
            payment_method=subscription.payment_method,
            payment_type=PaymentType.RECURRING.value,
            subscription_id=subscription_id,
        )
        return self.confirm_renewal(subscription_id, order.id)


def expire_overdue_subscriptions(db: DBSession, *, now: datetime | None = None) -> int:
    """Bulk-sweep every 'grace_period' Subscription whose
    grace_period_ends_at has passed, transitioning each to 'expired' via
    the EXISTING single-row SubscriptionService.expire_subscription() —
    no new expiry logic, just batching it (mirrors
    payment_order_service.expire_overdue_orders() exactly). Returns the
    count transitioned. NOT wired to any scheduler this etapa — see
    mission instruction "não criar scheduler externo de produção ainda".
    """
    now = now or _utcnow()
    svc = SubscriptionService(db)
    overdue = (
        db.query(Subscription)
        .filter(
            Subscription.status == SubscriptionStatus.GRACE_PERIOD.value,
            Subscription.grace_period_ends_at < now,
        )
        .all()
    )
    count = 0
    for subscription in overdue:
        try:
            svc.expire_subscription(subscription.id, now=now)
            count += 1
        except (SubscriptionNotFound, InvalidSubscriptionTransition, GracePeriodNotElapsed):
            # Another caller (e.g. a concurrent renewal that reactivated
            # this subscription) already moved it elsewhere — idempotent,
            # not an error. Same precedent as expire_overdue_orders()'s
            # own sweep.
            pass
    if count:
        logger.info("subscription: expired %d overdue grace-period subscription(s)", count)
    return count
