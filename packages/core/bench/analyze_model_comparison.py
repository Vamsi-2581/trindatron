"""Reproduce every figure in MODEL_COMPARISON.md from the raw benchmark rows.

    uv run python bench/analyze_model_comparison.py
    uv run python bench/analyze_model_comparison.py FILE [FILE ...]

Defaults to the economy run (`model_comparison.json`) plus the premium run
(`model_claude-sonnet-5.json`) when present. Prints tables; writes nothing.

Three rules are applied uniformly to every row, whichever harness version
produced it, so every model is judged by the same arithmetic:

1. **Cost is recomputed from token counts** at the catalog list price:
   input x input rate + output x output rate. Reasoning tokens are NOT added —
   they are already inside the provider's completion count (verified live for
   glm-5.3-flash, deepseek-flash and claude-sonnet-5). Rows written before
   that fix carry a double-counted `cost_usd`; it is ignored here.
2. **An empty visible answer scores zero.** A successful call that said
   nothing satisfies every "does not say X" check. Newer rows carry an
   `empty` flag; older rows are detected as output tokens <= reasoning tokens.
3. **Errored and UNSUPPORTED rows are excluded**, never scored zero.

Significance is a paired bootstrap over TASKS (20,000 resamples, 95%), with a
fixed seed per comparison so the published intervals can be reproduced
exactly. The unit is the task, not the call: repeats of one task are averaged
first, because five samples of the same prompt are not five independent
observations of the model.

The decision rule
-----------------
Written into this file BEFORE the premium run was analysed, so the verdict
could not be shaped around the numbers. For each task class, comparing a
candidate against the model production routes that class to today:

* MATERIALLY BETTER  — paired delta >= +0.05 AND the 95% interval excludes 0
* BETTER, NOT MATERIAL — interval excludes 0 but delta < +0.05
* WORSE              — interval entirely below 0
* NO SIGNIFICANT DIFFERENCE — interval spans 0
* INCONCLUSIVE       — fewer than 3 tasks in the class; no interval is trusted

Paying more is JUSTIFIED only when the candidate is MATERIALLY BETTER and the
incumbent falls short of the 0.90 quality bar. If the incumbent already clears
the bar, sufficient quality is already bought and the cheaper model wins —
TRINDATRON optimises for the lowest real cost that reaches sufficient quality,
not for the highest score. A justified class is only actionable if the router
has a capability that can route it on its own.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.run_policy_benchmark import ALL_MODELS, OFFICIAL_RESULTS, results_path_for

SEED = 20260912
RESAMPLES = 20_000
PREMIUM_KEY = "claude-sonnet-5"

QUALITY_BAR = 0.90
MATERIAL_DELTA = 0.05
MIN_TASKS_FOR_A_VERDICT = 3

#: Which benchmark classes map onto a capability the router can route by
#: itself. A class with no entry cannot receive localized routing, whatever
#: the benchmark says about it.
ROUTABLE_CAPABILITY = {
    "chat": "chat",
    "research": "research",
    "coding": "code",
    "agent": "agent",
}

#: The model production routes each class to today, on the low-complexity
#: path every benchmark task but one lands on (see MODEL_COMPARISON.md).
PRODUCTION_PRIMARY = {"agent": "mistral-small"}
DEFAULT_PRODUCTION_PRIMARY = "gpt-4o-mini"

#: Cached-input prices, used only for the steady-state caching projection.
#: gpt-4o-mini and mistral-small cache automatically (measured hits, catalog
#: prices); claude-sonnet-5 caches only when a request carries cache_control
#: (measured: 1454 of ~1468 policy tokens served from cache), at 0.1x input.
CACHED_INPUT_USD_PER_1M = {
    "gpt-4o-mini": 0.075,
    "mistral-small": 0.015,
    "claude-sonnet-5": 0.20,
    # Standard tier. xai-grok's $0.20 is confirmed by xAI's own billed cost
    # (usage.cost_in_usd_ticks) on a live call with cached tokens; gpt-4o's
    # is half its input rate; claude-haiku's is 0.1x input like Sonnet's.
    "xai-grok": 0.20,
    "gpt-4o": 1.25,
    "claude-haiku": 0.10,
}
#: Anthropic cache writes bill at 1.25x input (5-minute TTL).
CACHE_WRITE_MULTIPLIER = {"claude-sonnet-5": 1.25, "claude-haiku": 1.25}


def load_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        rows.extend(json.loads(Path(p).read_text(encoding="utf-8")))
    return rows


def is_valid(row: dict) -> bool:
    return not row.get("error") and not row.get("unsupported") and row.get("arm") in ("control", "policy")


def is_empty(row: dict) -> bool:
    if "empty" in row:
        return bool(row["empty"])
    return int(row.get("output_tokens", 0)) - int(row.get("reasoning_tokens", 0) or 0) <= 0


def normalise(row: dict) -> dict:
    """Apply rules 1 and 2. Returns a new dict; the input is left untouched."""
    spec = ALL_MODELS[row["model_key"]]
    out = dict(row)
    out["cost_usd"] = (
        row["input_tokens"] / 1e6 * spec.usd_per_1m_input
        + row["output_tokens"] / 1e6 * spec.usd_per_1m_output
    )
    if is_empty(row):
        out["empty"] = True
        out["quality"] = 0.0
        out["hallucinated"] = False
    return out


def measured_cached_cost_usd(row: dict) -> float | None:
    """Cost using the row's OWN measured cached/written token counts. None when
    the model has no documented cached rate."""
    key = row["model_key"]
    spec = ALL_MODELS[key]
    cached_rate = CACHED_INPUT_USD_PER_1M.get(key)
    if cached_rate is None:
        return None
    cached = int(row.get("cached_input_tokens") or 0)
    written = int(row.get("cache_write_tokens") or 0)
    uncached = row["input_tokens"] - cached - written
    return (
        uncached / 1e6 * spec.usd_per_1m_input
        + cached / 1e6 * cached_rate
        + written / 1e6 * spec.usd_per_1m_input * CACHE_WRITE_MULTIPLIER.get(key, 1.0)
        + row["output_tokens"] / 1e6 * spec.usd_per_1m_output
    )


def projected_cached_cost_usd(policy_row: dict, control_input_tokens: float) -> float | None:
    """Steady-state cost of a policy-arm call once the policy prefix is warm.

    The cacheable prefix is estimated as the tokens the policy ADDS over the
    same task's control arm — the constant system text. The user's question
    and the tiny control instruction stay at full price, which makes this an
    under-estimate of the saving rather than an over-estimate. Cache writes
    are amortised away, which is what "steady state" means; a cold or sparse
    cache pays them. A projection, not a measurement.
    """
    key = policy_row["model_key"]
    spec = ALL_MODELS[key]
    cached_rate = CACHED_INPUT_USD_PER_1M.get(key)
    if cached_rate is None:
        return None
    prefix = max(0.0, policy_row["input_tokens"] - control_input_tokens)
    uncached = policy_row["input_tokens"] - prefix
    return (
        uncached / 1e6 * spec.usd_per_1m_input
        + prefix / 1e6 * cached_rate
        + policy_row["output_tokens"] / 1e6 * spec.usd_per_1m_output
    )


def task_means(rows: list[dict], field: str) -> dict[str, float]:
    by_task: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(float(r[field]))
    return {t: sum(v) / len(v) for t, v in by_task.items()}


def paired_bootstrap(a: dict[str, float], b: dict[str, float],
                     seed: int = SEED, resamples: int = RESAMPLES) -> dict:
    """Mean of (b - a) over shared tasks, with a percentile 95% interval."""
    tasks = sorted(set(a) & set(b))
    if not tasks:
        return {"n": 0, "delta": float("nan"), "lo": float("nan"), "hi": float("nan")}
    diffs = [b[t] - a[t] for t in tasks]
    rng = random.Random(seed)
    k = len(diffs)
    boots = sorted(sum(diffs[rng.randrange(k)] for _ in range(k)) / k for _ in range(resamples))
    return {
        "n": k,
        "delta": sum(diffs) / k,
        "lo": boots[int(0.025 * resamples)],
        "hi": boots[int(0.975 * resamples) - 1],
    }


def verdict(bs: dict) -> str:
    if bs["n"] == 0:
        return "no shared tasks"
    return "significant" if (bs["lo"] > 0 or bs["hi"] < 0) else "not significant"


def classify(bs: dict) -> str:
    """The pre-registered comparison verdict. `bs` is candidate - incumbent."""
    if bs["n"] < MIN_TASKS_FOR_A_VERDICT:
        return "INCONCLUSIVE"
    if bs["lo"] > 0:
        return "MATERIALLY BETTER" if bs["delta"] >= MATERIAL_DELTA else "BETTER, NOT MATERIAL"
    if bs["hi"] < 0:
        return "WORSE"
    return "NO SIGNIFICANT DIFFERENCE"


def cost_justified(classification: str, incumbent_quality: float) -> bool:
    """Paying more is justified only for a material, significant gain where the
    incumbent does not already reach sufficient quality."""
    return classification == "MATERIALLY BETTER" and incumbent_quality < QUALITY_BAR


def cell(rows: list[dict], model: str, arm: str, category: str | None = None) -> list[dict]:
    return [r for r in rows if r["model_key"] == model and r["arm"] == arm
            and (category is None or r["category"] == category)]


def summary(items: list[dict]) -> dict:
    n = len(items)
    lat = [r["latency_s"] for r in items]
    cached = [c for c in (measured_cached_cost_usd(r) for r in items) if c is not None]
    return {
        "n": n,
        "quality": sum(r["quality"] for r in items) / n,
        "halluc": sum(1 for r in items if r["hallucinated"]) / n,
        "lat_mean": statistics.fmean(lat),
        "lat_median": statistics.median(lat),
        "cost": sum(r["cost_usd"] for r in items) / n,
        "cost_measured_cache": (sum(cached) / len(cached)) if len(cached) == n else None,
        "in_tok": sum(r["input_tokens"] for r in items) / n,
        "out_tok": sum(r["output_tokens"] for r in items) / n,
        "reasoning": sum(int(r.get("reasoning_tokens") or 0) for r in items) / n,
        "cached_in": sum(int(r.get("cached_input_tokens") or 0) for r in items) / n,
        "truncated": sum(1 for r in items if r.get("truncated")),
        "empty": sum(1 for r in items if r.get("empty")),
    }


def projected_policy_cost(rows: list[dict], model: str) -> float | None:
    control_in = task_means(cell(rows, model, "control"), "input_tokens")
    vals = [projected_cached_cost_usd(r, control_in.get(r["task_id"], r["input_tokens"]))
            for r in cell(rows, model, "policy")]
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def main(paths: list[str]) -> None:
    raw = load_rows(paths)
    errors = [r for r in raw if r.get("error")]
    rows = [normalise(r) for r in raw if is_valid(r)]
    models = sorted({r["model_key"] for r in rows},
                    key=lambda m: (m == PREMIUM_KEY, m))

    print("INPUTS:", ", ".join(paths))
    print(f"rows {len(raw)}  valid {len(rows)}  errored {len(errors)}  "
          f"unsupported {sum(1 for r in raw if r.get('unsupported'))}")

    print("\n== Per model and arm (list price, no caching) ==")
    print(f"{'model':16s} {'arm':8s} {'n':>4s} {'quality':>8s} {'halluc':>7s} {'lat_mean':>8s} "
          f"{'lat_med':>7s} {'$/task':>10s} {'in':>6s} {'out':>5s} "
          f"{'reas':>5s} {'cached':>6s} {'trunc':>5s} {'empty':>5s}")
    for m in models:
        for arm in ("control", "policy"):
            items = cell(rows, m, arm)
            if not items:
                continue
            s = summary(items)
            print(f"{m:16s} {arm:8s} {s['n']:4d} {s['quality']:8.3f} {s['halluc']:6.1%} "
                  f"{s['lat_mean']:8.2f} {s['lat_median']:7.2f} {s['cost']:10.7f} "
                  f"{s['in_tok']:6.0f} {s['out_tok']:5.0f} {s['reasoning']:5.0f} "
                  f"{s['cached_in']:6.0f} {s['truncated']:5d} {s['empty']:5d}")

    print("\n== Policy arm cost/task: list price vs steady-state cached projection ==")
    for m in models:
        items = cell(rows, m, "policy")
        if not items:
            continue
        proj = projected_policy_cost(rows, m)
        s = summary(items)
        print(f"  {m:16s} list {s['cost']:.7f}   cached projection "
              f"{(f'{proj:.7f}' if proj is not None else 'no documented cached rate')}")

    print("\n== Policy effect within each model (policy - control), paired over tasks ==")
    for m in models:
        for field in ("quality", "output_tokens"):
            bs = paired_bootstrap(task_means(cell(rows, m, "control"), field),
                                  task_means(cell(rows, m, "policy"), field))
            print(f"  {m:16s} {field:14s} {bs['delta']:+9.3f} [{bs['lo']:+.3f}, {bs['hi']:+.3f}] "
                  f"n={bs['n']} {verdict(bs)}")

    if PREMIUM_KEY not in models:
        return

    print(f"\n== {PREMIUM_KEY} vs incumbents, same arm, paired over shared tasks ==")
    print("   delta = premium - incumbent")
    for inc in ("gpt-4o-mini", "mistral-small"):
        if inc not in models:
            continue
        for arm in ("policy", "control"):
            for field in ("quality", "hallucinated", "cost_usd", "latency_s"):
                bs = paired_bootstrap(task_means(cell(rows, inc, arm), field),
                                      task_means(cell(rows, PREMIUM_KEY, arm), field))
                print(f"  vs {inc:14s} {arm:8s} {field:12s} {bs['delta']:+10.5f} "
                      f"[{bs['lo']:+.5f}, {bs['hi']:+.5f}] n={bs['n']} {verdict(bs)}")

    print(f"\n== Decision table, policy arm: {PREMIUM_KEY} vs the class's production primary ==")
    print(f"  {'class':13s} {'tasks':>5s} {'primary':>14s} {'q_primary':>9s} {'q_sonnet':>8s} "
          f"{'delta':>7s} {'95% CI':>17s} {'verdict':>26s} {'justified':>9s} {'routable':>9s}")
    categories = ["OVERALL"] + sorted({r["category"] for r in rows})
    for cat in categories:
        category = None if cat == "OVERALL" else cat
        primary = PRODUCTION_PRIMARY.get(cat, DEFAULT_PRODUCTION_PRIMARY)
        inc_items = cell(rows, primary, "policy", category)
        prem_items = cell(rows, PREMIUM_KEY, "policy", category)
        if not inc_items or not prem_items:
            print(f"  {cat:13s} {'-':>5s} {primary:>14s} "
                  f"{'UNSUPPORTED' if not inc_items else '':>9s} — no paired comparison possible")
            continue
        bs = paired_bootstrap(task_means(inc_items, "quality"), task_means(prem_items, "quality"))
        q_inc = sum(r["quality"] for r in inc_items) / len(inc_items)
        q_prem = sum(r["quality"] for r in prem_items) / len(prem_items)
        c = classify(bs)
        routable = "overall" if cat == "OVERALL" else ("yes" if cat in ROUTABLE_CAPABILITY else "no")
        print(f"  {cat:13s} {bs['n']:5d} {primary:>14s} {q_inc:9.3f} {q_prem:8.3f} "
              f"{bs['delta']:+7.3f} [{bs['lo']:+.3f},{bs['hi']:+.3f}] {c:>26s} "
              f"{str(cost_justified(c, q_inc)):>9s} {routable:>9s}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = [OFFICIAL_RESULTS]
        premium = results_path_for(PREMIUM_KEY)
        if Path(premium).exists():
            args.append(premium)
    main(args)
