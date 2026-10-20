"""
Billing route — PaymentOrder lifecycle API + Commercial Checkout V1.

Endpoints
---------
  GET    /billing/plans                     — purchasable plans + prices (200)
  GET    /billing/providers                 — payment providers usable right now (200)
  POST   /billing/checkout                  — start a plan-purchase checkout (201)
  POST   /billing/orders                    — create a generic (credit-purchase) order (201)
  GET    /billing/orders                    — list user's orders (200)
  GET    /billing/orders/{order_id}         — get one order (200)
  POST   /billing/orders/{order_id}/confirm — confirm order; credits/plan applied atomically (200)
  POST   /billing/orders/{order_id}/cancel  — cancel order (200)
  POST   /billing/orders/{order_id}/refund  — refund; credits debited atomically (200) —
                                               refused for a plan-purchase order (see
                                               PlanPurchaseNotRefundable)
  GET    /billing/balance                   — user's credit summary across all currencies (200)
  GET    /billing/balance/{currency}        — single-currency balance (200)
  POST   /billing/webhook/{provider}        — provider webhook (public, signature-verified) —
                                               routes BOTH one-time payment events AND (Stripe
                                               Recurring etapa) subscription-lifecycle events
                                               through this SAME endpoint (see receive_webhook()'s
                                               own docstring — no parallel webhook system)
  POST   /billing/subscriptions             — start a recurring plan subscription (201) —
                                               Stripe Recurring etapa
  GET    /billing/subscriptions/{id}        — get one subscription (200)
  POST   /billing/subscriptions/{id}/cancel — cancel a subscription; asks the PROVIDER to
                                               actually stop billing before updating local
                                               state (200) — see SubscriptionService.
                                               cancel_subscription()'s own docstring

Security invariants
-------------------
* user_id is ALWAYS sourced from the authenticated JWT via get_current_user.
  It is NEVER accepted from the request body — the body is untrusted.
* All order-specific endpoints enforce ownership: PaymentOrderService._get_or_raise()
  filters by (order_id, user_id). Cross-user access returns 404 without revealing
  that the order exists for another user.
* webhook_raw is not included in any response (absent from PaymentOrderResponse schema).
* No financial logic lives in this router. All delegated to PaymentOrderService and
  the services it orchestrates (CreditService, LedgerService).
* POST /orders/{id}/confirm refuses to confirm an order whose provider has a real
  adapter registered (registry.is_registered) — those providers prove payment via
  their own verified webhook; a bare client call is not evidence anything was paid.
  See confirm_order()'s docstring below.
* POST /orders and POST /checkout call out to a registered provider's adapter
  (create_provider_order()) when body.provider is registered; every other provider
  string takes the exact local-only path these endpoints always took.
* POST /checkout NEVER accepts amount_fiat/credits_amount from the client — always
  looked up server-side from src/billing/pricing.py, keyed by `plan`. A client
  cannot influence what it is charged for a given plan.
* The webhook handler validates the event's own amount/currency (when the provider
  supplies one) against the order's recorded amount BEFORE calling confirm_order() —
  a mismatch is a 400, never silently absorbed (see receive_webhook()'s step 6).
* POST /orders and POST /checkout are rate-limited per user (mission requirement 11)
  via a dedicated bucket — see _check_order_rate_limit().

Error mapping
-------------
  OrderNotFound                → 404 Not Found
  InvalidStatusTransition      → 409 Conflict
  OrderExpired                 → 409 Conflict
  RefundAmountExceedsEligible  → 422 Unprocessable Entity
  InsufficientCreditError      → 409 Conflict
  DuplicateIdempotencyKey      → 409 Conflict
  PlanPurchaseNotRefundable    → 409 Conflict
  Pydantic ValidationError     → 422 (handled automatically by FastAPI)

Idempotency preserved end-to-end
----------------------------------
  create_order / create_checkout — same idempotency_key returns existing order (no duplicate row)
  confirm_order                  — already-confirmed order returns immediately; no double-credit/activation
  refund_order                   — deterministic keys per order; safe to retry
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user, get_provider_registry
from src.billing.payment_router import (
    AllProvidersRejected,
    AllSubscriptionProvidersRejected,
    NoEligibleProvider,
    eligible_providers,
    resolve_default_currency,
    route_checkout,
)
from src.billing.pricing import CHECKOUT_CURRENCY, currency_candidates, get_plan_price, purchasable_plans
from src.billing.unit_economics import max_credits_for_payment
from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentCreationRejected,
    PaymentProvider,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    UnknownWebhookEvent,
)
from src.billing.providers.registry import ProviderNotFound, ProviderRegistry
from src.config import settings
from src.database import get_db
from src.models.ledger import CreditBalance
from src.models.payment_order import PaymentOrder
from src.models.user import User
from src.observability.metrics import core_metrics
from src.schemas.billing import (
    CheckoutResponse,
    CreateCheckoutRequest,
    CreateOrderRequest,
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    CreditBalanceResponse,
    PaymentOrderListResponse,
    PaymentOrderResponse,
    PaymentProvidersResponse,
    PlanPriceResponse,
    RefundOrderRequest,
    SubscriptionResponse,
    UserCreditSummaryResponse,
    WebhookEventResponse,
)
from src.services.billing_orchestrator import RefundAmountExceedsEligible
from src.services.credit_service import CreditService, DuplicateIdempotencyKey, InsufficientCreditError
from src.services.payment_order_service import (
    InvalidStatusTransition,
    OrderExpired,
    OrderNotFound,
    PaymentOrderService,
    PlanPurchaseNotRefundable,
    create_provider_order,
)
from src.services.request_control import InMemoryRateLimiter, RequestRejectedError
from src.services.subscription_service import (
    InvalidSubscriptionTransition,
    SubscriptionNotFound,
    SubscriptionNotRenewable,
    SubscriptionService,
)
from src.services import webhook_replay_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# ── Observability (Production Payment Activation — mission requirement
# 13). Counters only, no cost/TCU/secret ever recorded. ────────────────────
_orders_created_total = core_metrics.counter(
    "payment_orders_created_total", "PaymentOrders created, labeled by provider.",
)
_orders_confirmed_total = core_metrics.counter(
    "payment_orders_confirmed_total", "PaymentOrders confirmed, labeled by provider.",
)
_orders_failed_total = core_metrics.counter(
    "payment_orders_failed_total", "PaymentOrders that reached status=failed, labeled by provider.",
)
_webhook_invalid_signature_total = core_metrics.counter(
    "webhook_invalid_signature_total", "Webhook deliveries rejected for signature failure, labeled by provider.",
)
_plan_activation_failed_total = core_metrics.counter(
    "plan_activation_failed_total", "A plan-purchase order reached a terminal non-confirmed status, labeled by provider.",
)
_provider_unavailable_total = core_metrics.counter(
    "payment_provider_unavailable_total", "A registered provider's create_intent() raised ProviderUnavailable, labeled by provider.",
)

# Commercial Checkout V1 — dedicated rate limiter, own bucket (see
# config.py's billing_order_rate_limit docstring for why this is NOT the
# shared request_control singleton chat/agent routes use). Reuses the
# EXISTING InMemoryRateLimiter primitive (request_control.py) — no new
# rate-limiting mechanism.
_order_rate_limiter = InMemoryRateLimiter()
# Separate bucket, separate (generous) limits — the webhook has no
# authenticated user, so it is keyed by client IP instead. See
# config.py's billing_webhook_rate_limit docstring.
_webhook_rate_limiter = InMemoryRateLimiter()


async def _check_order_rate_limit(current_user: User) -> None:
    decision = await _order_rate_limiter.check(
        f"billing_order:{current_user.id}",
        limit=settings.billing_order_rate_limit,
        window_seconds=settings.billing_order_rate_window_s,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many order-creation attempts. Please wait and try again.",
        )


def _client_ip(request: Request) -> str | None:
    """Same extraction as sessions.py's own _client_ip() — see that
    function's own docstring for INFRA-01 (HIGH, final gate revision):
    prefers X-Real-IP (Railway's documented "single source of truth" for
    the connecting IP) over parsing X-Forwarded-For's ambiguous,
    contradictorily-documented hop chain; falls back to
    request.client.host."""
    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return request.client.host if request.client else None


async def _check_webhook_rate_limit(request: Request) -> None:
    client_ip = _client_ip(request)
    key = f"billing_webhook:{client_ip or 'unknown'}"
    decision = await _webhook_rate_limiter.check(
        key, limit=settings.billing_webhook_rate_limit, window_seconds=settings.billing_webhook_rate_window_s,
    )
    if not decision.allowed:
        logger.warning("webhook: rate limit exceeded | client_ip=%s", client_ip)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many webhook requests.")


# ---------------------------------------------------------------------------
# Exception helpers — domain errors → HTTP
# ---------------------------------------------------------------------------


def _404() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Payment order not found",
    )


def _404_subscription() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Subscription not found",
    )


def _map_service_error(exc: Exception) -> HTTPException:
    """Convert known service exceptions to appropriate HTTP errors.

    Unknown exceptions are re-raised — FastAPI will return 500.
    Internal details (balance amounts, stack traces) are never leaked.
    """
    if isinstance(exc, InvalidStatusTransition):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid status transition: '{exc.from_status}' → '{exc.to_status}'",
        )
    if isinstance(exc, OrderExpired):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment order has expired and can no longer be confirmed",
        )
    if isinstance(exc, RefundAmountExceedsEligible):
        return HTTPException(
            status_code=422,  # HTTP_422_UNPROCESSABLE_CONTENT in newer Starlette
            detail=f"Refund amount exceeds eligible ceiling of {exc.max_refundable}",
        )
    if isinstance(exc, InsufficientCreditError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Insufficient credit balance for this operation",
        )
    if isinstance(exc, DuplicateIdempotencyKey):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was previously used with different parameters",
        )
    if isinstance(exc, PlanPurchaseNotRefundable):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Payment order {exc.order_id!r} is a plan purchase and cannot be "
                "refunded through this endpoint."
            ),
        )
    raise exc  # unexpected — propagates as 500


def _webhook_ack(
    adapter: PaymentProvider,
    *,
    provider: str,
    event_type: str,
    order_id: str | None = None,
    subscription_id: str | None = None,
    new_status: str | None = None,
) -> JSONResponse | WebhookEventResponse:
    """Build the HTTP 200 response for a webhook that was received and
    handled (successfully processed, or intentionally absorbed — unknown
    event type, order not found; see receive_webhook()'s docstring).

    Some providers (e.g. Binance Pay) require a specific ack body or they
    keep retrying a webhook TRINITY already handled correctly — see
    PaymentProvider.webhook_ack_body()'s docstring. Providers with no
    override (the default, None) get the exact same WebhookEventResponse
    this route always returned before this helper existed — zero behavior
    change for them.

    `subscription_id` (Stripe Recurring etapa) is set instead of
    `order_id` for a subscription-lifecycle event delivery — never both.
    """
    override = adapter.webhook_ack_body()
    if override is not None:
        return JSONResponse(content=override, status_code=status.HTTP_200_OK)
    return WebhookEventResponse(
        received=True, provider=provider, event_type=event_type,
        order_id=order_id, subscription_id=subscription_id, new_status=new_status,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@router.post(
    "/orders",
    response_model=PaymentOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment order",
)
async def create_order(
    body: CreateOrderRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> PaymentOrderResponse:
    """Create a new PaymentOrder in 'pending' state.

    Idempotent: duplicate calls with the same idempotency_key return the
    existing order with 201 and the same body. credits_amount is locked
    at creation time and never recalculated at confirmation.

    Real provider (e.g. Binance Pay)
    ---------------------------------
    If body.provider has a real adapter registered, order creation goes
    through create_provider_order() (payment_order_service.py): the local
    PaymentOrder is created first (as always), THEN the adapter is asked to
    create its own order — on success the local order moves straight to
    'processing' with external_order_id set, ready for that provider's
    webhook to confirm it (see confirm_order()'s evidence-of-payment guard
    below — client-initiated confirmation is refused for these orders).
    A provider with no adapter registered (every provider string used by
    the existing test suite, and any legacy/manual-flow provider) takes the
    exact same path this endpoint always took — zero behavior change for it.

    Rate limited (mission requirement 11: protect against abusive creation
    of thousands of pending orders) — see _check_order_rate_limit().
    """
    await _check_order_rate_limit(current_user)
    # The client names the price and the credits; only the relationship
    # between them is ours to enforce. A webhook proves amount_fiat was paid,
    # never that credits_amount was worth it.
    credits_cap = max_credits_for_payment(
        float(body.amount_fiat), body.currency_fiat, body.currency_credits,
    )
    if credits_cap is None or body.credits_amount > Decimal(str(round(credits_cap, 8))):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="credits_amount exceeds what this payment can buy.",
        )
    if registry.is_registered(body.provider):
        adapter = registry.get(body.provider)
        try:
            order, _intent = await create_provider_order(
                db, adapter,
                user_id=current_user.id,  # always from JWT, never from body
                amount_fiat=body.amount_fiat,
                currency_fiat=body.currency_fiat,
                credits_amount=body.credits_amount,
                currency_credits=body.currency_credits,
                idempotency_key=body.idempotency_key,
                expires_at=body.expires_at,
            )
        except ProviderUnavailable as exc:
            _provider_unavailable_total.inc(provider=body.provider)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Payment provider {body.provider!r} is temporarily unavailable",
            ) from exc
        except (OrderExpired, InvalidStatusTransition) as exc:
            # Hardening: a concurrent expire_order() (background sweep)
            # won the race for this order before create_intent() was
            # ever called — see PaymentOrderService.
            # assert_order_still_pending()'s own docstring.
            raise _map_service_error(exc) from exc
        _orders_created_total.inc(provider=body.provider)
        return order

    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=current_user.id,  # always from JWT, never from body
        provider=body.provider,
        amount_fiat=body.amount_fiat,
        currency_fiat=body.currency_fiat,
        credits_amount=body.credits_amount,
        currency_credits=body.currency_credits,
        idempotency_key=body.idempotency_key,
        expires_at=body.expires_at,
    )
    _orders_created_total.inc(provider=body.provider)
    return order


# ---------------------------------------------------------------------------
# Commercial Checkout V1 — plan purchase
# ---------------------------------------------------------------------------


@router.get(
    "/plans",
    response_model=list[PlanPriceResponse],
    summary="List purchasable plans and their prices",
)
def list_plan_prices(
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> list[PlanPriceResponse]:
    """Server-side price catalog (src/billing/pricing.py) — the frontend
    must not maintain its own copy of these figures, same convention
    already established for entitlements.py's usage limits (see
    GET /users/me/entitlement).

    The currency shown for each plan is the SAME B2 default POST
    /billing/checkout would resolve for a checkout with no explicit
    `currency`/`provider` — see resolve_default_currency()'s own
    docstring (e.g. Mercado Pago registered and no USD-capable provider
    -> BRL). Falls back to CHECKOUT_CURRENCY (USD) when no registered
    provider is eligible for any of the plan's priced currencies (e.g.
    no providers configured at all, same as every pre-existing dev/test
    environment behaved before this resolution existed)."""
    prices: list[PlanPriceResponse] = []
    for plan in purchasable_plans():
        currency = resolve_default_currency(
            registry, catalog_currencies=currency_candidates(plan),
        ) or CHECKOUT_CURRENCY
        prices.append(PlanPriceResponse(plan=plan, amount=get_plan_price(plan, currency), currency=currency))
    return prices


@router.get(
    "/providers",
    response_model=PaymentProvidersResponse,
    summary="List payment providers actually usable right now",
)
def list_payment_providers(
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> PaymentProvidersResponse:
    """Empty in any environment without real payment credentials
    configured (the honest, expected answer today — see
    docs/CHECKOUT.md). The frontend uses this to decide whether to show
    a real checkout button or fall back to the existing manual/email
    upgrade flow."""
    return PaymentProvidersResponse(providers=registry.names())


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a plan-purchase checkout",
)
async def create_checkout(
    body: CreateCheckoutRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> CheckoutResponse:
    """Create a plan-purchase PaymentOrder — the entry point for
    'Usuário → Planos → Escolhe plano → Checkout'.

    Unlike POST /billing/orders, amount_fiat/credits_amount are NEVER
    accepted from the client — they are looked up server-side from
    src/billing/pricing.py, keyed by `plan`. expires_at is computed
    server-side too (settings.checkout_order_ttl_seconds from now).

    Idempotent exactly like POST /billing/orders: the SAME
    idempotency_key (the frontend generates one per checkout attempt and
    persists it, e.g. in sessionStorage, so a page refresh or a retried
    click never creates a second order) returns the existing order.

    404 if `plan` has no price in the catalog for the resolved currency
    (unknown plan, "free" — never a checkout target — or an unpriced
    currency; `currency` defaults to CHECKOUT_CURRENCY/USD).

    `provider` is optional (Payments Foundation / Payment Router etapas)
    — when omitted, the Payment Router (src/billing/payment_router.py)
    picks among every registered provider whose declared capabilities
    match `payment_method`/`payment_type`/the resolved `currency`,
    deterministically ordered by settings.payment_provider_priority (no
    more "ambiguous, specify `provider`" 400 — two-or-more eligible
    candidates just means the higher-priority one is used, with
    automatic, safety-gated failover to the next if it deterministically
    rejects the request — see that module's own docstring for the full
    failure-taxonomy/concurrency design). When `provider` IS given
    explicitly (LEGACY), dispatch is UNCHANGED from before the router
    existed: a registered adapter creates a real provider-side order
    with no failover (there is nothing to fail over between when the
    caller pinned one exact provider); otherwise the order is created
    locally only, pending — confirmable ONLY via the admin/test endpoint
    (POST /billing/orders/{id}/confirm), never silently auto-confirmed.
    See docs/CHECKOUT.md.

    Rate limited — same bucket as POST /billing/orders (mission
    requirement 11).
    """
    await _check_order_rate_limit(current_user)

    # B2: an explicit `currency` from the client is always honored
    # unchanged (an explicit override). Omitted -> resolve which of the
    # plan's priced currencies an eligible provider can actually accept
    # (Mercado Pago -> BRL, a USD-capable provider -> USD unchanged) —
    # see resolve_default_currency()'s own docstring. No eligible
    # provider for ANY priced currency (or none registered at all) falls
    # back to CHECKOUT_CURRENCY, the exact pre-existing default.
    if body.currency:
        currency = body.currency
    else:
        currency = resolve_default_currency(
            registry, catalog_currencies=currency_candidates(body.plan),
            provider=body.provider, payment_method=body.payment_method, payment_type=body.payment_type,
        ) or CHECKOUT_CURRENCY
    price = get_plan_price(body.plan, currency)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {body.plan!r} is not purchasable in {currency!r} (unknown plan, or no price for that currency).",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.checkout_order_ttl_seconds)
    payment_method = body.payment_method.value if body.payment_method else None
    payment_type = body.payment_type.value if body.payment_type else None

    # LEGACY explicit-provider path — UNCHANGED, no router, no failover.
    if body.provider is not None:
        provider_name = body.provider
        if registry.is_registered(provider_name):
            adapter = registry.get(provider_name)
            try:
                order, intent = await create_provider_order(
                    db, adapter,
                    user_id=current_user.id,  # always from JWT, never from body
                    amount_fiat=price,
                    currency_fiat=currency,
                    credits_amount=price,
                    currency_credits=currency,
                    idempotency_key=body.idempotency_key,
                    expires_at=expires_at,
                    plan=body.plan,
                    payment_method=payment_method,
                    payment_type=payment_type,
                )
            except ProviderUnavailable as exc:
                _provider_unavailable_total.inc(provider=provider_name)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Payment provider {provider_name!r} is temporarily unavailable",
                ) from exc
            except (OrderExpired, InvalidStatusTransition) as exc:
                # Hardening: a concurrent expire_order() (background
                # sweep) won the race for this order before
                # create_intent() was ever called.
                raise _map_service_error(exc) from exc
            _orders_created_total.inc(provider=provider_name)
            return CheckoutResponse(
                order=order,
                redirect_url=intent.redirect_url if intent else None,
                client_secret=intent.client_secret if intent else None,
            )

        svc = PaymentOrderService(db)
        order = svc.create_order(
            user_id=current_user.id,  # always from JWT, never from body
            provider=provider_name,
            amount_fiat=price,
            currency_fiat=currency,
            credits_amount=price,
            currency_credits=currency,
            idempotency_key=body.idempotency_key,
            expires_at=expires_at,
            plan=body.plan,
            payment_method=payment_method,
            payment_type=payment_type,
        )
        _orders_created_total.inc(provider=provider_name)
        return CheckoutResponse(order=order)

    # Auto-selection path — Payment Router.
    try:
        order, intent = await route_checkout(
            db, registry,
            user_id=current_user.id,  # always from JWT, never from body
            plan=body.plan,
            amount_fiat=price,
            currency_fiat=currency,
            credits_amount=price,
            currency_credits=currency,
            idempotency_key=body.idempotency_key,
            expires_at=expires_at,
            payment_method=body.payment_method,
            payment_type=body.payment_type,
        )
    except NoEligibleProvider as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No active payment provider supports the requested checkout "
                "(payment_method/payment_type/currency). Specify `provider` explicitly."
            ),
        ) from exc
    except AllProvidersRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Every eligible payment provider rejected this checkout. Please try again shortly.",
        ) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider is temporarily unavailable",
        ) from exc
    except (OrderExpired, InvalidStatusTransition) as exc:
        # Hardening: a concurrent expire_order() (background sweep) won
        # the race for this order before any provider was ever tried.
        raise _map_service_error(exc) from exc

    _orders_created_total.inc(provider=order.provider)
    return CheckoutResponse(
        order=order,
        redirect_url=intent.redirect_url if intent else None,
        client_secret=intent.client_secret if intent else None,
    )


@router.get(
    "/orders",
    response_model=PaymentOrderListResponse,
    summary="List user's payment orders",
)
def list_orders(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaymentOrderListResponse:
    """Return the authenticated user's payment orders, newest first.

    Use ?status=pending|confirmed|cancelled|… to filter by lifecycle state.
    """
    svc = PaymentOrderService(db)
    orders = svc.list_orders(current_user.id, status=status_filter, limit=limit, offset=offset)
    return PaymentOrderListResponse(orders=orders, total=len(orders))


@router.get(
    "/orders/{order_id}",
    response_model=PaymentOrderResponse,
    summary="Get a single payment order",
)
def get_order(
    order_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaymentOrderResponse:
    """Return a PaymentOrder that belongs to the authenticated user.

    Returns 404 if the order does not exist OR belongs to another user —
    we do not reveal that the order exists for a different owner.
    """
    svc = PaymentOrderService(db)
    order = svc.get_order(order_id, current_user.id)
    if order is None:
        raise _404()
    return order


@router.post(
    "/orders/{order_id}/confirm",
    response_model=PaymentOrderResponse,
    summary="Confirm a payment order",
)
def confirm_order(
    order_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> PaymentOrderResponse:
    """Confirm a pending/processing order and credit the user's balance atomically.

    All four writes (balance update, credit transaction, two ledger entries) plus
    the order status change commit in a single transaction — atomically or not at all.

    Idempotent: already-confirmed orders are returned unchanged. No double-credit.

    Evidence-of-payment guard
    -------------------------
    If a real adapter is registered for this order's provider (registry.is_registered),
    that provider proves payment via its own verified webhook — the client calling this
    endpoint directly is NOT evidence a payment occurred, only that the user clicked a
    button. Confirming here anyway would let any authenticated user credit themselves
    without ever paying. So this path is refused (409) for registered providers; only
    the webhook route (POST /billing/webhook/{provider}), which requires a verified
    provider signature, may confirm those orders. Orders whose provider has NO adapter
    registered (e.g. legacy/manual-flow orders, or in a test registry) are unaffected —
    this guard only starts applying to a provider the moment a real adapter for it is
    registered.
    """
    svc = PaymentOrderService(db)
    order = svc.get_order(order_id, current_user.id)
    if order is None:
        raise _404()
    if registry.is_registered(order.provider):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Provider {order.provider!r} confirms payment via verified webhook, "
                "not client-initiated confirmation."
            ),
        )
    # No adapter means no payment evidence of any kind. Unless the deployment
    # explicitly runs without payment providers, only an admin may do this.
    if not (settings.billing_client_confirm_enabled or current_user.is_admin):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This order has no payment provider to prove it was paid.",
        )
    try:
        order = svc.confirm_order(order_id, current_user.id)
    except OrderNotFound:
        raise _404()
    except Exception as exc:
        raise _map_service_error(exc)
    return order


@router.post(
    "/orders/{order_id}/cancel",
    response_model=PaymentOrderResponse,
    summary="Cancel a payment order",
)
def cancel_order(
    order_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaymentOrderResponse:
    """Cancel a pending or processing order. No financial operation is performed."""
    svc = PaymentOrderService(db)
    try:
        order = svc.cancel_order(order_id, current_user.id)
    except OrderNotFound:
        raise _404()
    except Exception as exc:
        raise _map_service_error(exc)
    return order


@router.post(
    "/orders/{order_id}/refund",
    response_model=PaymentOrderResponse,
    summary="Refund a confirmed payment order",
)
def refund_order(
    order_id: str,
    body: RefundOrderRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaymentOrderResponse:
    """Refund a confirmed order by debiting the user's credit balance atomically.

    All four writes (balance debit, credit transaction, two ledger entries) plus
    the order status change commit in a single transaction.

    amount must be ≤ available credits (credits_amount minus already-refunded).
    Full refund → status 'refunded'. Partial refund → status 'partial_refund'
    (non-terminal in Billing-2: further refunds from partial_refund are allowed).
    """
    svc = PaymentOrderService(db)
    try:
        order = svc.refund_order(
            order_id, current_user.id, body.amount,
            note=body.note, idempotency_key=body.idempotency_key,
        )
    except OrderNotFound:
        raise _404()
    except Exception as exc:
        raise _map_service_error(exc)
    return order


# ---------------------------------------------------------------------------
# Credit balance
# ---------------------------------------------------------------------------


@router.get(
    "/balance",
    response_model=UserCreditSummaryResponse,
    summary="Get all credit balances",
)
def credit_summary(
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserCreditSummaryResponse:
    """Return all credit balances for the authenticated user, one entry per currency.

    Returns an empty list if the user has no credit balances yet.
    """
    rows = (
        db.query(CreditBalance)
        .filter(CreditBalance.user_id == current_user.id)
        .order_by(CreditBalance.currency)
        .all()
    )
    balances = [
        CreditBalanceResponse(user_id=current_user.id, currency=r.currency, balance=r.balance)
        for r in rows
    ]
    return UserCreditSummaryResponse(user_id=current_user.id, balances=balances)


@router.get(
    "/balance/{currency}",
    response_model=CreditBalanceResponse,
    summary="Get credit balance for one currency",
)
def credit_balance(
    currency: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CreditBalanceResponse:
    """Return the credit balance for a specific currency.

    Returns 0.00 if the user has no balance for this currency yet.
    """
    svc = CreditService(db)
    balance = svc.balance(current_user.id, currency.upper())
    return CreditBalanceResponse(
        user_id=current_user.id,
        currency=currency.upper(),
        balance=balance,
    )


# ---------------------------------------------------------------------------
# Webhooks (Billing-2)
# ---------------------------------------------------------------------------


@router.post(
    "/webhook/{provider}",
    response_model=WebhookEventResponse,
    summary="Receive a payment provider webhook",
    # No authentication — provider calls this publicly.
    # Security is enforced via HMAC signature verification inside the handler.
)
async def receive_webhook(
    provider: str,
    request: Request,
    db: DBSession = Depends(get_db),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> WebhookEventResponse | JSONResponse:
    """Receive, verify, and route an inbound payment provider webhook.

    Response body
    -------------
    Every 200 return goes through _webhook_ack(), which returns TRINITY's
    own WebhookEventResponse shape UNLESS the resolved adapter overrides
    PaymentProvider.webhook_ack_body() (Binance Pay does — see that
    docstring) — declared response_model applies only to the former; a
    JSONResponse returned directly bypasses it, which FastAPI supports
    natively for exactly this per-provider-ack-shape case.

    Security design
    ---------------
    * A public, unauthenticated, generous per-IP rate limit guards this
      endpoint against raw volumetric abuse — see _check_webhook_rate_limit().
      No authenticated user exists here (the provider calls this
      publicly), so this is NOT the per-user bucket POST /billing/orders
      and /checkout share.
    * raw_body is read BEFORE any JSON parsing — required for HMAC verification.
    * Signature is read from the X-Signature header (provider-agnostic).
    * Unknown providers → 400 (not 404 — prevents provider enumeration).
    * Invalid signature → 400.
    * Replay protection (Production Payment Activation hardening): once
      an adapter supplies a `nonce` (WebhookEvent.nonce), a delivery
      whose (provider, nonce) was already recorded as fully processed is
      short-circuited BEFORE any order lookup or PaymentOrderService
      call — see src/services/webhook_replay_service.py's own docstring
      for the crash-safe ordering (nonce recorded only AFTER successful
      processing, never before).
    * Unknown event type → 200 (idempotent; caller should stop retrying).
    * Order not found by provider_ref → 200 (idempotent; no retry).
    * Provider mismatch (order.provider != path provider) → 400 (security).
    * Amount mismatch → 400 (security) — see step 6 below.

    HTTP 200 is returned for all non-error paths so the provider stops retrying.
    Business logic failures (order already confirmed, wrong state) are absorbed.

    Subscription-lifecycle events (Stripe Recurring etapa)
    -------------------------------------------------------
    ONE endpoint serves both vocabularies — no parallel webhook system.
    Immediately after resolving `adapter`, a cheap, UNVERIFIED peek
    (PaymentProvider.is_subscription_event() — see that method's own
    docstring for why reading the payload before verification is safe)
    decides whether THIS delivery belongs to the subscription-lifecycle
    vocabulary (SubscriptionEventKind) rather than the one-time payment
    vocabulary (WebhookEvent). If so, _handle_subscription_webhook()
    takes over: it calls verify_subscription_webhook() (the SAME
    Stripe-Signature mechanism, never a weaker one), reuses this SAME
    webhook_nonce replay table (keyed by the event's own event_id, same
    role as WebhookEvent.nonce), and dispatches to
    SubscriptionService.apply_subscription_webhook_event() — never
    PaymentOrderService. A provider that doesn't declare
    PaymentType.RECURRING never reaches this branch at all.
    """
    await _check_webhook_rate_limit(request)

    # 1. Read raw body BEFORE any Pydantic or JSON parsing.
    #    HMAC/signature verification requires the exact original bytes.
    raw_body: bytes = await request.body()
    # Full header set, not a single "X-Signature" — different providers need
    # different headers to verify (see PaymentProvider.verify_webhook's
    # docstring). Adapters read only what their scheme requires.
    headers: Mapping[str, str] = request.headers

    # 2. Resolve provider adapter — 400 for unknown providers.
    try:
        adapter = registry.get(provider)
    except ProviderNotFound:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown payment provider: {provider!r}",
        )

    # 2b. Subscription-lifecycle events (Stripe Recurring etapa) branch
    # off to a SEPARATE verify_*/consumer pair here — see this
    # function's own "Subscription-lifecycle events" docstring section
    # above. Gated on supported_payment_types first so a provider with
    # no recurring capability never even calls is_subscription_event().
    if PaymentType.RECURRING in adapter.supported_payment_types and adapter.is_subscription_event(raw_body):
        return await _handle_subscription_webhook(adapter, provider, raw_body, headers, db)

    # 3. Verify signature and parse event.
    try:
        event = await adapter.verify_webhook(raw_body, headers)
    except InvalidWebhookSignature:
        _webhook_invalid_signature_total.inc(provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook signature verification failed",
        )
    except UnknownWebhookEvent:
        logger.warning("webhook: unknown event type from provider=%s", provider)
        return _webhook_ack(adapter, provider=provider, event_type="unknown")
    except Exception:
        # Any OTHER adapter-raised failure (e.g. a body that passed
        # signature verification but is malformed/unparseable — an edge
        # case a real, correctly-signed delivery should never produce,
        # but never trust that assumption blindly) is rejected the same
        # way an invalid signature is: 400, no plan/credit ever touched.
        # Deliberately a broad `except Exception`, not a provider-specific
        # exception type (e.g. Binance Pay's own BinancePayError) — this
        # generic route must never import a specific adapter's exception
        # classes (see billing/providers/base.py's own "Core must never
        # depend on a specific payment SDK" principle); this stays correct
        # for any future provider's own malformed-input edge case too.
        logger.exception("webhook: adapter raised an unexpected error while parsing | provider=%s", provider)
        _webhook_invalid_signature_total.inc(provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload could not be verified",
        )

    # 3b. Replay check — a signature can be genuinely valid yet still be a
    #     captured-and-resent delivery. Checked as early as possible
    #     (before order lookup) — a replay is never worth any further
    #     work. See webhook_replay_service.py's own docstring for why
    #     this can NEVER falsely block a not-yet-processed payment.
    if event.nonce is not None and webhook_replay_service.was_nonce_processed(db, provider, event.nonce):
        return _webhook_ack(adapter, provider=provider, event_type=event.event_type)

    # 4. Look up the order by the provider's own reference ID.
    order: PaymentOrder | None = None
    if event.provider_ref:
        order = (
            db.query(PaymentOrder)
            .filter(PaymentOrder.external_order_id == event.provider_ref)
            .first()
        )

    if order is None:
        logger.warning(
            "webhook: no order found for provider_ref=%r provider=%s event_type=%s",
            event.provider_ref, provider, event.event_type,
        )
        return _webhook_ack(adapter, provider=provider, event_type=event.event_type)

    # 5. Security: path provider must match the order's recorded provider.
    #    Prevents cross-provider spoofing (e.g. a fake Binance webhook for a Stripe order).
    if order.provider != provider:
        logger.critical(
            "webhook: provider mismatch order.provider=%r path.provider=%r order_id=%s",
            order.provider, provider, order.id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provider mismatch between webhook path and order record",
        )

    # 6. Amount validation (mission requirement: a CONFIRMED event's own
    #    reported amount/currency must match what this order was actually
    #    created for) — checked BEFORE confirm_order() is ever called, so
    #    a mismatched webhook can never credit/activate anything. Only
    #    enforced when the provider's event actually carries an amount
    #    (WebhookEvent.amount is Optional — an adapter that cannot supply
    #    it is unaffected, same as every other pre-existing behavior
    #    here). Same security severity/style as the provider-mismatch
    #    check above (log critical, 400 — this is an anomaly worth
    #    investigating, not a routine idempotent no-op to silently
    #    absorb).
    if event.status == PaymentStatus.CONFIRMED and event.amount is not None:
        amount_mismatch = event.amount != order.amount_fiat
        currency_mismatch = event.currency is not None and event.currency.upper() != order.currency_fiat.upper()
        if amount_mismatch or currency_mismatch:
            logger.critical(
                "webhook: amount mismatch order=%s order.amount=%s %s event.amount=%s %s",
                order.id, order.amount_fiat, order.currency_fiat, event.amount, event.currency,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook amount does not match the order's recorded amount",
            )

    # 7. Route the event to the appropriate service method.
    svc = PaymentOrderService(db)

    if event.status == PaymentStatus.PROCESSING:
        try:
            svc.mark_processing(order.id, order.user_id, external_order_id=event.provider_ref)
        except (InvalidStatusTransition, OrderNotFound):
            pass  # Already in target state or beyond — idempotent.

    elif event.status == PaymentStatus.CONFIRMED:
        try:
            svc.confirm_order(order.id, order.user_id, webhook_raw=event.raw_payload)
            _orders_confirmed_total.inc(provider=provider)
        except OrderExpired:
            if order.plan is not None:
                _plan_activation_failed_total.inc(provider=provider)
        except (InvalidStatusTransition, DuplicateIdempotencyKey, OrderNotFound):
            pass  # Idempotent — already confirmed or expired.

    elif event.status == PaymentStatus.FAILED:
        try:
            svc.fail_order(order.id, order.user_id, webhook_raw=event.raw_payload)
            _orders_failed_total.inc(provider=provider)
            if order.plan is not None:
                _plan_activation_failed_total.inc(provider=provider)
        except (InvalidStatusTransition, OrderNotFound):
            pass  # Idempotent.

    else:
        logger.info(
            "webhook: unhandled event status=%r provider=%s event_type=%s order_id=%s",
            event.status, provider, event.event_type, order.id,
        )

    # 8. Record the nonce ONLY NOW — after the business logic above has
    #    already run to completion. See webhook_replay_service.py's own
    #    docstring: recording it any earlier could let a crash mid-
    #    processing wrongly suppress a legitimate, needed retry.
    if event.nonce is not None:
        webhook_replay_service.record_nonce(db, provider, event.nonce)

    db.refresh(order)
    return _webhook_ack(
        adapter, provider=provider, event_type=event.event_type,
        order_id=order.id, new_status=order.status,
    )


# ---------------------------------------------------------------------------
# Subscriptions (Stripe Recurring etapa)
# ---------------------------------------------------------------------------


async def _handle_subscription_webhook(
    adapter: PaymentProvider,
    provider: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    db: DBSession,
) -> WebhookEventResponse | JSONResponse:
    """The subscription-lifecycle counterpart of receive_webhook()'s
    steps 3-8 — called ONLY from receive_webhook() once
    PaymentProvider.is_subscription_event() has classified this
    delivery, never a second, independently-mounted route (see that
    function's own "Subscription-lifecycle events" docstring section).

    Same security posture as the one-time path: signature verified
    first (via verify_subscription_webhook() — the SAME Stripe-Signature
    mechanism), then replay-checked against the SAME webhook_nonce
    table (keyed by event_id, the subscription event's own equivalent
    of WebhookEvent.nonce), THEN dispatched to
    SubscriptionService.apply_subscription_webhook_event() — which
    itself applies the per-stream ordering guard (see that method's own
    docstring) before ever mutating a Subscription row.

    SubscriptionNotRenewable / InvalidSubscriptionTransition (a real
    event that is currently inapplicable — e.g. a renewal for an
    already-cancelled subscription) are absorbed exactly like the
    one-time path absorbs (InvalidStatusTransition, OrderNotFound):
    logged, 200 ack, never a retry signal.
    """
    try:
        event = await adapter.verify_subscription_webhook(raw_body, headers)
    except InvalidWebhookSignature:
        _webhook_invalid_signature_total.inc(provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook signature verification failed",
        )
    except UnknownWebhookEvent:
        logger.warning("subscription webhook: unknown event type from provider=%s", provider)
        return _webhook_ack(adapter, provider=provider, event_type="unknown")
    except Exception:
        # Same broad-catch rationale as receive_webhook()'s own step 3 —
        # never trust that a signature-verified payload is also
        # well-formed enough for the rest of parsing to succeed.
        logger.exception(
            "subscription webhook: adapter raised an unexpected error while parsing | provider=%s", provider,
        )
        _webhook_invalid_signature_total.inc(provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload could not be verified",
        )

    if event.event_id and webhook_replay_service.was_nonce_processed(db, provider, event.event_id):
        return _webhook_ack(adapter, provider=provider, event_type=event.kind.value)

    try:
        subscription = SubscriptionService(db).apply_subscription_webhook_event(event)
    except (SubscriptionNotRenewable, InvalidSubscriptionTransition) as exc:
        logger.warning(
            "subscription webhook: event could not be applied (inapplicable transition) | "
            "provider=%s kind=%s reason=%s",
            provider, event.kind.value, exc,
        )
        subscription = None

    if event.event_id:
        webhook_replay_service.record_nonce(db, provider, event.event_id)

    return _webhook_ack(
        adapter, provider=provider, event_type=event.kind.value,
        subscription_id=subscription.id if subscription is not None else None,
        new_status=subscription.status if subscription is not None else None,
    )


@router.post(
    "/subscriptions",
    response_model=CreateSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a recurring plan subscription",
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> CreateSubscriptionResponse:
    """Create a Subscription and its external (provider-side) mandate —
    the entry point for 'Usuário → Planos → Assinatura recorrente'.

    Like POST /billing/checkout, `amount` is NEVER accepted from the
    client — always looked up server-side from src/billing/pricing.py,
    keyed by `plan`. Unlike checkout, there is no legacy explicit-
    `provider` path: the ONLY selection mechanism is the Payment
    Router's own capability filter (eligible_providers(), reused
    UNCHANGED — no duplicated business logic), keyed on
    `payment_method` + PaymentType.RECURRING + the resolved `currency`.

    Two service calls compose the whole flow, no business logic
    duplicated here:
      1. SubscriptionService.create_subscription() — creates the LOCAL
         row (idempotent on `idempotency_key`).
      2. SubscriptionService.initiate_external_subscription() — calls
         route_subscription_creation() (Payment Router) to actually
         create the provider-side mandate (idempotent — a retry with
         the same idempotency_key never calls the provider twice).

    404 if `plan` has no price in the catalog for the resolved
    currency. 409 if no registered provider supports RECURRING for the
    requested payment_method/currency, or if every eligible one
    explicitly rejected creation. 503 if the provider was ambiguously
    unavailable (timeout/network) — safe to retry with the SAME
    idempotency_key.

    Rate limited — same bucket as POST /billing/orders and /checkout
    (mission requirement 11).
    """
    await _check_order_rate_limit(current_user)

    currency = (body.currency or CHECKOUT_CURRENCY).upper()
    price = get_plan_price(body.plan, currency)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {body.plan!r} is not purchasable in {currency!r} (unknown plan, or no price for that currency).",
        )

    candidates, excluded = eligible_providers(
        registry, payment_method=body.payment_method, payment_type=PaymentType.RECURRING, currency=currency,
    )
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No active payment provider supports recurring billing for the requested "
                "payment_method/currency."
            ),
        )

    svc = SubscriptionService(db)
    subscription = svc.create_subscription(
        user_id=current_user.id,  # always from JWT, never from body
        plan=body.plan,
        provider=candidates[0][0],
        payment_method=body.payment_method,
        currency=currency,
        amount=price,
        idempotency_key=body.idempotency_key,
    )

    try:
        subscription, intent = await svc.initiate_external_subscription(
            registry, subscription.id, idempotency_key=body.idempotency_key,
        )
    except SubscriptionNotFound:
        raise _404_subscription()
    except NoEligibleProvider as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active payment provider supports recurring billing for this subscription.",
        ) from exc
    except AllSubscriptionProvidersRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Every eligible payment provider rejected this subscription creation. Please try again shortly.",
        ) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider is temporarily unavailable",
        ) from exc

    return CreateSubscriptionResponse(subscription=subscription, redirect_url=intent.redirect_url)


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionResponse,
    summary="Get a single subscription",
)
def get_subscription(
    subscription_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SubscriptionResponse:
    """Return a Subscription that belongs to the authenticated user.

    Returns 404 if it does not exist OR belongs to another user — same
    no-ownership-leak convention as GET /billing/orders/{order_id}.
    """
    svc = SubscriptionService(db)
    subscription = svc.get_subscription(subscription_id, current_user.id)
    if subscription is None:
        raise _404_subscription()
    return subscription


@router.post(
    "/subscriptions/{subscription_id}/cancel",
    response_model=SubscriptionResponse,
    summary="Cancel a subscription",
)
async def cancel_subscription(
    subscription_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> SubscriptionResponse:
    """Cancel a subscription — asks the PROVIDER to actually stop
    billing (adapter.cancel_subscription()) BEFORE writing local
    status='cancelled'; see SubscriptionService.cancel_subscription()'s
    own docstring for the full safe-against-failure design (a
    503/409 here means the provider was NOT confirmed to have
    cancelled — the subscription is still billing, retry or contact
    the provider directly).
    """
    svc = SubscriptionService(db)
    try:
        subscription = await svc.cancel_subscription(registry, subscription_id, user_id=current_user.id)
    except SubscriptionNotFound:
        raise _404_subscription()
    except ProviderNotFound:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider is not currently registered; the subscription was NOT cancelled at the provider.",
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Payment provider is temporarily unavailable; cancellation was NOT confirmed. "
                "Please retry — the subscription is still active."
            ),
        ) from exc
    except PaymentCreationRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The subscription could not be cancelled right now. Please try again.",
        ) from exc
    except InvalidSubscriptionTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid status transition: '{exc.from_status}' → '{exc.to_status}'",
        ) from exc
    return subscription
