"""Find, and correct, accounts that hold a paid plan no payment backs.

Two defects left such accounts behind, and neither can be ruled out without
looking at the data:

* until 8f4417d every signup landed on START — the column default was
  "start" and create_user() set no plan;
* until 41eea2f any account could confirm its own unpaid plan order
  (POST /billing/orders/{id}/confirm with a provider that has no adapter).

Evidence that the CURRENT plan was paid for is one of:

* a confirmed PaymentOrder for that plan confirmed by a verified provider
  webhook (the webhook payload is recorded on the order). A confirmation the
  client made itself proves only that someone pressed a button;
* a Subscription for that plan that is active, past_due or in grace_period.

Never touched automatically: admins (operators assign their own plans), ids
passed in `keep_user_ids` (deliberate comps), and TEAM accounts that belong
to a team — members are assigned by an admin and never hold an order of their
own, so they are reported for review instead.

Nothing changes without --apply. Applying writes a report that is the audit
trail and the undo: --restore puts back every plan the report changed, but
only where the account is still on the plan the report set, so a purchase
made in between is never overwritten.

    python -m src.services.plan_reconciliation                       # report only
    python -m src.services.plan_reconciliation --apply --report out.json
    python -m src.services.plan_reconciliation --restore out.json

This is a data operation, not a migration: it depends on runtime evidence
(orders, subscriptions), and an Alembic revision cannot be added while the
uncommitted Presenton migrations 0030/0031 would give the chain two heads.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.models.payment_order import PaymentOrder
from src.models.subscription import Subscription, SubscriptionStatus
from src.models.usage import UsageRecord
from src.models.user import User

logger = logging.getLogger("trinity.plan_reconciliation")

PAID_PLANS: tuple[str, ...] = ("start", "pro", "max", "team")
TARGET_PLAN = "free"
BACKING_SUBSCRIPTION_STATUSES: tuple[str, ...] = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.PAST_DUE.value,
    SubscriptionStatus.GRACE_PERIOD.value,
)


@dataclass(frozen=True)
class Finding:
    user_id: str
    plan: str
    created_at: str
    #: Confirmed orders for this plan with no provider evidence — the
    #: self-confirmation defect, as opposed to the signup default.
    self_confirmed_orders: int
    usage_records: int
    spend_usd: float
    #: TEAM member: reported, never changed automatically.
    review_only: bool


def _has_payment_evidence(db: DBSession, user: User) -> bool:
    webhook_confirmed = (
        db.query(PaymentOrder.id)
        .filter(
            PaymentOrder.user_id == user.id,
            PaymentOrder.plan == user.plan,
            PaymentOrder.status == "confirmed",
            PaymentOrder.webhook_raw.isnot(None),
        )
        .first()
    )
    if webhook_confirmed is not None:
        return True
    subscription = (
        db.query(Subscription.id)
        .filter(
            Subscription.user_id == user.id,
            Subscription.plan == user.plan,
            Subscription.status.in_(BACKING_SUBSCRIPTION_STATUSES),
        )
        .first()
    )
    if subscription is not None:
        return True
    # TEAM by seat: an active seat in a paid team is the evidence for its plan.
    from src.services.team_service import seat_backs_plan

    return seat_backs_plan(db, user)


def _finding(db: DBSession, user: User) -> Finding:
    self_confirmed = (
        db.query(func.count(PaymentOrder.id))
        .filter(
            PaymentOrder.user_id == user.id,
            PaymentOrder.plan == user.plan,
            PaymentOrder.status == "confirmed",
            PaymentOrder.webhook_raw.is_(None),
        )
        .scalar()
    )
    usage_count, spend = (
        db.query(func.count(UsageRecord.id), func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
        .filter(UsageRecord.user_id == user.id, UsageRecord.status != "failed")
        .one()
    )
    created = user.created_at.isoformat() if getattr(user, "created_at", None) else ""
    return Finding(
        user_id=user.id,
        plan=user.plan,
        created_at=created,
        self_confirmed_orders=int(self_confirmed or 0),
        usage_records=int(usage_count or 0),
        spend_usd=round(float(spend or 0.0), 6),
        review_only=user.plan == "team" and bool(getattr(user, "team_id", None)),
    )


def find_unbacked_paid_accounts(db: DBSession, *, keep_user_ids: frozenset[str] = frozenset()) -> list[Finding]:
    """Every non-admin account on a paid plan with no evidence of payment."""
    users = (
        db.query(User)
        .filter(User.plan.in_(PAID_PLANS), User.is_admin.is_(False))
        .order_by(User.created_at)
        .all()
    )
    return [
        _finding(db, user)
        for user in users
        if user.id not in keep_user_ids and not _has_payment_evidence(db, user)
    ]


def summarize(findings: list[Finding]) -> dict:
    """Aggregates only — safe to print or paste anywhere."""
    by_plan: dict[str, int] = {}
    for f in findings:
        by_plan[f.plan] = by_plan.get(f.plan, 0) + 1
    return {
        "accounts": len(findings),
        "by_plan": by_plan,
        "to_change": sum(1 for f in findings if not f.review_only),
        "review_only_team_members": sum(1 for f in findings if f.review_only),
        "with_usage": sum(1 for f in findings if f.usage_records),
        "with_self_confirmed_orders": sum(1 for f in findings if f.self_confirmed_orders),
        "total_spend_usd": round(sum(f.spend_usd for f in findings), 6),
    }


def apply(db: DBSession, findings: list[Finding], *, now: datetime | None = None) -> dict:
    """Move every changeable finding to FREE, in one transaction.

    Each account is re-checked first — still on the reported plan, still
    without evidence — so running this twice, or after a purchase landed,
    changes nothing it should not. Returns the report (audit trail + undo).
    """
    now = now or datetime.now(timezone.utc)
    changes = []
    for finding in findings:
        if finding.review_only:
            continue
        user = db.query(User).filter(User.id == finding.user_id).first()
        if user is None or user.plan != finding.plan or user.is_admin or _has_payment_evidence(db, user):
            continue
        user.plan = TARGET_PLAN
        changes.append({"user_id": user.id, "from": finding.plan, "to": TARGET_PLAN})
    db.commit()
    for change in changes:
        logger.warning("plan_reconciliation: unbacked paid plan removed | user=%s from=%s to=%s",
                       change["user_id"], change["from"], change["to"])
    return {
        "generated_at": now.isoformat(),
        "reason": "paid plan without payment evidence",
        "summary": summarize(findings),
        "changes": changes,
        "review_only": [asdict(f) for f in findings if f.review_only],
    }


def restore(db: DBSession, report: dict) -> int:
    """Undo a report: every account still on the plan it set gets its previous plan back."""
    restored = 0
    for change in report.get("changes", []):
        user = db.query(User).filter(User.id == change["user_id"]).first()
        if user is not None and user.plan == change["to"]:
            user.plan = change["from"]
            restored += 1
    db.commit()
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="change the plans (default: report only)")
    parser.add_argument("--report", help="where --apply writes its audit/undo report (required with --apply)")
    parser.add_argument("--restore", help="undo a previous --apply using its report")
    parser.add_argument("--keep", action="append", default=[], help="user id to leave untouched (repeatable)")
    args = parser.parse_args(argv)
    if args.apply and not args.report:
        parser.error("--apply needs --report so the change can be audited and undone")

    from src.database import SessionLocal

    db = SessionLocal()
    try:
        if args.restore:
            with open(args.restore, encoding="utf-8") as fh:
                print(json.dumps({"restored": restore(db, json.load(fh))}))
            return 0
        findings = find_unbacked_paid_accounts(db, keep_user_ids=frozenset(args.keep))
        if not args.apply:
            print(json.dumps({"dry_run": True, **summarize(findings)}, indent=2))
            return 0
        report = apply(db, findings)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(json.dumps({"applied": len(report["changes"]), **report["summary"]}, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
