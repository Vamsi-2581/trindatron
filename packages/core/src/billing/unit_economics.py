"""Unit economics of TRINDATRON, worst case first.

economics.py derives what a price can afford at the Stripe US card rate and
holds a 45% gross margin at the ceiling. This module is the fuller, stricter
model the 2026-09-13 economic audit asked for:

  * the WORST fee any reachable payment processor charges, not the cheapest;
  * infrastructure, fixed and per account, with the points where a service
    changes tier;
  * an allowance for provider spend that can still escape TCU;
  * FREE as a population bounded by its plan-wide daily budget;
  * TEAM as the flat plan it is today, and as the seat model proposed to
    replace it;
  * scale and plan-mix scenarios at 10-100% utilisation;
  * what overage credits would earn at a given price.

Every figure is either read from the live tables (prices, entitlements,
catalog) or is a sourced input below with the date it was read. Inputs that
are assumptions — how many requests an active account makes, how much data
it leaves behind — are named as assumptions, so anyone can change one and
rerun:

    uv run python -m src.billing.unit_economics
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.billing import economics as ec
from src.billing import team_pricing
from src.billing.pricing import PLAN_PRICES
from src.entitlements import PLAN_ENTITLEMENTS

USD_PER_TCU: float = ec.USD_PER_TCU
WEEKS_PER_MONTH: float = ec.WEEKS_PER_MONTH
DAYS_PER_MONTH: float = 365.25 / 12
SECONDS_PER_MONTH: float = DAYS_PER_MONTH * 24 * 3600

PLANS: tuple[str, ...] = ("free", "start", "pro", "max", "team")
PAID_PLANS: tuple[str, ...] = ("start", "pro", "max", "team")


# ── Payment processors (read 2026-09-13) ─────────────────────────────────


@dataclass(frozen=True)
class FeeProfile:
    name: str
    percent: float
    fixed_usd: float
    source: str

    def fee_usd(self, amount_usd: float) -> float:
        return amount_usd * self.percent + self.fixed_usd if amount_usd > 0 else 0.0


STRIPE_US_CARD = FeeProfile(
    "stripe-us-card+billing", 0.029 + 0.007, 0.30,
    "2.9% + $0.30 per card charge (the rate economics.py uses); Stripe Billing "
    "0.7% of recurring volume. The US page could not be read from this region.",
)
STRIPE_BR_INTERNATIONAL_USD = FeeProfile(
    "stripe-br-international-usd+billing", 0.0399 + 0.02 + 0.02 + 0.007, 0.30,
    "stripe.com/br/pricing: 3.99% + R$0.39 domestic card, +2% international "
    "card, currency conversion from 2%, Billing 0.7%. The fixed part is "
    "modelled at $0.30, above R$0.39, to stay on the safe side.",
)
NOWPAYMENTS_TO_FIAT = FeeProfile(
    "nowpayments-settled-to-fiat", 0.01 + 0.023, 0.0,
    "NOWPayments help center: 1% multi-currency (0.5% mono), conversion to "
    "fiat 1.5-2.3%.",
)
BINANCE_PAY = FeeProfile(
    "binance-pay", 0.01, 0.0,
    "No merchant rate published for this region (Binance Payouts: 0.8%). 1% "
    "assumed.",
)
#: Mercado Pago is not listed: plans are priced in USD only and the Mercado
#: Pago adapter accepts ARS/BRL/CLP/COP/MXN/PEN/UYU, so it cannot sell a plan
#: today. Its Brazilian credit-card rate (4.98% on same-day settlement) would
#: sit below the Stripe profile above if that changed.
PROCESSORS: tuple[FeeProfile, ...] = (
    STRIPE_US_CARD, STRIPE_BR_INTERNATIONAL_USD, NOWPAYMENTS_TO_FIAT, BINANCE_PAY,
)


def worst_case_fee(amount_usd: float) -> FeeProfile:
    """The processor that takes the most from this ticket."""
    return max(PROCESSORS, key=lambda p: p.fee_usd(amount_usd))


# ── Margins and allowances ───────────────────────────────────────────────

#: Provider spend that can still escape TCU after the 2026-09-13 fixes
#: (quality-rejected answers, long-context tiers and the CODE/WORK loops are
#: now metered): a failed attempt a provider bills anyway, token rounding.
#: 5% is deliberately larger than anything observed.
UNMETERED_SPEND_ALLOWANCE: float = 0.05

#: What a paid plan must still contribute, as a share of net revenue after
#: the worst fee, when a customer consumes every unit the plan allows. It pays
#: for the fixed costs below and for the fact that this is a business.
MIN_CONTRIBUTION_AT_CEILING: float = 0.20


# ── Infrastructure (prices read 2026-09-13) ──────────────────────────────

RAILWAY_PRO_PLAN_USD = 20.0            # railway.com/pricing: Pro $20, includes $20 of usage
RAILWAY_VCPU_MONTH_USD = 0.00000772 * SECONDS_PER_MONTH      # $0.00000772 per vCPU-second
RAILWAY_GB_RAM_MONTH_USD = 0.00000386 * SECONDS_PER_MONTH    # $0.00000386 per GB-second
RAILWAY_VOLUME_GB_MONTH_USD = 0.15
RAILWAY_EGRESS_GB_USD = 0.05
VERCEL_PRO_SEAT_USD = 20.0             # vercel.com/pricing; Hobby is non-commercial only
VERCEL_INCLUDED_TRANSFER_GB = 1_000.0
VERCEL_TRANSFER_OVERAGE_GB_USD = 0.15
R2_FREE_STORAGE_GB = 10.0              # developers.cloudflare.com/r2/pricing
R2_STORAGE_GB_MONTH_USD = 0.015
ELEVENLABS_STARTER_USD = 6.0           # elevenlabs.io/pricing/api: STT at $0.22/h needs Starter
DOMAIN_MONTHLY_USD = 15.0 / 12         # assumption: one .com renewal a year

# Assumptions about load — named, not measured. Production usage data was not
# available to this audit; change them and rerun.
CORE_REPLICA_VCPU = 0.5
CORE_REPLICA_GB = 1.0
ACTIVE_ACCOUNTS_PER_CORE_REPLICA = 1_500
POSTGRES_VCPU = 0.5
POSTGRES_GB = 1.0
ACTIVE_ACCOUNTS_PER_POSTGRES_STEP = 5_000
#: Needed from the second core replica on: Request Control's rate and
#: concurrency limits are in-process memory (see request_control.py).
REDIS_MONTHLY_USD = 8.0
STORAGE_GB_PER_ACTIVE_ACCOUNT = 0.02
TRANSFER_GB_PER_REQUEST = 8.0 / 1_000_000     # ~8 KB of streamed text and JSON
VERCEL_GB_PER_ACTIVE_ACCOUNT = 0.005
REQUESTS_PER_ACTIVE_ACCOUNT_MONTH = {"free": 60, "start": 400, "pro": 1_200, "max": 3_000, "team": 3_000}
#: Safety factor on the per-account share of infrastructure.
INFRA_SAFETY_FACTOR = 2.0


def infrastructure_month(active_by_plan: dict[str, int]) -> dict[str, float]:
    """Monthly infrastructure for a population of active accounts.

    TEAM counts members, not subscriptions: every member is an account that
    makes requests and stores data.
    """
    active = sum(active_by_plan.values())
    requests = sum(n * REQUESTS_PER_ACTIVE_ACCOUNT_MONTH[p] for p, n in active_by_plan.items())

    replicas = max(1, math.ceil(active / ACTIVE_ACCOUNTS_PER_CORE_REPLICA))
    core = replicas * (CORE_REPLICA_VCPU * RAILWAY_VCPU_MONTH_USD + CORE_REPLICA_GB * RAILWAY_GB_RAM_MONTH_USD)
    postgres_steps = max(1, math.ceil(active / ACTIVE_ACCOUNTS_PER_POSTGRES_STEP))
    postgres = postgres_steps * (POSTGRES_VCPU * RAILWAY_VCPU_MONTH_USD + POSTGRES_GB * RAILWAY_GB_RAM_MONTH_USD)
    storage = active * STORAGE_GB_PER_ACTIVE_ACCOUNT * RAILWAY_VOLUME_GB_MONTH_USD
    redis = REDIS_MONTHLY_USD if replicas > 1 else 0.0
    egress = requests * TRANSFER_GB_PER_REQUEST * RAILWAY_EGRESS_GB_USD
    railway_usage = core + postgres + storage + redis + egress
    railway = RAILWAY_PRO_PLAN_USD + max(0.0, railway_usage - RAILWAY_PRO_PLAN_USD)

    vercel_gb = active * VERCEL_GB_PER_ACTIVE_ACCOUNT
    vercel = VERCEL_PRO_SEAT_USD + max(0.0, vercel_gb - VERCEL_INCLUDED_TRANSFER_GB) * VERCEL_TRANSFER_OVERAGE_GB_USD

    items = {
        "railway": railway,
        "vercel": vercel,
        "elevenlabs": ELEVENLABS_STARTER_USD,
        "domain": DOMAIN_MONTHLY_USD,
        "r2": 0.0,
        "email": 0.0,
    }
    items["total"] = sum(items.values())
    items["core_replicas"] = float(replicas)
    items["postgres_steps"] = float(postgres_steps)
    items["redis_required"] = 1.0 if redis else 0.0
    return items


def variable_infra_per_account_usd(plan: str) -> float:
    """One more active account's share of infrastructure, with a safety factor."""
    compute = (CORE_REPLICA_VCPU * RAILWAY_VCPU_MONTH_USD + CORE_REPLICA_GB * RAILWAY_GB_RAM_MONTH_USD) \
        / ACTIVE_ACCOUNTS_PER_CORE_REPLICA
    database = (POSTGRES_VCPU * RAILWAY_VCPU_MONTH_USD + POSTGRES_GB * RAILWAY_GB_RAM_MONTH_USD) \
        / ACTIVE_ACCOUNTS_PER_POSTGRES_STEP
    storage = STORAGE_GB_PER_ACTIVE_ACCOUNT * RAILWAY_VOLUME_GB_MONTH_USD
    egress = REQUESTS_PER_ACTIVE_ACCOUNT_MONTH[plan] * TRANSFER_GB_PER_REQUEST * RAILWAY_EGRESS_GB_USD
    return (compute + database + storage + egress) * INFRA_SAFETY_FACTOR


# ── AI cost and plan unit economics ──────────────────────────────────────


def price_usd(plan: str) -> float:
    if plan == "team":
        # Flat TEAM is no longer sold; its last price is kept for the comparison below.
        return float(team_pricing.LEGACY_FLAT_TEAM_USD)
    prices = PLAN_PRICES.get(plan)
    return float(prices["USD"]) if prices else 0.0


def binding_weekly_tcu(plan: str) -> float:
    """The weekly allowance that caps what a SUBSCRIPTION can cost — the
    shared pool on TEAM, the account's own window elsewhere."""
    e = PLAN_ENTITLEMENTS[plan]
    return float(e.team_weekly_pool_tcu_limit or e.weekly_tcu_limit)


def ai_cost_month_usd(plan: str, utilization: float) -> float:
    """Provider spend for one subscription using `utilization` of its weekly
    allowance every week, including the unmetered allowance."""
    return binding_weekly_tcu(plan) * utilization * WEEKS_PER_MONTH * USD_PER_TCU \
        * (1.0 + UNMETERED_SPEND_ALLOWANCE)


@dataclass(frozen=True)
class PlanMonth:
    plan: str
    utilization: float
    members: int
    revenue: float
    payment_fee: float
    net: float
    ai: float
    infra: float
    contribution: float

    @property
    def margin(self) -> float | None:
        return self.contribution / self.net if self.net > 0 else None


def plan_month(plan: str, utilization: float, *, members: int = 1,
               fee: FeeProfile | None = None) -> PlanMonth:
    price = price_usd(plan)
    profile = fee or worst_case_fee(price)
    payment_fee = profile.fee_usd(price)
    net = price - payment_fee
    ai = ai_cost_month_usd(plan, utilization)
    infra = variable_infra_per_account_usd(plan) * members
    return PlanMonth(plan, utilization, members, price, payment_fee, net, ai, infra, net - ai - infra)


def safe_weekly_ceiling_tcu(plan: str, *, members: int = 1) -> float:
    """The largest weekly allowance that still leaves MIN_CONTRIBUTION_AT_CEILING
    of net revenue at the worst fee, after infrastructure and the unmetered
    allowance."""
    price = price_usd(plan)
    net = price - worst_case_fee(price).fee_usd(price)
    ai_budget = net * (1.0 - MIN_CONTRIBUTION_AT_CEILING) - variable_infra_per_account_usd(plan) * members
    return max(0.0, ai_budget) / (1.0 + UNMETERED_SPEND_ALLOWANCE) / WEEKS_PER_MONTH / USD_PER_TCU


def free_population_ai_month_usd(accounts: int, utilization: float) -> float:
    """FREE spend for a population, bounded by the plan-wide daily budget."""
    from src.config import Settings

    per_account = ai_cost_month_usd("free", utilization)
    budget_day = Settings.model_fields["free_plan_global_daily_budget_usd"].default
    uncapped = accounts * per_account
    if budget_day is None:
        return uncapped
    return min(uncapped, budget_day * DAYS_PER_MONTH)


# ── Scale and plan mix ───────────────────────────────────────────────────

#: Shares of SUBSCRIPTIONS. A TEAM subscription brings TEAM_MEMBERS_ASSUMED accounts.
MIXES: dict[str, dict[str, float]] = {
    "conservative": {"free": 0.90, "start": 0.06, "pro": 0.03, "max": 0.008, "team": 0.002},
    "base": {"free": 0.80, "start": 0.10, "pro": 0.07, "max": 0.025, "team": 0.005},
    "optimistic": {"free": 0.70, "start": 0.12, "pro": 0.12, "max": 0.05, "team": 0.01},
    "free_heavy": {"free": 0.97, "start": 0.02, "pro": 0.01, "max": 0.0, "team": 0.0},
    "pro_centric": {"free": 0.60, "start": 0.10, "pro": 0.25, "max": 0.04, "team": 0.01},
}
TEAM_MEMBERS_ASSUMED = 5
SCALES: tuple[int, ...] = (10, 50, 100, 500, 1_000, 5_000, 10_000)
UTILIZATIONS: tuple[float, ...] = (0.10, 0.20, 0.50, 0.75, 1.00)


@dataclass(frozen=True)
class ScaleMonth:
    subscriptions: int
    mix: str
    utilization: float
    revenue: float
    payment_fees: float
    ai: float
    infra: float

    @property
    def result(self) -> float:
        return self.revenue - self.payment_fees - self.ai - self.infra

    @property
    def margin(self) -> float | None:
        return self.result / self.revenue if self.revenue > 0 else None


def simulate(subscriptions: int, mix: str, utilization: float) -> ScaleMonth:
    shares = MIXES[mix]
    counts = {p: round(subscriptions * s) for p, s in shares.items()}
    revenue = sum(counts[p] * price_usd(p) for p in PAID_PLANS)
    fees = sum(counts[p] * worst_case_fee(price_usd(p)).fee_usd(price_usd(p)) for p in PAID_PLANS)
    ai = sum(counts[p] * ai_cost_month_usd(p, utilization) for p in PAID_PLANS) \
        + free_population_ai_month_usd(counts["free"], utilization)
    active = dict(counts)
    active["team"] = counts["team"] * TEAM_MEMBERS_ASSUMED
    infra = infrastructure_month(active)["total"]
    return ScaleMonth(subscriptions, mix, utilization, revenue, fees, ai, infra)


def break_even_subscriptions(mix: str, utilization: float) -> int | None:
    """Smallest population whose month is not a loss, or None within 1M."""
    lo, hi = 1, 1_000_000
    if simulate(hi, mix, utilization).result < 0:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if simulate(mid, mix, utilization).result >= 0:
            hi = mid
        else:
            lo = mid + 1
    return lo


# ── TEAM: flat plan with a pool vs seats ─────────────────────────────────

#: Published per-seat prices (USD/month) of the products TEAM is compared with,
#: (billed annually, billed monthly). Read 2026-09-13; benchmarks, not a model.
MARKET_SEATS_USD = {
    "claude_team_standard": (20.0, 25.0),
    "claude_team_premium": (100.0, 125.0),
    "chatgpt_business_standard": (20.0, 25.0),
    "chatgpt_business_premium": (100.0, 125.0),
}
#: A seat's entitlement is an existing plan's: Standard = PRO (standard band),
#: Premium = MAX (premium band, Opus). No new quota is invented.
SEAT_ENTITLEMENT = dict(team_pricing.SEAT_PLAN)
#: The seat prices, read from the one catalog (src/billing/team_pricing.py).
#: Each sits above seat_price_floor_usd() and below the monthly market price.
PROPOSED_SEAT_USD = {seat: float(price) for seat, price in team_pricing.SEAT_PRICES_USD.items()}
TEAM_SEAT_COUNTS: tuple[int, ...] = (1, 2, 3, 5, 10, 20, 50, 100, 150)
TEAM_PREMIUM_SHARES: tuple[float, ...] = (0.0, 0.2, 0.4, 0.5, 0.8, 1.0)


def seat_price_floor_usd(seat: str) -> float:
    """Lowest monthly seat price that leaves MIN_CONTRIBUTION_AT_CEILING at
    the worst fee when the seat uses every unit of its entitlement."""
    plan = SEAT_ENTITLEMENT[seat]
    ai = ai_cost_month_usd(plan, 1.0)
    infra = variable_infra_per_account_usd(plan)
    fee = STRIPE_BR_INTERNATIONAL_USD
    return ((ai + infra) / (1.0 - MIN_CONTRIBUTION_AT_CEILING) + fee.fixed_usd) / (1.0 - fee.percent)


@dataclass(frozen=True)
class TeamMonth:
    model: str
    seats: int
    premium_share: float
    utilization: float
    revenue: float
    payment_fee: float
    ai: float
    infra: float
    tcu_per_member_week: float

    @property
    def contribution(self) -> float:
        return self.revenue - self.payment_fee - self.ai - self.infra

    @property
    def margin(self) -> float | None:
        net = self.revenue - self.payment_fee
        return self.contribution / net if net > 0 else None


def seat_team_month(seats: int, premium_share: float, utilization: float) -> TeamMonth:
    premium = round(seats * premium_share)
    standard = seats - premium
    revenue = standard * PROPOSED_SEAT_USD["standard"] + premium * PROPOSED_SEAT_USD["premium"]
    fee = STRIPE_BR_INTERNATIONAL_USD.fee_usd(revenue)   # one invoice per workspace
    ai = standard * ai_cost_month_usd("pro", utilization) + premium * ai_cost_month_usd("max", utilization)
    infra = standard * variable_infra_per_account_usd("pro") + premium * variable_infra_per_account_usd("max")
    per_member = (standard * binding_weekly_tcu("pro") + premium * binding_weekly_tcu("max")) / max(1, seats)
    return TeamMonth("seats", seats, premium_share, utilization, revenue, fee, ai, infra, per_member)


def flat_team_month(members: int, utilization: float) -> TeamMonth:
    price = price_usd("team")
    fee = worst_case_fee(price).fee_usd(price)
    ai = ai_cost_month_usd("team", utilization)
    infra = variable_infra_per_account_usd("team") * members
    pool = binding_weekly_tcu("team")
    per_member = min(PLAN_ENTITLEMENTS["team"].weekly_tcu_limit, pool / max(1, members))
    return TeamMonth("flat", members, 1.0, utilization, price, fee, ai, infra, per_member)


# ── TEAM: which commercial shape (pre-launch closure, 2026-09-13) ─────────
#
# The rule: a customer pays in proportion to the capacity they receive, and
# no supported combination costs more than it earns. Capacity is weekly TCU,
# so "price per weekly TCU" is the unit both sides of that rule are held to.

#: A seat carries an existing plan's entitlement. It must never be cheaper
#: per unit than that plan bought alone (a team would become the cheap way to
#: buy PRO), and should not cost much more — the difference pays for team
#: features, as Claude Team's $25 Standard seat does over its $20 Pro.
SEAT_PREMIUM_OVER_INDIVIDUAL_MAX = 1.30
#: Claude Team sells 2 to 150 seats; TRINDATRON's TEAM is capped at 150 members.
TEAM_MIN_SEATS = team_pricing.MIN_SEATS
TEAM_MAX_SEATS = team_pricing.MAX_SEATS


def usd_per_weekly_tcu(price: float, weekly_tcu: float) -> float:
    return price / weekly_tcu if weekly_tcu > 0 else float("inf")


def flat_team_usable_weekly_tcu(members: int) -> float:
    """What a flat TEAM can really consume: the pool, but never more than
    every member using their own weekly cap."""
    e = PLAN_ENTITLEMENTS["team"]
    return min(float(e.team_weekly_pool_tcu_limit), max(0, members) * float(e.weekly_tcu_limit))


def flat_team_usd_per_usable_tcu(members: int) -> float:
    return usd_per_weekly_tcu(price_usd("team"), flat_team_usable_weekly_tcu(members))


def seat_usd_per_weekly_tcu(seat: str) -> float:
    return usd_per_weekly_tcu(PROPOSED_SEAT_USD[seat], binding_weekly_tcu(SEAT_ENTITLEMENT[seat]))


def seat_safe_weekly_ceiling_tcu(seat: str) -> float:
    """The largest weekly allowance one seat could carry at its proposed price
    and still clear the contribution floor — billed alone at the worst fee,
    which is the worst case for the fixed part of that fee."""
    price = PROPOSED_SEAT_USD[seat]
    net = price - worst_case_fee(price).fee_usd(price)
    ai_budget = net * (1.0 - MIN_CONTRIBUTION_AT_CEILING) \
        - variable_infra_per_account_usd(SEAT_ENTITLEMENT[seat])
    return max(0.0, ai_budget) / (1.0 + UNMETERED_SPEND_ALLOWANCE) / WEEKS_PER_MONTH / USD_PER_TCU


# ── Overage credits ──────────────────────────────────────────────────────


def credit_pack_margin(pack_usd: float, tcu_granted: float) -> float:
    """Contribution share of a credit pack consumed in full, at the worst fee."""
    net = pack_usd - worst_case_fee(pack_usd).fee_usd(pack_usd)
    cost = tcu_granted * USD_PER_TCU * (1.0 + UNMETERED_SPEND_ALLOWANCE)
    return (net - cost) / net if net > 0 else float("-inf")


def minimum_usd_per_tcu_for_credits(pack_usd: float = 10.0) -> float:
    """Price per TCU a credit pack must carry to leave the contribution floor."""
    return pack_usd / max_credits_for_payment(pack_usd, "USD", "TCU")


#: Currencies a payment can be valued in without an exchange rate.
USD_PEGGED_CURRENCIES = frozenset({"USD", "USDT", "USDC"})


def max_credits_for_payment(amount_fiat: float, currency_fiat: str, currency_credits: str) -> float | None:
    """The most credits a payment may buy — enforced on POST /billing/orders.

    The client used to choose credits_amount freely, so a verified $1 payment
    could carry a million credits. TCU credits are spent on provider calls, so
    they are capped where the pack still clears the contribution floor at the
    worst processor fee; USD credits are capped 1:1. None means the payment
    cannot be valued server-side (a currency with no rate here), and the order
    is refused rather than trusted.
    """
    if currency_fiat.upper() not in USD_PEGGED_CURRENCIES:
        return None
    credits = currency_credits.upper()
    if credits == "USD":
        return float(amount_fiat)
    if credits == "TCU":
        net = amount_fiat - worst_case_fee(amount_fiat).fee_usd(amount_fiat)
        return max(0.0, net * (1.0 - MIN_CONTRIBUTION_AT_CEILING) / (1.0 + UNMETERED_SPEND_ALLOWANCE) / USD_PER_TCU)
    return None


# ── Report ───────────────────────────────────────────────────────────────


def _money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def report() -> str:
    out: list[str] = []
    w = out.append

    w("== Payment processors (fee on each plan price) ==")
    w("processor                            " + "".join(f"{p:>10s}" for p in PAID_PLANS))
    for proc in PROCESSORS:
        w(f"{proc.name:36s} " + "".join(f"{_money(proc.fee_usd(price_usd(p))):>10s}" for p in PAID_PLANS))

    w("\n== Plan ceilings: current vs safe (worst fee, infra, allowance, 20% floor) ==")
    w("plan   price    current TCU/wk  safe TCU/wk  headroom  AI at ceiling  net      contribution  margin")
    for p in PAID_PLANS:
        members = 150 if p == "team" else 1
        m = plan_month(p, 1.0, members=members)
        cur, safe = binding_weekly_tcu(p), safe_weekly_ceiling_tcu(p, members=members)
        w(f"{p:6s} {_money(m.revenue):>7s}  {cur:>12,.0f}  {safe:>11,.0f}  {safe / cur - 1:>8.0%}  "
          f"{_money(m.ai):>13s}  {_money(m.net):>7s}  {_money(m.contribution):>12s}  {m.margin:>6.1%}")
    fm = plan_month("free", 1.0)
    w(f"free   $0.00    {binding_weekly_tcu('free'):>12,.0f}            —         —  "
      f"{_money(fm.ai):>13s}    $0.00  {_money(-fm.ai - fm.infra):>12s}       —")

    w("\n== Per subscription by utilisation (worst fee) ==")
    w("plan   " + "".join(f"{u:>12.0%}" for u in UTILIZATIONS))
    for p in PLANS:
        members = TEAM_MEMBERS_ASSUMED if p == "team" else 1
        w(f"{p:6s} " + "".join(f"{_money(plan_month(p, u, members=members).contribution):>12s}"
                               for u in UTILIZATIONS))

    w("\n== FREE population exposure (per month, plan-wide budget applied) ==")
    for n in (100, 1_000, 10_000, 100_000):
        w(f"{n:>7,} accounts  at 20%: {_money(free_population_ai_month_usd(n, 0.2)):>10s}   "
          f"at 100%: {_money(free_population_ai_month_usd(n, 1.0)):>10s}")

    w("\n== Infrastructure by scale (base mix) ==")
    for n in SCALES:
        counts = {p: round(n * s) for p, s in MIXES["base"].items()}
        counts["team"] *= TEAM_MEMBERS_ASSUMED
        infra = infrastructure_month(counts)
        w(f"{n:>6,} subs  total {_money(infra['total']):>9s}  railway {_money(infra['railway']):>9s}  "
          f"replicas {infra['core_replicas']:.0f}  pg steps {infra['postgres_steps']:.0f}  "
          f"redis {'yes' if infra['redis_required'] else 'no'}")

    for mix in MIXES:
        w(f"\n== Scale x utilisation, mix '{mix}': monthly result (margin) ==")
        w("subs    " + "".join(f"{u:>20.0%}" for u in UTILIZATIONS))
        for n in SCALES:
            cells = []
            for u in UTILIZATIONS:
                s = simulate(n, mix, u)
                cells.append(f"{_money(s.result):>11s} ({(s.margin or 0):>5.0%})")
            w(f"{n:>6,}  " + "".join(f"{c:>20s}" for c in cells))
        be = {u: break_even_subscriptions(mix, u) for u in (0.2, 0.5, 1.0)}
        w("break-even subscriptions: " + ", ".join(f"{u:.0%} use -> {v if v is not None else 'never'}"
                                                    for u, v in be.items()))

    w("\n== TEAM seat floor (worst fee, seat at 100% of its entitlement, 20% floor) ==")
    for seat in ("standard", "premium"):
        w(f"{seat:9s} floor {_money(seat_price_floor_usd(seat)):>8s}  proposed "
          f"{_money(PROPOSED_SEAT_USD[seat]):>8s}  market monthly "
          f"{_money(MARKET_SEATS_USD[f'claude_team_{seat}'][1]):>8s}")

    w("\n== TEAM seats at 100% utilisation: contribution (margin) ==")
    w("seats " + "".join(f"{s:>18.0%}" for s in TEAM_PREMIUM_SHARES))
    for n in TEAM_SEAT_COUNTS:
        cells = [seat_team_month(n, s, 1.0) for s in TEAM_PREMIUM_SHARES]
        w(f"{n:>5} " + "".join(f"{_money(c.contribution):>11s} ({(c.margin or 0):>4.0%})" for c in cells))

    w("\n== Flat TEAM ($199.90, shared pool) at 100% utilisation ==")
    for n in TEAM_SEAT_COUNTS:
        f = flat_team_month(n, 1.0)
        w(f"{n:>5} members  contribution {_money(f.contribution):>9s} ({(f.margin or 0):>4.0%})  "
          f"pool per member {f.tcu_per_member_week:>7,.0f} TCU/wk")

    w("\n== Overage credits ==")
    w(f"$10 pack at cost (1,000 TCU): margin {credit_pack_margin(10.0, 1_000):.1%}")
    w(f"minimum price per TCU for a $10 pack to clear the floor: ${minimum_usd_per_tcu_for_credits(10.0):.4f}")
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
