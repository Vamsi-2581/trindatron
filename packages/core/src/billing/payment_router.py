"""
Payment Router — orchestration layer that picks WHICH registered
PaymentProvider handles a checkout, sitting between
routes/billing.py's create_checkout() and
payment_order_service.create_provider_order().

Architectural audit this etapa was built on (summarized here so the
design rationale below is traceable to real code, not assumption):

1. Interfaces the router consumes
   ---------------------------------
   * src/billing/providers/base.py — PaymentProvider ABC (create_intent/
     get_intent/verify_webhook/refund), its capability properties
     (supported_payment_methods/supported_payment_types/
     supported_currencies), and its exception vocabulary
     (ProviderUnavailable, PaymentCreationRejected — see below).
   * src/billing/providers/registry.py — ProviderRegistry.names()/get()/
     is_registered(). The router NEVER imports a concrete adapter class.
   * src/services/payment_order_service.py — PaymentOrderService.
     create_order() (idempotent-by-idempotency_key), the NEW
     lock_order_for_provider_attempt()/reassign_provider_unsafe_guard()
     (added this etapa — see that module's own docstrings),
     mark_processing(), and the existing create_provider_order()
     (reused UNCHANGED for the LEGACY explicit-`provider` checkout path
     — the router only replaces AUTO-selection).

2. Where the provider was previously chosen
   -------------------------------------------
   routes/billing.py's _resolve_checkout_provider() (Payments Foundation
   etapa): a PLAIN capability filter — if exactly one registered
   provider matched payment_method/payment_type, use it; zero matches
   -> 409; two-or-more matches -> 400 ("ambiguous, specify `provider`").
   This etapa REPLACES that filter-only behavior with real priority-
   ordered selection (no more "ambiguous" 400 — see route_checkout()
   below) — _resolve_checkout_provider() itself is removed from
   billing.py in favor of this module.

3. PaymentOrder data available for the decision
   ------------------------------------------------
   At checkout time: user_id, plan, amount_fiat/currency_fiat (server-
   priced, from pricing.py), credits_amount/currency_credits,
   idempotency_key, expires_at, payment_method, payment_type (all
   already on the model — see PaymentOrder's own docstring, Payments
   Foundation etapa). No country/region field exists ANYWHERE in the
   domain (User, PaymentOrder, CreateCheckoutRequest) — confirmed by
   grep before writing this module; eligibility below does NOT filter
   on country, per this mission's own "só considere o que já existe no
   domínio" instruction. See eligible_providers()'s own docstring for
   exactly where a `country` filter would slot in once that field
   exists.

4. Capabilities each provider declares (as of this etapa)
   ------------------------------------------------------------
     mercado_pago:  methods={PIX, CREDIT_CARD, DEBIT_CARD, BOLETO}
                    types={ONE_TIME}
                    currencies={ARS,BRL,CLP,COP,MXN,PEN,UYU} (confirmed, narrow)
     stripe:        methods={CREDIT_CARD, DEBIT_CARD, BOLETO} (no Pix — invite-only, unconfirmed)
                    types={ONE_TIME}
                    currencies=UNRESTRICTED (Stripe's real list is broad and not exhaustively confirmed — see base.py's own docstring for why empty means "no restriction" here, the OPPOSITE default from methods/types)
     nowpayments:   methods={CRYPTO}  types={ONE_TIME}  currencies=UNRESTRICTED
     binance_pay:   methods={CRYPTO}  types={ONE_TIME}  currencies=UNRESTRICTED

5. Where payment-method/currency/type rules live
   ---------------------------------------------------
   src/billing/providers/base.py's three capability properties (per-
   adapter declarations) + src/billing/pricing.py (which currencies a
   PLAN is even priced in, checked by the route BEFORE the router ever
   runs — a plan unpriced in the requested currency 404s earlier, so the
   router only ever sees a currency the catalog already confirmed).
   Country/region: not yet part of the domain (see #3).

6. How retry/failover is kept from double-charging
   -------------------------------------------------------
   See "Failure taxonomy" and "Concurrency" sections below — the two
   mechanisms (safe-vs-ambiguous exception classification, and the row
   lock spanning the external call) are the actual answer; summarized
   here only as an index into them.

7. How the effectively-chosen provider is persisted
   --------------------------------------------------------
   PaymentOrder.provider (already existed) — set at row-creation time,
   mutated in place ONLY pre-external-commitment during failover (see
   PaymentOrderService.reassign_provider_unsafe_guard()). This is the
   SAME column routes/billing.py, the webhook route, and every existing
   adapter test already read — the router changes WHEN/HOW it gets
   written, never its meaning or any consumer of it.

8. Future gateways (Asaas, Adyen, ...)
   ------------------------------------------
   Register a new adapter (build_asaas_adapter() -> main.py lifespan,
   exactly like the four existing ones) and OPTIONALLY add its name to
   settings.payment_provider_priority. Zero changes to this module —
   eligible_providers() only ever calls the generic ABC surface
   (registry.names()/get(), the three capability properties), never a
   concrete adapter type. An unranked-but-registered provider is still
   selectable (sorts last, deterministically) — see _priority_index().

Failure taxonomy — what may safely fail over, what may NEVER
--------------------------------------------------------------------
Exactly two provider-call outcomes are possible from create_intent(),
and every one of the four existing adapters' transport layers already
normalizes into one or the other (verified against nowpayments_adapter.
py/binance_pay_adapter.py/mercadopago_adapter.py/stripe_adapter.py's own
`_post()`/`_get()` — every network/timeout exception is caught and
re-raised as ProviderUnavailable; a real HTTP response is what a
concrete adapter parses before ever raising its own XxxError):

  A/B) SAFE — PaymentCreationRejected (base.py, new this etapa; every
       adapter's own XxxError now inherits from it): a REAL response was
       received from the provider and it explicitly rejects the
       request. No charge/session could have been created provider-side
       — trying the next eligible candidate for the SAME logical
       operation cannot produce a duplicate. This covers BOTH "provider
       rejected before any real attempt" (bad currency, bad request
       shape) and "provider explicitly says no" cases the mission
       described as A/B — both are represented by the identical,
       single, provider-agnostic exception class.

  C/D) UNSAFE — ProviderUnavailable (timeout, connection failure): the
       request MAY have reached the provider and created a REAL
       external session/charge despite the local side never confirming
       it. NEVER triggers failover. The router stops the ENTIRE
       candidate loop immediately, leaves the order exactly as-is
       (provider = whichever adapter returned the ambiguous result,
       external_order_id still NULL), and re-raises — routes/billing.py
       maps this to the SAME 503 it always has for a single explicit
       provider. A human/later job resolves it (e.g. by querying the
       provider's own get_intent() out of band) — this etapa does not
       attempt automatic reconciliation (explicitly out of scope, see
       "Deliberately not implemented" below).

Concurrency — how two simultaneous calls for the SAME PaymentOrder
never both reach a provider
--------------------------------------------------------------------
Every entry into "is this order's external attempt still pending, and
if so, attempt it" — whether a brand-new checkout, a retry after a
crash, or the router's own failover step — goes through
PaymentOrderService.lock_order_for_provider_attempt() BEFORE inspecting
external_order_id or calling create_intent(). On PostgreSQL this SELECT
... FOR UPDATE genuinely serializes concurrent callers for the same row:
the loser blocks until the winner's transaction commits (having set
external_order_id via mark_processing()), then re-reads a row that
already has it set and returns immediately without ever calling the
provider a second time. SQLite (dev/test) silently ignores the lock —
same documented, pre-existing limitation as refund_order()'s own use of
this exact idiom; see test_billing_postgres_concurrency.py for the
real-DB-backed pattern this module's own tests intentionally do NOT
duplicate (a sequential-repeated-call stand-in is used instead, proving
the LOGICAL idempotency invariant, not the lock's true exclusivity).

Hardening follow-up (post-launch audit): the same lock also closes a
SEPARATE race against the expiry background job
(payment_order_service.expire_overdue_orders()/expire_order()), which
mutates a 'pending' order's status WITHOUT ever calling create_intent().
expire_order() now acquires the IDENTICAL row lock before transitioning
— see that method's own updated docstring — so it can never commit
'expired' out from under an in-flight create_intent() call, and this
function (via assert_order_still_pending(), called immediately after
every lock acquisition/re-acquisition) can never call create_intent()
for an order the sweep already expired moments before this call even
started. Whichever side acquires the lock first determines the outcome;
the loser's transition is safely rejected (OrderExpired/
InvalidStatusTransition), never a corrupted in-between state where a
real external session exists with no local record able to track it.

Idempotency — two layers preserved
--------------------------------------------------------------------
* TRINDATRON's own: PaymentOrder.idempotency_key UNIQUE +
  PaymentOrderService.create_order()'s existing IntegrityError-recovery
  (unchanged) — a retry with the SAME idempotency_key NEVER creates a
  second row, router or no router.
* Provider-native (where confirmed): Mercado Pago/Stripe both receive
  their own idempotency header on create_intent() (see each adapter's
  own module docstring) — a genuine retry of the identical outbound
  request is deduplicated a SECOND time, provider-side.
The router adds a THIRD guarantee on top: it never generates a new
"logical identity" for the same operation — see "How the effectively-
chosen provider is persisted" above; a retry always resumes the ONE
existing row via lock_order_for_provider_attempt(), never a new one.

Selection strategy — eligibility + deterministic priority, NOT cost
--------------------------------------------------------------------
No cost/quote-based routing exists or is invented here — this mission
explicitly forbids it without real, live cost data (none exists in this
domain today). Selection is: filter registered providers by the three
capability properties (methods/types/currency) -> sort the survivors by
settings.payment_provider_priority (an explicit, ordered, config-driven
list — see config.py's own comment — never scattered if/else) -> take
survivors in that order, attempting create_intent() against each in
turn until one succeeds or a genuinely UNSAFE (ambiguous) failure stops
the loop.

Deliberately NOT implemented this etapa (see the mission's own list)
--------------------------------------------------------------------
* Cost-based / real-time-quote routing.
* Automatic reconciliation after a ProviderUnavailable (no background
  job polls the ambiguous provider's own get_intent() to resolve it —
  a human/future job does).
* Country/region-based eligibility (the field doesn't exist yet).
* Chargeback handling or refund policy — this module never touches
  either.

Stripe Recurring etapa (2026-09-08) addendum — route_subscription_creation()
--------------------------------------------------------------------
Added below: the SAME eligibility/priority/failover/lock-then-check
shape as route_checkout() above, applied to establishing a NEW
provider-side recurring mandate (Subscription.provider_subscription_id)
instead of a NEW PaymentOrder. Reuses eligible_providers() UNCHANGED
(filtered by PaymentType.RECURRING — today only Stripe declares it, so
this naturally has one candidate; the SAME failover shape applies
transparently once a second RECURRING-capable provider exists, with NO
code change here). The row lock is acquired via a direct, inline query
rather than SubscriptionService.lock_subscription_for_update() — that
service module itself imports route_checkout() from THIS module, so the
reverse import would be circular; the query itself is a trivial,
non-business-logic SELECT ... FOR UPDATE, not a duplicated business rule.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session as DBSession

from src.billing.providers.base import (
    PaymentCreationRejected,
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    SubscriptionIntent,
)
from src.billing.providers.registry import ProviderNotFound, ProviderRegistry
from src.config import settings
from src.models.payment_order import PaymentOrder
from src.models.subscription import Subscription
from src.services.payment_order_service import PaymentOrderService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NoEligibleProvider(Exception):
    """Raised when ZERO registered providers satisfy the requested
    payment_method/payment_type/currency — before any external call is
    ever attempted (no PaymentOrder row is created for this)."""

    def __init__(
        self, *, payment_method: PaymentMethod | None, payment_type: PaymentType | None,
        currency: str, excluded: dict[str, str],
    ) -> None:
        self.payment_method = payment_method
        self.payment_type = payment_type
        self.currency = currency
        self.excluded = excluded
        super().__init__(
            f"No eligible payment provider for payment_method={payment_method!r}, "
            f"payment_type={payment_type!r}, currency={currency!r}. "
            f"Excluded: {excluded!r}"
        )


class AllProvidersRejected(Exception):
    """Raised when every ELIGIBLE candidate's create_intent() raised
    PaymentCreationRejected (a real, deterministic rejection) — the
    order row still exists (provider = the LAST candidate genuinely
    tried), but no provider could create the intent."""

    def __init__(self, *, order_id: str, tried: list[str]) -> None:
        self.order_id = order_id
        self.tried = tried
        super().__init__(
            f"All eligible payment providers rejected checkout for order_id={order_id!r}: tried={tried!r}"
        )


class SubscriptionRowNotFound(Exception):
    """Raised by route_subscription_creation() when subscription_id
    doesn't exist. Deliberately LOCAL to this module (not
    subscription_service.SubscriptionNotFound) to avoid a circular
    import — SubscriptionService itself imports route_checkout() from
    this module. SubscriptionService.initiate_external_subscription()
    translates this into its own SubscriptionNotFound at the service
    boundary."""

    def __init__(self, subscription_id: str) -> None:
        self.subscription_id = subscription_id
        super().__init__(f"Subscription {subscription_id!r} not found")


class AllSubscriptionProvidersRejected(Exception):
    """Raised when every ELIGIBLE RECURRING-capable candidate's
    create_subscription() raised PaymentCreationRejected."""

    def __init__(self, *, subscription_id: str, tried: list[str]) -> None:
        self.subscription_id = subscription_id
        self.tried = tried
        super().__init__(
            f"All eligible payment providers rejected subscription creation for "
            f"subscription_id={subscription_id!r}: tried={tried!r}"
        )


# ---------------------------------------------------------------------------
# Eligibility + priority — pure, no DB/HTTP, independently testable.
# ---------------------------------------------------------------------------


def _priority_index(provider_name: str, priority: list[str]) -> int:
    try:
        return priority.index(provider_name)
    except ValueError:
        return len(priority)  # unranked -> sorts after every ranked provider


def resolve_default_currency(
    registry: ProviderRegistry,
    *,
    catalog_currencies: list[str],
    provider: str | None = None,
    payment_method: PaymentMethod | None = None,
    payment_type: PaymentType | None = None,
) -> str | None:
    """Pick which of `catalog_currencies` (a plan/seat's own priced
    currencies, in priority order — see pricing.currency_candidates() /
    team_pricing.seat_currency_candidates(), the only intended callers)
    the checkout should actually charge in — B2: Mercado Pago (BRL-only,
    no USD) gets BRL, every USD-capable provider keeps USD unchanged.

    Returns the first catalog currency ANY eligible provider can accept,
    or None if none can (caller's job to turn that into NoEligibleProvider/
    404, exactly as before this function existed for a plan priced in a
    single currency).

    Two modes, mirroring the two checkout dispatch paths in routes/
    billing.py:
      * `provider` given (LEGACY explicit-provider path): check that ONE
        adapter's own supported_currencies directly — no priority/
        failover logic applies, there is only one candidate.
      * `provider` omitted (auto-selection path): reuse eligible_providers()
        per candidate currency — the first currency with at least one
        eligible registered provider wins, so the SAME provider priority
        route_checkout() will itself apply later governs which currency
        is even offered.
    """
    if provider is not None:
        try:
            adapter = registry.get(provider)
        except ProviderNotFound:
            return None
        for currency in catalog_currencies:
            if not adapter.supported_currencies or currency.upper() in adapter.supported_currencies:
                return currency
        return None

    for currency in catalog_currencies:
        candidates, _ = eligible_providers(
            registry, payment_method=payment_method, payment_type=payment_type, currency=currency,
        )
        if candidates:
            return currency
    return None


def eligible_providers(
    registry: ProviderRegistry,
    *,
    payment_method: PaymentMethod | None,
    payment_type: PaymentType | None,
    currency: str,
    priority: list[str] | None = None,
) -> tuple[list[tuple[str, PaymentProvider]], dict[str, str]]:
    """Return (eligible, excluded) for this request.

    `eligible` is priority-ordered (settings.payment_provider_priority by
    default; unranked registered providers sort last, then alphabetical
    as a final deterministic tiebreak — see _priority_index()).
    `excluded` maps provider_name -> a human-readable reason, for
    observability (never raised as an error here — NoEligibleProvider is
    the caller's job when `eligible` ends up empty).

    "provider enabled" and "credenciais/configuração completas" are
    DELIBERATELY not re-checked here — registry.names() membership
    ALREADY guarantees both (every build_xxx_adapter() factory returns
    None, and is therefore never registered, unless enabled=True AND
    every required credential is non-empty — see each adapter's own
    factory function). Re-checking config here would duplicate, not
    strengthen, that guarantee.

    Country/region is not filtered — see this module's own docstring
    section 3: no such field exists in the domain yet. Once it does,
    add it here as `adapter.supported_countries` (same pattern as
    currency below), never as an if/else on a specific provider name.
    """
    priority = priority if priority is not None else settings.payment_provider_priority
    currency_upper = currency.upper()

    eligible: list[tuple[str, PaymentProvider]] = []
    excluded: dict[str, str] = {}
    for name in registry.names():
        adapter = registry.get(name)
        reasons: list[str] = []

        if payment_method is not None and payment_method not in adapter.supported_payment_methods:
            reasons.append(f"payment_method={payment_method.value!r} not supported")
        if payment_type is not None and payment_type not in adapter.supported_payment_types:
            reasons.append(f"payment_type={payment_type.value!r} not supported")
        # Empty supported_currencies means UNRESTRICTED for this ONE
        # capability — see PaymentProvider.supported_currencies' own
        # docstring for the deliberate asymmetry vs. methods/types above.
        if adapter.supported_currencies and currency_upper not in adapter.supported_currencies:
            reasons.append(f"currency={currency_upper!r} not supported")

        if reasons:
            excluded[name] = "; ".join(reasons)
        else:
            eligible.append((name, adapter))

    eligible.sort(key=lambda item: (_priority_index(item[0], priority), item[0]))
    return eligible, excluded


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _order_priority_list(order_provider: str, candidates: list[tuple[str, PaymentProvider]], registry: ProviderRegistry) -> list[tuple[str, PaymentProvider]]:
    """Reorders `candidates` so that `order_provider` (whatever is
    ALREADY persisted on the order — from an earlier attempt, this same
    call, or a concurrent race) is tried FIRST, never silently
    overridden by a freshly recomputed priority. If `order_provider`
    isn't among the current eligibility candidates (e.g. it was
    deregistered since, or never matched this request's capability
    filter — only possible if it was assigned by a DIFFERENT, earlier
    call with different inputs), it is still tried first PROVIDED it is
    still registered — resuming an in-flight attempt takes precedence
    over recomputing eligibility for a row that already exists."""
    ordered = [c for c in candidates if c[0] == order_provider]
    ordered += [c for c in candidates if c[0] != order_provider]
    if not ordered or ordered[0][0] != order_provider:
        try:
            ordered.insert(0, (order_provider, registry.get(order_provider)))
        except ProviderNotFound:
            pass  # fall back to the eligible list only
    return ordered


async def route_checkout(
    db: DBSession,
    registry: ProviderRegistry,
    *,
    user_id: str,
    plan: str | None,
    amount_fiat: Decimal,
    currency_fiat: str,
    credits_amount: Decimal,
    currency_credits: str,
    idempotency_key: str,
    expires_at: datetime,
    payment_method: PaymentMethod | None,
    payment_type: PaymentType | None,
    subscription_id: str | None = None,
) -> tuple[PaymentOrder, PaymentIntent | None]:
    """Auto-select an eligible provider and create the checkout —
    REPLACES routes/billing.py's old _resolve_checkout_provider() +
    inline create_provider_order() dispatch for the auto-selection path
    (no explicit `provider` given). The LEGACY explicit-`provider` path
    is UNCHANGED — it still calls create_provider_order() directly, with
    no failover (there is nothing to fail over BETWEEN when the caller
    pinned one exact provider).

    Raises:
        NoEligibleProvider:    zero registered providers match method/
                               type/currency — no order row is created.
        AllProvidersRejected:  every eligible candidate's create_intent()
                               deterministically rejected the request.
        ProviderUnavailable:   an AMBIGUOUS failure (timeout/network) —
                               the loop stops immediately, no further
                               candidate is tried. Same exception class
                               routes/billing.py already maps to 503 for
                               the single-explicit-provider path.
        OrderExpired:          a concurrent expire_order() call (the
                               background sweep) won the race for this
                               row before create_intent() was ever
                               called — see PaymentOrderService.
                               assert_order_still_pending()'s own
                               docstring. routes/billing.py maps this to
                               409 (same mapping confirm_order() already
                               gets via _map_service_error()).
        InvalidStatusTransition: same race, a different non-'pending'
                               status (e.g. a concurrent cancel/fail).

    `subscription_id` (Subscription Engine etapa) links the created
    PaymentOrder to a recurring agreement — passed straight through to
    PaymentOrderService.create_order() (see that method's own
    docstring). None (the default) is every pre-existing caller's
    behavior, completely unaffected; this function has NO subscription-
    specific logic of its own — SubscriptionService is the only caller
    that ever passes a non-None value, reusing this exact same
    eligibility/failover/concurrency machinery for a renewal attempt
    instead of duplicating any of it.
    """
    candidates, excluded = eligible_providers(
        registry, payment_method=payment_method, payment_type=payment_type, currency=currency_fiat,
    )
    logger.info(
        "payment_router: eligibility computed | payment_method=%s payment_type=%s currency=%s "
        "eligible=%s excluded=%s",
        payment_method.value if payment_method else None,
        payment_type.value if payment_type else None,
        currency_fiat.upper(),
        [name for name, _ in candidates],
        excluded,
    )
    if not candidates:
        raise NoEligibleProvider(
            payment_method=payment_method, payment_type=payment_type, currency=currency_fiat, excluded=excluded,
        )

    svc = PaymentOrderService(db)
    payment_method_value = payment_method.value if payment_method else None
    payment_type_value = payment_type.value if payment_type else None

    # Idempotent: a retry with the SAME idempotency_key (or a concurrent
    # racing call) returns the EXISTING row untouched — see create_order()'s
    # own docstring. The first candidate is only a PROPOSAL for a brand-new
    # row; an existing row's OWN persisted provider always wins below.
    order = svc.create_order(
        user_id=user_id, provider=candidates[0][0], amount_fiat=amount_fiat, currency_fiat=currency_fiat,
        credits_amount=credits_amount, currency_credits=currency_credits,
        idempotency_key=idempotency_key, expires_at=expires_at, plan=plan,
        payment_method=payment_method_value, payment_type=payment_type_value,
        subscription_id=subscription_id,
    )

    # Row lock spans every remaining step — see module docstring's
    # "Concurrency" section. Held until a commit/rollback below.
    order = svc.lock_order_for_provider_attempt(order.id)
    if order.external_order_id:
        db.commit()  # release the lock — already succeeded (this call or a concurrent one).
        return order, None
    try:
        svc.assert_order_still_pending(order)
    except Exception:
        # Hardening: a concurrent expire_order() (or cancel/fail) call
        # may have won the race for this row BEFORE this call even
        # acquired the lock — see PaymentOrderService.
        # assert_order_still_pending()'s own docstring. Never call
        # create_intent() for an order already moved past 'pending'.
        db.commit()  # release the lock — no mutation happened here
        raise

    ordered = _order_priority_list(order.provider, candidates, registry)

    last_exc: PaymentCreationRejected | None = None
    tried: list[str] = []
    for name, adapter in ordered:
        if order.provider != name:
            svc.reassign_provider_unsafe_guard(order)
            order.provider = name
            db.commit()  # durable checkpoint BEFORE the next external call
            order = svc.lock_order_for_provider_attempt(order.id)  # re-lock post-commit
            try:
                svc.assert_order_still_pending(order)
            except Exception:
                db.commit()
                raise

        tried.append(name)
        intent_metadata = {"payment_method": payment_method_value} if payment_method_value else None
        try:
            intent = await adapter.create_intent(
                amount=amount_fiat, currency=currency_fiat, user_id=user_id,
                idempotency_key=idempotency_key, metadata=intent_metadata,
            )
        except PaymentCreationRejected as exc:
            logger.warning(
                "payment_router: provider=%s rejected checkout | order_id=%s reason=%s — trying next candidate",
                name, order.id, exc,
            )
            last_exc = exc
            continue
        except ProviderUnavailable:
            db.commit()  # persist provider=name as the last genuinely attempted one
            logger.error(
                "payment_router: provider=%s unavailable (ambiguous outcome) | order_id=%s — NOT failing over",
                name, order.id,
            )
            raise

        order = svc.mark_processing(order.id, order.user_id, external_order_id=intent.id)
        logger.info(
            "payment_router: selected provider=%s | order_id=%s payment_method=%s payment_type=%s currency=%s",
            name, order.id, payment_method_value, payment_type_value, currency_fiat.upper(),
        )
        return order, intent

    db.commit()  # persist the final (last-tried) provider assignment even though every candidate rejected
    logger.error(
        "payment_router: all eligible providers rejected | order_id=%s tried=%s", order.id, tried,
    )
    raise AllProvidersRejected(order_id=order.id, tried=tried) from last_exc


async def route_subscription_creation(
    db: DBSession,
    registry: ProviderRegistry,
    *,
    subscription_id: str,
    idempotency_key: str,
) -> tuple[Subscription, SubscriptionIntent]:
    """Auto-select an eligible RECURRING-capable provider and create the
    external subscription mandate — see module docstring's "Stripe
    Recurring etapa addendum" for the full design and why the row lock
    is inlined here rather than reusing SubscriptionService's own.

    Called by SubscriptionService.initiate_external_subscription() ONLY
    — this function has NO subscription-status-machine knowledge (it
    never sets Subscription.status), it only establishes/records the
    PROVIDER identity and provider_subscription_id (the latter stays
    NULL here — only a later webhook confirms it, see
    SubscriptionService.confirm_external_subscription()).

    Raises:
        SubscriptionRowNotFound:            subscription_id doesn't exist.
        NoEligibleProvider:                 zero registered providers
                                             declare PaymentType.RECURRING
                                             for this method/currency.
        AllSubscriptionProvidersRejected:   every eligible candidate's
                                             create_subscription()
                                             deterministically rejected.
        ProviderUnavailable:                AMBIGUOUS failure — the loop
                                             stops immediately; NEVER
                                             creates a second external
                                             subscription for the same
                                             intent on retry (the row's
                                             OWN persisted `provider` is
                                             resumed, exactly like
                                             route_checkout()'s own
                                             guarantee).
    """
    subscription = db.query(Subscription).filter(Subscription.id == subscription_id).with_for_update().first()
    if subscription is None:
        raise SubscriptionRowNotFound(subscription_id)

    payment_method = PaymentMethod(subscription.payment_method)
    candidates, excluded = eligible_providers(
        registry, payment_method=payment_method, payment_type=PaymentType.RECURRING, currency=subscription.currency,
    )
    logger.info(
        "payment_router: subscription-creation eligibility computed | subscription_id=%s payment_method=%s "
        "currency=%s eligible=%s excluded=%s",
        subscription_id, payment_method.value, subscription.currency.upper(),
        [name for name, _ in candidates], excluded,
    )
    if not candidates:
        db.commit()  # release the lock — nothing to do
        raise NoEligibleProvider(
            payment_method=payment_method, payment_type=PaymentType.RECURRING,
            currency=subscription.currency, excluded=excluded,
        )

    # Already CONFIRMED (a webhook already resolved the mandate) —
    # idempotent no-op.
    if subscription.provider_subscription_id:
        db.commit()
        return subscription, SubscriptionIntent(
            id=subscription.provider_setup_intent_id or subscription.id, provider=subscription.provider,
            status=PaymentStatus.CONFIRMED, idempotency_key=idempotency_key,
            provider_subscription_id=subscription.provider_subscription_id,
        )
    # Already ATTEMPTED (this call or a concurrent one reached the
    # provider and got a setup intent back) but not yet confirmed by a
    # webhook — idempotent no-op. Without this check, a retry BEFORE
    # confirmation would call create_subscription() a SECOND time — see
    # provider_setup_intent_id's own docstring on the Subscription model.
    if subscription.provider_setup_intent_id:
        db.commit()
        return subscription, SubscriptionIntent(
            id=subscription.provider_setup_intent_id, provider=subscription.provider,
            status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
        )

    ordered = _order_priority_list(subscription.provider, candidates, registry)

    last_exc: PaymentCreationRejected | None = None
    tried: list[str] = []
    for name, adapter in ordered:
        if subscription.provider != name:
            subscription.provider = name
            db.commit()  # durable checkpoint BEFORE the next external call
            subscription = (
                db.query(Subscription).filter(Subscription.id == subscription_id).with_for_update().first()
            )

        tried.append(name)
        try:
            intent = await adapter.create_subscription(
                amount=subscription.amount, currency=subscription.currency, user_id=subscription.user_id,
                idempotency_key=idempotency_key, metadata={"payment_method": payment_method.value},
            )
        except PaymentCreationRejected as exc:
            logger.warning(
                "payment_router: provider=%s rejected subscription creation | subscription_id=%s reason=%s "
                "— trying next candidate",
                name, subscription_id, exc,
            )
            last_exc = exc
            continue
        except ProviderUnavailable:
            db.commit()  # persist provider=name as the last genuinely attempted one
            logger.error(
                "payment_router: provider=%s unavailable (ambiguous outcome) | subscription_id=%s — NOT failing over",
                name, subscription_id,
            )
            raise

        subscription.provider_setup_intent_id = intent.id
        db.commit()
        logger.info(
            "payment_router: selected provider=%s for subscription creation | subscription_id=%s intent_id=%s",
            name, subscription_id, intent.id,
        )
        return subscription, intent

    db.commit()  # persist the final (last-tried) provider assignment even though every candidate rejected
    logger.error(
        "payment_router: all eligible providers rejected subscription creation | subscription_id=%s tried=%s",
        subscription_id, tried,
    )
    raise AllSubscriptionProvidersRejected(subscription_id=subscription_id, tried=tried) from last_exc
