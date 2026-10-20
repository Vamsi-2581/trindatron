"""Prompt caching: the cost of a cache hit, and the invariants around it.

What this is actually guarding
------------------------------
Nothing here enables prompt caching, because nothing can: OpenAI and Mistral
both do it automatically for a stable prefix over their minimum length, with
no request parameter to set. Measured against the live APIs on 2026-09-12
with the real System Policy as the prefix — gpt-4o-mini cached 1024 of 1082
prompt tokens from the second call onward, mistral-small 1072 of 1105.

The defect was downstream: `prompt_tokens` is the FULL prompt, and the router
was pricing all of it at the standard input rate. A cached token bills at half
(OpenAI) or a tenth (Mistral), so every cache hit was over-charged — 1.86x on
gpt-4o-mini and 7.02x on mistral-small, which is the primary for
capability="agent". TCU derives from that cost and a plan's quota is spent in
TCU, so the error was consuming users' windows several times faster than their
real usage.

These tests pin the corrected arithmetic, the conservative defaults for every
provider whose cache behaviour is NOT confirmed, and the fact that none of
this reaches routing.
"""
from __future__ import annotations

import pytest

from trinity_router.providers import PROVIDER_CATALOG, rank_by_cost
from trinity_router.tcu import calculate_actual_cost_usd, calculate_tcu

#: Token counts OBSERVED against the live APIs — not invented.
MEASURED = {
    # 2026-09-12, real System Policy as the prefix.
    "gpt-4o-mini": {"prompt": 1082, "cached": 1024, "output": 7},
    "mistral-small": {"prompt": 1105, "cached": 1072, "output": 5},
    # 2026-09-13. xAI returns its own bill per call; this call billed
    # 1,218,500 ticks, which is exactly 75 x $1.25 + 128 x $0.20 + 1 x $2.50
    # per 1M. The same match held over 170 benchmark calls.
    "xai-grok": {"prompt": 203, "cached": 128, "output": 1},
    # 2026-09-13, real System Policy as the prefix, 6 of 6 repeated calls.
    # prompt and cached are the measurement; output is the probe's token cap.
    "gpt-4o": {"prompt": 1339, "cached": 1152, "output": 8},
}


# -- 11/12. The corrected cost, and the TCU derived from it ---------------

@pytest.mark.parametrize("key", sorted(MEASURED))
def test_a_cache_hit_costs_less_than_billing_the_whole_prompt(key):
    cfg = PROVIDER_CATALOG[key]
    m = MEASURED[key]
    assert cfg.cost_per_1m_cached_input_usd is not None, (
        f"{key} was measured caching; it must carry a verified cached price"
    )
    corrected = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd,
        m["prompt"], m["output"],
        cached_input_tokens=m["cached"],
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    old = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd,
        m["prompt"], m["output"],
    )
    assert corrected < old
    # Recompute independently rather than restating the implementation.
    expected = (
        (m["prompt"] - m["cached"]) / 1e6 * cfg.cost_per_1m_input_usd
        + m["cached"] / 1e6 * cfg.cost_per_1m_cached_input_usd
        + m["output"] / 1e6 * cfg.cost_per_1m_output_usd
    )
    assert corrected == pytest.approx(expected, abs=1e-9)


def test_the_agent_primary_was_the_worst_over_charge():
    """mistral-small's cached read is a tenth of its input rate, so the old
    arithmetic over-billed it ~7x — worth pinning, because it is the entry a
    future editor is most likely to 'simplify' to match gpt-4o-mini's half."""
    cfg = PROVIDER_CATALOG["mistral-small"]
    m = MEASURED["mistral-small"]
    old = calculate_actual_cost_usd(cfg.cost_per_1m_input_usd,
                                    cfg.cost_per_1m_output_usd, m["prompt"], m["output"])
    new = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, m["prompt"], m["output"],
        cached_input_tokens=m["cached"],
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    assert old / new > 5.0
    assert cfg.cost_per_1m_cached_input_usd == pytest.approx(0.015)


@pytest.mark.parametrize("key", sorted(MEASURED))
def test_tcu_follows_the_corrected_cost(key):
    """TCU is what a plan's quota is actually spent in, so it must track the
    corrected cost rather than being patched separately."""
    cfg = PROVIDER_CATALOG[key]
    m = MEASURED[key]
    cost = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, m["prompt"], m["output"],
        cached_input_tokens=m["cached"],
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    assert calculate_tcu(cost) == pytest.approx(round(cost * 100, 4))


# -- 4/5. Providers without confirmed caching must not change -------------

def test_omitting_the_new_arguments_reproduces_the_old_arithmetic():
    for cfg in PROVIDER_CATALOG.values():
        legacy = (2000 / 1e6 * cfg.cost_per_1m_input_usd
                  + 500 / 1e6 * cfg.cost_per_1m_output_usd)
        assert calculate_actual_cost_usd(
            cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 2000, 500,
        ) == pytest.approx(round(legacy, 8))


def test_absent_cached_tokens_is_not_an_error_and_costs_full_price():
    """A provider that reports no cache detail must behave exactly as before —
    this is the path every unconfirmed provider takes on every call."""
    cfg = PROVIDER_CATALOG["claude-sonnet"]
    with_zero = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 1000, 100,
        cached_input_tokens=0, cost_per_1m_cached_input_usd=None,
    )
    without = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 1000, 100,
    )
    assert with_zero == without


def test_cached_tokens_without_a_verified_price_bills_at_full_rate():
    """The conservative direction. A provider may report cached tokens before
    anyone has confirmed what they cost; guessing a discount would under-bill,
    so the absent price means 'charge normally', never 'assume cheaper'."""
    cfg = PROVIDER_CATALOG["claude-haiku"]
    assert cfg.cost_per_1m_cached_input_usd is None
    assert calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 1000, 100,
        cached_input_tokens=900, cost_per_1m_cached_input_usd=None,
    ) == calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 1000, 100,
    )


def test_only_models_measured_caching_carry_a_cached_price():
    """A price here is a claim that a cache hit was OBSERVED. Keeping the set
    exact stops someone copying litellm's table in for models nobody tested.
    xai-grok and gpt-4o joined on 2026-09-13, once both were observed caching
    the real policy prefix against the live APIs (xai-grok confirmed by xAI's
    own per-call bill). The Anthropic models stay out: production sends no
    cache_control, so they never hit, and claude-haiku-4-5 cannot cache a
    prefix this size even with it (4,096-token minimum)."""
    priced = {k for k, m in PROVIDER_CATALOG.items()
              if m.cost_per_1m_cached_input_usd is not None}
    assert priced == set(MEASURED)


# -- Defensive arithmetic -------------------------------------------------

def test_more_cached_than_prompt_tokens_is_clamped_not_trusted():
    cfg = PROVIDER_CATALOG["gpt-4o-mini"]
    cost = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 100, 10,
        cached_input_tokens=500,
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    assert cost == pytest.approx(
        100 / 1e6 * cfg.cost_per_1m_cached_input_usd + 10 / 1e6 * cfg.cost_per_1m_output_usd,
        abs=1e-9,
    )
    assert cost > 0


def test_negative_token_counts_still_raise():
    with pytest.raises(ValueError):
        calculate_actual_cost_usd(0.15, 0.60, 100, 10, cached_input_tokens=-1)
    with pytest.raises(ValueError):
        calculate_actual_cost_usd(0.15, 0.60, -1, 10)


def test_a_full_cache_hit_never_produces_a_negative_or_zero_cost():
    cfg = PROVIDER_CATALOG["mistral-small"]
    cost = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, 1000, 50,
        cached_input_tokens=1000,
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    assert cost > 0


# -- 9. Caching must not touch routing ------------------------------------

def test_the_cached_price_does_not_change_the_ranking():
    """Ranking must stay predictable. If cost ordering depended on whether a
    prefix happened to be warm, the same request would route differently from
    one minute to the next."""
    models = list(PROVIDER_CATALOG.values())
    before = [id(m) for m in rank_by_cost(models)]
    after = [id(m) for m in rank_by_cost(models, capability="agent")]
    assert before == [id(m) for m in rank_by_cost(models)]
    # The agent tiebreak still applies; the cached price contributes nothing.
    assert set(before) == set(after)


def test_agent_routing_is_unchanged_by_this_work():
    from trinity_router.limits import get_plan_limits
    from trinity_router.router import ModelRouter
    from trinity_router.selector import TaskType

    for plan in ("pro", "max", "team"):
        decision = ModelRouter()._build_chain_decision(
            TaskType.AGENT, plan, get_plan_limits(plan), "agent",
            disabled_provider_keys=frozenset(),
            messages=[{"role": "system", "content": "sys"},
                      {"role": "user", "content": "hi"}],
            tools=None,
        )
        chain = [k for k, _ in decision.chain]
        # gpt-4o-mini leads since it carries gpt-6-luna, which is cheaper than
        # mistral-small (2026-10-20); see test_agent_capability_priority.py.
        assert chain[0] == "gpt-4o-mini", chain
        assert chain[1] == "mistral-small", chain


# -- 10. No content, no secrets in the observability ----------------------

def test_cache_observation_records_counts_only(monkeypatch):
    """The metric labels must stay provider/model — never a session, a user,
    or anything derived from the prompt."""
    from trinity_router import router as R

    seen: list[dict] = []
    for metric in (R._cached_input_tokens, R._uncached_input_tokens, R._cache_hit_rate):
        monkeypatch.setattr(
            metric, "observe",
            lambda value, _s=seen, **labels: _s.append({"value": value, **labels}),
        )
    R._observe_cache(PROVIDER_CATALOG["gpt-4o-mini"], provider_key="gpt-4o-mini",
                     input_tokens=1082, cached_tokens=1024)
    assert len(seen) == 3
    for row in seen:
        assert set(row) == {"value", "provider", "model"}
    hit = [r for r in seen if 0 < r["value"] < 1][0]
    assert hit["value"] == pytest.approx(1024 / 1082)


def test_a_cache_miss_is_recorded_as_zero_not_skipped(monkeypatch):
    """Silence and a miss must not look the same, or a cache that stops
    working would simply vanish from the metrics."""
    from trinity_router import router as R

    seen: list[float] = []
    monkeypatch.setattr(R._cache_hit_rate, "observe",
                        lambda value, **labels: seen.append(value))
    R._observe_cache(PROVIDER_CATALOG["claude-sonnet"], provider_key="claude-sonnet",
                     input_tokens=500, cached_tokens=0)
    assert seen == [0.0]


def test_zero_input_tokens_records_nothing_rather_than_dividing_by_zero(monkeypatch):
    from trinity_router import router as R

    seen: list[float] = []
    monkeypatch.setattr(R._cache_hit_rate, "observe",
                        lambda value, **labels: seen.append(value))
    R._observe_cache(PROVIDER_CATALOG["gpt-4o-mini"], provider_key="gpt-4o-mini",
                     input_tokens=0, cached_tokens=0)
    assert seen == []
