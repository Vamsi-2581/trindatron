"""Media Economics (CP36): premium media operations get their own budget.

media/plans.py's two guards (count ceilings, and every job reserving its estimated cost as TCU
through entitlement_service — max_tcu_per_request, session/daily/weekly windows) were sized for
GENERATE work: a handful of TCU per image or per second of video. A hosted, per-second-priced
operation like character_animate (Higgsfield Genjutsu, ~$0.318-0.681/s) breaks that assumption —
a single 3-second call already costs more than PRO's or even MAX's max_tcu_per_request, the flat
safety ceiling every ordinary chat/agent/image/video request is bounded by (src/entitlements.py).

Raising max_tcu_per_request to fit media would loosen that ceiling for every ordinary request too
— exactly what CP36 forbids. This module is the alternative: a SEPARATE ceiling, for a named,
explicit set of operations, that a plan must be granted access to at all before cost is even
considered. It does not replace max_tcu_per_request or entitlement_service.check_and_reserve
(both still run, unchanged, for every media job including premium ones) — it is an earlier,
additional gate that only premium operations pass through, in place of the ordinary
count-ceiling/fits_request_ceiling pair (media/plans.py) that GENERATE and non-premium
TRANSFORM/COMPOSE operations (background_remove, vectorize) keep using exactly as before.

Where this plugs in: services/media_service.py `_create()`, right where it currently reads
`daily.limit` / `plans.fits_request_ceiling` — see that function's own comment for the branch.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.media import plans

#: Operations gated by this module instead of the ordinary per-kind daily count + flat per-request
#: ceiling. Not every TRANSFORM/COMPOSE operation belongs here — background_remove and vectorize
#: are cheap and self-hosted, and stay on the path media/plans.py already prices correctly. A new
#: operation joins this set only when it is genuinely priced like Genjutsu (hosted, per-second,
#: materially above what max_tcu_per_request allows) — never inferred from cost at request time.
PREMIUM_MEDIA_OPERATIONS: frozenset[str] = frozenset({"character_animate"})


@dataclass(frozen=True)
class MediaBudget:
    """One plan's premium-media allowance.

    `allowed_operations` is the auditable policy: which premium operations this plan may submit
    AT ALL, independent of cost — checked before any price is even estimated, so a denied plan
    never reaches a "too expensive" message for a feature it was never sold. `request_ceiling_tcu`
    is this plan's max_tcu_per_request-equivalent, but scoped to premium media only: it exists
    because the real max_tcu_per_request (11 TCU on PRO, 60 on MAX) was sized for ordinary
    requests and is deliberately left untouched. `daily_ceiling_tcu` bounds premium media's own
    share of a day, independent of (and additional to) the plan's ordinary daily/weekly TCU
    windows, which entitlement_service.check_and_reserve still enforces unchanged for every job.
    """

    plan: str
    allowed_operations: frozenset[str]
    request_ceiling_tcu: float
    daily_ceiling_tcu: float | None = None


# ── The numbers (CP36 proposal — see the report's section D for the derivation and impact) ──────
#
# Product decision (given, not derived): START and PRO get no premium media operation at all —
# allowed_operations is empty, so operation_allowed() below refuses them before cost is
# considered, regardless of any ceiling value. MAX and TEAM get character_animate.
#
# request_ceiling_tcu (MAX/TEAM) = 200 TCU ($2.00 at plans._USD_PER_TCU=0.01). Derived from
# Genjutsu's own list pricing (settings.py media_higgsfield_genjutsu_usd_per_second_*) against
# this platform's EXISTING max video duration for these plans (5 s, media/plans.py MEDIA_ENTITLEMENTS
# "max"/"team") rather than Genjutsu's full 1-30 s documented range:
#   5s @ 480p = 5 * 0.318 = $1.590 = 159.0 TCU   -- fits
#   5s @ 720p = 5 * 0.681 = $3.405 = 340.5 TCU   -- does NOT fit at 200; 720p tops out ~2.9s here
#   3s @ 480p = 3 * 0.318 = $0.954 =  95.4 TCU   -- fits (the CP35/CP36 dry-run case)
# 200 covers the platform's own existing "up to 5s" video promise at 480p with headroom, while
# 720p above ~2.9s needs a deliberate future increase, not a silent allowance.
#
# daily_ceiling_tcu (MAX/TEAM) = 250 TCU ($2.50). Above the request ceiling (room for one full-size
# request plus a small second call the same day) but well under MAX's own daily_tcu_limit (350,
# src/entitlements.py) — premium media alone can never consume a whole day's ordinary budget.
# This is NOT media/plans.py's CREATION_SHARE_OF_DAILY_WINDOW (0.25): that fraction was calibrated
# for cheap, frequent creations (a few TCU each); applying it here (0.25 * 350 = 87.5) would be
# smaller than a single realistic request (95.4) and block the feature on its first honest use.
#
# Monthly/periodic ceiling: not applicable. No plan has a monthly TCU cap anywhere in this
# codebase today (src/entitlements.py, every plan's monthly_tcu_limit=None, "PARTE 10 — no
# operational monthly usage cap"). Introducing one just for media would add a tracking mechanism
# nothing else uses; the plan's existing WEEKLY window (235 TCU PRO / 950 TCU MAX, enforced
# unchanged by entitlement_service.check_and_reserve for every job including premium ones) already
# bounds premium media's cost over time.
#
# TEAM: mirrors MAX value-for-value. TEAM's weekly pool (src/entitlements.py
# team_weekly_pool_tcu_limit) is a downstream concern of entitlement_service.check_and_reserve,
# unchanged and untouched here — this module only decides whether a premium operation may be
# ATTEMPTED and how big ONE attempt or ONE day of them may be; the shared pool still has the last
# word on whether the reservation itself succeeds, exactly as it already does for every other job.
MEDIA_BUDGETS: dict[str, MediaBudget] = {
    "free": MediaBudget("free", frozenset(), 0.0, 0.0),
    "start": MediaBudget("start", frozenset(), 0.0, 0.0),
    "pro": MediaBudget("pro", frozenset(), 0.0, 0.0),
    "max": MediaBudget("max", frozenset({"character_animate"}), 200.0, 250.0),
    "team": MediaBudget("team", frozenset({"character_animate"}), 200.0, 250.0),
}


def budget_for(plan: str) -> MediaBudget:
    return MEDIA_BUDGETS.get((plan or "").lower(), MEDIA_BUDGETS["free"])


def operation_allowed(operation: str, plan: str) -> bool:
    """Whether `plan` may submit `operation` at all, independent of cost — one table, auditable,
    never an `if plan == "max"` at the call site. True for any operation NOT in
    PREMIUM_MEDIA_OPERATIONS: this function only gates the operations named above."""
    if operation not in PREMIUM_MEDIA_OPERATIONS:
        return True
    return operation in budget_for(plan).allowed_operations


def fits_media_request_ceiling(operation: str, plan: str, estimated_usd: float) -> bool:
    """The premium-media equivalent of plans.fits_request_ceiling — a SEPARATE number
    (MediaBudget.request_ceiling_tcu), never the real max_tcu_per_request. True for any operation
    not in PREMIUM_MEDIA_OPERATIONS (nothing for this module to bound)."""
    if operation not in PREMIUM_MEDIA_OPERATIONS:
        return True
    return plans.usd_to_tcu(estimated_usd) <= budget_for(plan).request_ceiling_tcu


def fits_media_daily_ceiling(operation: str, plan: str, used_today_usd: float, estimated_usd: float) -> bool:
    """Whether adding this request's estimated cost to what the plan already spent on `operation`
    today still fits MediaBudget.daily_ceiling_tcu. None (no separate daily ceiling) always fits —
    the plan's ordinary daily/weekly TCU window (entitlement_service) still applies regardless."""
    if operation not in PREMIUM_MEDIA_OPERATIONS:
        return True
    ceiling = budget_for(plan).daily_ceiling_tcu
    if ceiling is None:
        return True
    return plans.usd_to_tcu(used_today_usd + estimated_usd) <= ceiling + 1e-9
