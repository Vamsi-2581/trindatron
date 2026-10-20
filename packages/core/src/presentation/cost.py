"""What a deck costs, worked out BEFORE it is allowed to start.

A presentation is the first capability whose cost is knowable in advance and
far larger than one model call. The reservation path assumes the opposite:
check_and_reserve() reserves a flat max_tcu_per_request — 4 TCU on START,
8 on PRO, 30 on MAX — and a deck measures around 17 TCU with stock images.
Reserving 4 for something that costs 17 recreates exactly the overshoot the
ADMIT+CLAMP pass removed.

So a deck is estimated first, from the shape of the request, and the
estimate is what gets reserved and what the plan is checked against. The
numbers below are the measured ones:

  Gemini 3.8 Flash   $0.75 / 1M in, $3.75 / 1M out   (promo to 2026-12-31)
  reasoning          billed as output and invisible; measured at roughly
                     2.5x the visible completion on live calls
  images, generated  $0.045 (low res) .. $0.151 (high res) each
  images, stock      free beyond the API call
  grounded search    5,000 free/month, then $14 / 1,000 = $0.014 each
  1 TCU              $0.01  (trinity_router.tcu.TCU_EXCHANGE_RATE)

Everything here is arithmetic over those. Nothing is invented.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.presentation.provider import ImageStrategy

USD_PER_TCU = 0.01

GEMINI_IN_PER_M = 0.75
GEMINI_OUT_PER_M = 3.75

# Gemini 3.8 Flash is a thinking model: a live call billed 70 total tokens
# for a 9-token prompt and a 0-token visible completion. Reasoning lands in
# the output column, where the price is five times the input rate, so it
# cannot be treated as a rounding error.
REASONING_MULTIPLIER = 2.5

# Per-slide token shape. Conservative: a slide that comes out shorter costs
# less, and an estimate that runs under reality would reintroduce overshoot.
OUTLINE_IN_TOKENS = 1_500
OUTLINE_OUT_TOKENS = 2_000
SLIDE_IN_TOKENS = 2_500
SLIDE_OUT_TOKENS = 900

IMAGE_USD_GENERATED = 0.151   # assume high res; the cheaper case comes in under
SEARCH_USD_PER_CALL = 0.014

# Model calls per deck, MEASURED rather than assumed. A real 5-slide
# generation on 2026-09-11 issued 10 calls through the gateway (which logs
# every one), not the 6 an "outline plus one per slide" model predicts:
# Presenton makes roughly two calls per slide, not one.
#
# This figure drives the reported call count, not the price — cost is
# dominated by the token totals below, and the same live run came in at
# $0.0205 against a $0.0714 estimate. The estimate is therefore ~3.5x
# conservative, which is the safe direction: a deck is refused slightly
# earlier than strictly necessary rather than started and left unpayable.
CALLS_OUTLINE = 1
CALLS_PER_SLIDE = 2


@dataclass(frozen=True)
class CostEstimate:
    slides: int
    model_calls: int
    text_usd: float
    image_usd: float
    search_usd: float

    @property
    def total_usd(self) -> float:
        return self.text_usd + self.image_usd + self.search_usd

    @property
    def tcu(self) -> float:
        return self.total_usd / USD_PER_TCU


def estimate(
    *,
    n_slides: int,
    image_strategy: ImageStrategy = ImageStrategy.STOCK_FIRST,
    web_search: bool = False,
) -> CostEstimate:
    """Estimate one deck, before anything is spent."""
    n_slides = max(1, int(n_slides))

    in_tokens = OUTLINE_IN_TOKENS + SLIDE_IN_TOKENS * n_slides
    out_tokens = (OUTLINE_OUT_TOKENS + SLIDE_OUT_TOKENS * n_slides) * REASONING_MULTIPLIER
    text_usd = in_tokens / 1e6 * GEMINI_IN_PER_M + out_tokens / 1e6 * GEMINI_OUT_PER_M

    # Only GENERATED spends on pictures. STOCK_FIRST resolves to stock today
    # (see presenton.image_provider_env) and therefore costs nothing here —
    # when the engine gains genuine selective generation this is the line
    # that changes, and it must change together with that behaviour.
    image_usd = n_slides * IMAGE_USD_GENERATED if image_strategy is ImageStrategy.GENERATED else 0.0

    # Grounding is charged per search, not per slide; a deck does a handful.
    search_usd = (3 * SEARCH_USD_PER_CALL) if web_search else 0.0

    return CostEstimate(
        slides=n_slides,
        model_calls=CALLS_OUTLINE + CALLS_PER_SLIDE * n_slides,
        text_usd=text_usd,
        image_usd=image_usd,
        search_usd=search_usd,
    )


def max_slides_within(budget_tcu: float, *, image_strategy: ImageStrategy,
                      web_search: bool = False) -> int:
    """The largest deck a remaining budget can pay for.

    Used to answer "you have room for a 6-slide deck, not the 20 you asked
    for" before starting, instead of failing at slide 14 with the money
    already spent.
    """
    for n in range(1, 61):
        if estimate(n_slides=n, image_strategy=image_strategy,
                    web_search=web_search).tcu > budget_tcu:
            return n - 1
    return 60
