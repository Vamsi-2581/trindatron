"""Continuity entitlement + financial enforcement — Etapa 5F (closing the
two gaps flagged in the first 5F round: plan eligibility, and what funds a
Continuity execution).

── The two controls, kept deliberately separate ────────────────────────────
  A) PREMIUM QUOTA — unchanged, entirely owned by
     src/services/entitlement_service.py (check_and_reserve/finalize_usage/
     cancel_reservation, UsageRecord). This module never reads or writes
     UsageRecord and never calls check_and_reserve() — calling it again
     here would just re-raise the same "exhausted" rejection that is the
     very reason Continuity is being considered at all.

  B) CONTINUITY BUDGET — new in this module. A separate per-plan daily TCU
     ceiling (PlanEntitlement.continuity_daily_tcu_limit,
     src/entitlements.py), tracked independently by summing this user's
     own CreditTransaction history for TODAY (reference_type="continuity",
     currency="TCU") — not by touching UsageRecord at all.

Exhausted premium quota is NEVER treated as unlimited access to (B) — both
controls, plus the user's actual financial balance, must independently
allow an execution (Etapa 5F correction, section 10: "não confundir quota
com saldo").

── Financial mechanism: the EXISTING ledger, not a new one ────────────────
Every Continuity debit/refund goes through
`BillingOrchestrator.debit_and_record()` / `.credit_and_record()`
(src/services/billing_orchestrator.py) — the SAME atomic
CreditService+LedgerService orchestration already used for every other
credit movement in this codebase. No new currency, no new ledger table, no
new PaymentOrder-like construct. `currency="TCU"` was already a documented
supported currency in credit_service.py before this etapa; this is the
first code path to actually use it. `ledger_entry_type="ai_cost"` and
`account_credit="ai_cost"` reuse the account convention already documented
in src/models/ledger.py's LedgerEntry docstring ("ai_cost — cost paid to
AI providers") — not invented here.

CreditService/BillingOrchestrator invariants relied on, unmodified:
  - CreditBalance.balance can never go negative (DB CHECK constraint +
    InsufficientCreditError raised before any write).
  - Idempotency: every CreditTransaction/LedgerEntry requires a caller-
    supplied idempotency_key; a retried call with the same key returns
    the original record rather than double-charging.
  - Atomicity: debit_and_record()/credit_and_record() flush the balance
    update AND the paired LedgerEntry inside ONE transaction, commit once;
    any exception rolls back both together (BillingOrchestrator's own
    documented guarantee — never touched here).
  - Optimistic locking on CreditBalance.version protects concurrent
    debits for the SAME (user, currency) from corrupting the balance.

Concurrency for the BUDGET check specifically (not the balance, which
CreditService already protects) uses the exact same pg_advisory_xact_lock
pattern entitlement_service.check_and_reserve() already established and
documents (including its known, accepted write-skew window on non-
PostgreSQL backends) — keyed distinctly ("continuity_tcu_{user_id}", vs
check_and_reserve()'s "tcu_{user_id}") so the two dimensions never
serialize against each other unnecessarily.

Does NOT touch: PaymentOrder, refund_and_record(), Billing webhook/
provider code, entitlement_service.py, or any premium-quota semantics.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, text
from sqlalchemy.orm import Session as DBSession

from src.entitlements import get_plan_entitlement
from src.models.ledger import CreditTransaction
from src.observability.metrics import core_metrics
from src.services.billing_orchestrator import BillingOrchestrator, BillingResult
from src.services.credit_service import InsufficientCreditError

logger = logging.getLogger("trinity.continuity_entitlement")

# Etapa 5G — additive alongside the existing logger calls below; does not
# change any authorization/financial decision. Values observed are always
# the same real numbers already computed by the code around each call —
# never invented (see this module's own docstring on "não invente valores").
_debits_total = core_metrics.counter(
    "continuity_debits_total", "check_and_debit_continuity() outcomes, labeled by plan/outcome.",
)
_tcu_debited = core_metrics.histogram(
    "continuity_tcu_debited", "Real TCU debited per successful Continuity execution, labeled by plan.",
)
_tcu_refunded = core_metrics.histogram(
    "continuity_tcu_true_up", "true_up_continuity() adjustment amount, labeled by direction (refund|extra_charge).",
)

TCU_CURRENCY = "TCU"
_CONTINUITY_REFERENCE_TYPE = "continuity"

__all__ = [
    "TCU_CURRENCY",
    "ContinuityNotEnabledError",
    "ContinuityBudgetExceededError",
    "check_and_debit_continuity",
    "true_up_continuity",
    "continuity_used_today",
]


class ContinuityNotEnabledError(Exception):
    """The plan does not have Continuity Mode enabled (Etapa 5F product
    decision: free plan only, today — see src/entitlements.py)."""

    def __init__(self, plan: str) -> None:
        super().__init__(f"Continuity Mode is not enabled for plan {plan!r}")
        self.plan = plan


class ContinuityBudgetExceededError(Exception):
    """Continuity's OWN daily TCU budget is exhausted — distinct from
    (and never satisfied merely by) the premium quota already being
    exhausted, which is what triggered this check in the first place."""

    def __init__(self, used: float, limit: float) -> None:
        super().__init__(f"Continuity daily budget exhausted: used={used:.4f}, limit={limit:.4f}")
        self.used = used
        self.limit = limit


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_window() -> tuple[datetime, datetime]:
    now = _utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def continuity_used_today(db: DBSession, user_id: str) -> float:
    """Sum of this user's Continuity TCU consumption today — read-only,
    used both by the authorization gate below and available for a future
    /users/me/usage-style display endpoint."""
    day_start, day_end = _day_window()
    result = (
        db.query(func.coalesce(func.sum(CreditTransaction.amount), 0))
        .filter(
            CreditTransaction.user_id == user_id,
            CreditTransaction.currency == TCU_CURRENCY,
            CreditTransaction.reference_type == _CONTINUITY_REFERENCE_TYPE,
            CreditTransaction.transaction_type == "consume",
            CreditTransaction.created_at >= day_start,
            CreditTransaction.created_at < day_end,
        )
        .scalar()
    )
    return float(result)


def check_and_debit_continuity(
    db: DBSession,
    *,
    user_id: str,
    plan: str,
    estimated_tcu: float,
    execution_id: str,
) -> BillingResult:
    """Authorize AND finance one Continuity Mode execution, atomically.

    Two independent gates, both must pass — see this module's docstring:
      1. PRODUCT ENTITLEMENT: plan.continuity_enabled, and Continuity's
         own daily budget has room for `estimated_tcu`.
      2. FINANCIAL BALANCE: the user's real CreditBalance("TCU") can
         cover `estimated_tcu` — enforced by BillingOrchestrator/
         CreditService, which never lets the balance go negative.

    Callers MUST have already confirmed (before calling this — see
    continuity_mode.py) that the request is TEXT-ONLY and that an
    eligible economic model exists: this function performs the FINANCIAL
    commitment and must be the last gate, never the first, so a rejected-
    for-other-reasons request never touches the ledger at all.

    Raises ContinuityNotEnabledError, ContinuityBudgetExceededError, or
    InsufficientCreditError — none of these leave any row behind (the
    budget check is read-only; the plan check is pure; the financial
    debit only ever partially executes inside BillingOrchestrator's own
    atomic transaction, which rolls back completely on
    InsufficientCreditError).
    """
    entitlement = get_plan_entitlement(plan)

    if not entitlement.continuity_enabled:
        _debits_total.inc(plan=plan, outcome="not_enabled")
        raise ContinuityNotEnabledError(plan)

    # Advisory lock: closes the same write-skew window
    # entitlement_service.check_and_reserve() already documents and
    # accepts for premium quota — applied here for Continuity's own
    # budget dimension, under a distinct lock key so the two never
    # serialize against each other. Silently skipped on SQLite/tests,
    # identical fallback behavior to check_and_reserve().
    try:
        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"continuity_tcu_{user_id}"},
            )
    except Exception:
        logger.debug("Continuity advisory lock unavailable, continuing without it | user=%s", user_id)

    used_today = continuity_used_today(db, user_id)
    if used_today + estimated_tcu > entitlement.continuity_daily_tcu_limit:
        logger.info(
            "continuity budget exceeded | user=%s plan=%s used=%.4f estimated=%.4f limit=%.4f",
            user_id, plan, used_today, estimated_tcu, entitlement.continuity_daily_tcu_limit,
        )
        _debits_total.inc(plan=plan, outcome="budget_exceeded")
        raise ContinuityBudgetExceededError(used_today, entitlement.continuity_daily_tcu_limit)

    orchestrator = BillingOrchestrator(db)
    try:
        result = orchestrator.debit_and_record(
            user_id=user_id,
            currency=TCU_CURRENCY,
            amount=Decimal(str(estimated_tcu)),
            transaction_type="consume",
            ledger_entry_type="ai_cost",
            account_debit=f"user:{user_id}",
            account_credit="ai_cost",
            credit_idempotency_key=f"continuity:{execution_id}",
            ledger_idempotency_key=f"continuity:{execution_id}:ledger",
            reference_id=execution_id,
            reference_type=_CONTINUITY_REFERENCE_TYPE,
            note="Continuity Mode execution",
        )
    except InsufficientCreditError:
        _debits_total.inc(plan=plan, outcome="insufficient_balance")
        raise
    logger.info(
        "continuity debited | user=%s plan=%s amount=%.4f execution_id=%s tx_id=%s",
        user_id, plan, estimated_tcu, execution_id, result.transaction.id,
    )
    _debits_total.inc(plan=plan, outcome="debited")
    _tcu_debited.observe(estimated_tcu, plan=plan)
    return result


def true_up_continuity(
    db: DBSession,
    *,
    user_id: str,
    estimated_tcu: float,
    actual_tcu: float,
    execution_id: str,
) -> None:
    """Reconcile the conservative estimate debited by
    check_and_debit_continuity() against real usage, once known.

    actual < estimated (the common case — Continuity's economic model is
    bounded by a conservative max-output-tokens estimate): refund the
    difference via the ledger's own 'refund' transaction/entry type
    (CreditService.refund()'s own documented use — "payment reversal,
    failed charge, etc." — an over-estimated charge is exactly this
    category). This is entirely independent of PaymentOrder/
    refund_and_record(), which govern actual payment reversals, not
    ledger-internal estimate corrections.

    actual > estimated (rare): an additional best-effort debit for the
    difference. If the user's balance cannot cover it,
    InsufficientCreditError is caught and logged, never re-raised — the
    Continuity response was already delivered; Continuity's own daily
    budget check already bounded the worst case at authorization time,
    and there is no way to retroactively un-deliver a response.
    """
    diff = round(actual_tcu - estimated_tcu, 8)
    if diff == 0:
        return

    orchestrator = BillingOrchestrator(db)
    if diff < 0:
        orchestrator.credit_and_record(
            user_id=user_id, currency=TCU_CURRENCY, amount=Decimal(str(-diff)),
            transaction_type="refund", ledger_entry_type="refund",
            account_debit="ai_cost", account_credit=f"user:{user_id}",
            credit_idempotency_key=f"continuity:{execution_id}:trueup",
            ledger_idempotency_key=f"continuity:{execution_id}:trueup:ledger",
            reference_id=execution_id, reference_type=_CONTINUITY_REFERENCE_TYPE,
            note="Continuity Mode true-up refund",
        )
        _tcu_refunded.observe(-diff, direction="refund")
        return

    try:
        orchestrator.debit_and_record(
            user_id=user_id, currency=TCU_CURRENCY, amount=Decimal(str(diff)),
            transaction_type="consume", ledger_entry_type="ai_cost",
            account_debit=f"user:{user_id}", account_credit="ai_cost",
            credit_idempotency_key=f"continuity:{execution_id}:trueup",
            ledger_idempotency_key=f"continuity:{execution_id}:trueup:ledger",
            reference_id=execution_id, reference_type=_CONTINUITY_REFERENCE_TYPE,
            note="Continuity Mode true-up additional charge",
        )
        _tcu_refunded.observe(diff, direction="extra_charge")
    except InsufficientCreditError:
        _debits_total.inc(plan="", outcome="true_up_insufficient_balance")
        logger.warning(
            "continuity true-up additional charge failed (insufficient balance), not retried | "
            "user=%s execution_id=%s diff=%.8f",
            user_id, execution_id, diff,
        )
