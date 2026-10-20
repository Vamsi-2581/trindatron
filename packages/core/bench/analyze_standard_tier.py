"""Reproduce every STANDARD-tier figure in MODEL_COMPARISON.md from raw rows.

    uv run python bench/analyze_standard_tier.py

Reads `bench/results/standard_<key>.json` for the four standard-tier models
and the two low-tier references. Prints tables; writes nothing.

The arithmetic is the main analysis's (`analyze_model_comparison.py`): cost
recomputed from token counts at list price with reasoning never added on top,
empty answers scored zero, errored rows excluded, task means over repeats, and
a paired bootstrap over tasks (20,000 resamples, seed 20260912).

THE DECISION RULE
-----------------
Written into this file on 2026-09-13, BEFORE the standard suite made a single
model call, and committed before the run. It may not be edited after results
exist; a changed rule would need a new run.

Scope. Only tasks the router itself classifies `standard` (recomputed below
from the task definitions with the real `compute_complexity`). Only the
policy arm, because production sends the policy; the control arm is reported
and never decided on.

Definitions, for X against Y, delta = X - Y, per the paired bootstrap:

  SUFFICIENT(X)              mean quality >= 0.90 (the same bar as the main suite)
  MATERIALLY BETTER          delta >= +0.05 and the 95% interval excludes 0
  WORSE                      the interval lies entirely below 0
  NON-INFERIOR               the interval's lower bound is above -0.05 — the
                             same 0.05 margin, applied in the other direction
  CHEAPER                    the paired cost-per-task interval lies entirely
                             below 0 (list price: traffic-independent, and the
                             same basis the router ranks on)
  INCONCLUSIVE               fewer than 3 tasks

Production's standard chain, by catalog blended cost:
  xai-grok -> claude-haiku -> claude-sonnet-5 -> gpt-4o
START uses slots 1-2, PRO 1-3, MAX and TEAM 1-4.

Step 1. BEST ECONOMIC MODEL = the cheapest model (measured policy cost per
        task) that is SUFFICIENT and NON-INFERIOR to every model that costs
        more than it. None qualifies -> the whole tier is INCONCLUSIVE.

Step 2. One verdict per standard-tier model, about what happens to IT:

  KEEP          it is the best economic model and already the primary; or it
                costs more than the best but is MATERIALLY BETTER in a routable
                class (>= 3 tasks) where the best is not SUFFICIENT — it earns
                its slot for that class (JUSTIFIED).
  REPLACE       it loses its slot to a named substitute that is SUFFICIENT and
                NON-INFERIOR to it: the best economic model takes the primary
                slot; or its provider is already covered earlier in the chain,
                it adds no JUSTIFIED class, and a model from a provider not yet
                in the chain is SUFFICIENT and NON-INFERIOR to it.
  FALLBACK      it stays in a later slot: SUFFICIENT, and it is the chain's
                first model from its provider (availability), with no JUSTIFIED
                class of its own.
  LAB           it leaves standard routing: not SUFFICIENT, WORSE than the best
                economic model, and the best economic model — SUFFICIENT and
                NON-INFERIOR to it — substitutes.
  INCONCLUSIVE  anything else. The rule then changes nothing.

Step 3. Per routable class (>= 3 tasks), the class's own best economic model.
        A localized route is JUSTIFIED only if it differs from the overall
        best, the overall best is not SUFFICIENT in that class, and the class
        best is MATERIALLY BETTER than it there. Otherwise no localized change.

Step 4. The low-tier references are compared against the best economic model.
        If one is SUFFICIENT and NON-INFERIOR to it, the finding is that the
        standard tier buys nothing on these tasks: REQUIRES A CALIBRATION
        DECISION on the complexity threshold. It is never acted on here.

Latency, hallucination rate and quality-per-dollar are reported beside every
verdict and decide nothing: the rule is quality floor first, then cost.
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.analyze_model_comparison import (  # noqa: E402
    MATERIAL_DELTA,
    MIN_TASKS_FOR_A_VERDICT,
    QUALITY_BAR,
    classify,
    is_valid,
    load_rows,
    measured_cached_cost_usd,
    normalise,
    paired_bootstrap,
    task_means,
    verdict,
)
from bench.run_policy_benchmark import results_path_for  # noqa: E402

STANDARD_CHAIN: tuple[str, ...] = ("xai-grok", "claude-haiku", "claude-sonnet-5", "gpt-4o")
LOW_REFERENCE: tuple[str, ...] = ("gpt-4o-mini", "mistral-small")
PROVIDER = {
    "xai-grok": "xai",
    "claude-haiku": "anthropic",
    "claude-sonnet-5": "anthropic",
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "mistral-small": "mistral",
}
#: Every standard-suite class is a capability the router routes on its own.
ROUTABLE = frozenset({"agent", "code", "research", "work", "chat"})
NON_INFERIORITY_MARGIN = MATERIAL_DELTA
DECISION_ARM = "policy"
#: The runner's socket timeout. A call slower than this cannot be a slow
#: provider — each socket wait is bounded by it — so it means the host was
#: stalled or suspended mid-call, and its latency is not a measurement.
#: Observed 2026-09-13: the machine idle-slept 13:03-13:49 UTC during a run
#: and one call recorded 2,767 s. That run was discarded. Reported here as an
#: integrity counter; it decides nothing.
SUSPENDED_HOST_LATENCY_S = 180.0


# ── the rule ─────────────────────────────────────────────────────────────

def non_inferior(bs: dict) -> bool:
    return bs["n"] >= MIN_TASKS_FOR_A_VERDICT and bs["lo"] > -NON_INFERIORITY_MARGIN


def cheaper(cost_bs: dict) -> bool:
    return cost_bs["n"] >= MIN_TASKS_FOR_A_VERDICT and cost_bs["hi"] < 0


def best_economic_model(models, mean_quality, mean_cost, quality_pair) -> str | None:
    """Step 1. `quality_pair(x, y)` is the bootstrap of x - y."""
    by_cost = sorted(models, key=lambda m: mean_cost[m])
    for i, m in enumerate(by_cost):
        if mean_quality[m] < QUALITY_BAR:
            continue
        if all(non_inferior(quality_pair(m, pricier)) for pricier in by_cost[i + 1:]):
            return m
    return None


def tier_verdicts(chain, mean_quality, mean_cost, quality_pair, justified_classes) -> dict[str, tuple[str, str]]:
    """Step 2. `justified_classes(y, best)` lists routable classes (>= 3 tasks)
    where y is MATERIALLY BETTER than best and best is not SUFFICIENT."""
    best = best_economic_model(chain, mean_quality, mean_cost, quality_pair)
    if best is None:
        reason = "no model is SUFFICIENT and NON-INFERIOR to every pricier model"
        return {m: ("INCONCLUSIVE", reason) for m in chain}

    out: dict[str, tuple[str, str]] = {}
    providers_ahead = {PROVIDER[best]}
    for m in chain:
        if m == best:
            if chain[0] == best:
                out[m] = ("KEEP", "best economic model, already the primary")
            else:
                out[m] = ("KEEP", f"best economic model; should hold the primary slot instead of {chain[0]}")
            continue

        sufficient = mean_quality[m] >= QUALITY_BAR
        vs_best = quality_pair(m, best)
        best_substitutes = mean_quality[best] >= QUALITY_BAR and non_inferior(quality_pair(best, m))
        classes = justified_classes(m, best)

        if classes and mean_cost[m] > mean_cost[best]:
            out[m] = ("KEEP", f"JUSTIFIED in {', '.join(classes)}")
            providers_ahead.add(PROVIDER[m])
            continue

        if not sufficient and classify(vs_best) == "WORSE" and best_substitutes:
            out[m] = ("LAB", f"below {QUALITY_BAR:.2f}, WORSE than {best}, which substitutes")
            continue

        if m == chain[0] and best_substitutes:
            role = "FALLBACK" if sufficient and PROVIDER[m] not in providers_ahead else "REPLACE"
            out[m] = (role, f"primary slot goes to {best}, SUFFICIENT and NON-INFERIOR to it")
            if role == "FALLBACK":
                providers_ahead.add(PROVIDER[m])
            continue

        if sufficient and PROVIDER[m] not in providers_ahead:
            out[m] = ("FALLBACK", f"SUFFICIENT; first {PROVIDER[m]} model in the chain")
            providers_ahead.add(PROVIDER[m])
            continue

        if sufficient and PROVIDER[m] in providers_ahead:
            substitute = next(
                (s for s in chain
                 if s != m and PROVIDER[s] not in providers_ahead
                 and mean_quality[s] >= QUALITY_BAR and non_inferior(quality_pair(s, m))),
                None,
            )
            if substitute:
                out[m] = ("REPLACE", f"{PROVIDER[m]} already covered earlier; {substitute} adds a provider "
                                     f"and is SUFFICIENT and NON-INFERIOR to it")
                continue

        out[m] = ("INCONCLUSIVE", "the rule's conditions for KEEP, REPLACE, FALLBACK or LAB are not met")
        if sufficient:
            providers_ahead.add(PROVIDER[m])
    return out


# ── data ─────────────────────────────────────────────────────────────────

def standard_task_ids() -> dict[str, str]:
    """task_id -> required_quality_class, from the real router function."""
    from bench.policy_benchmark.tasks_standard import STANDARD_TASKS, complexity_of

    return {t.task_id: complexity_of(t).required_quality_class for t in STANDARD_TASKS}


def default_paths() -> list[str]:
    """Each model's main file, followed by any `--tasks` resume files beside it."""
    paths: list[str] = []
    for key in (*STANDARD_CHAIN, *LOW_REFERENCE):
        main_file = Path(results_path_for(key, "standard"))
        if main_file.exists():
            paths.append(str(main_file))
            paths.extend(sorted(str(p) for p in main_file.parent.glob(main_file.stem + ".resume-*.json")))
    return paths


def cell(rows, model, arm, category=None):
    return [r for r in rows if r["model_key"] == model and r["arm"] == arm
            and (category is None or r["category"] == category)]


def summary(items: list[dict]) -> dict:
    n = len(items)
    lat = [r["latency_s"] for r in items]
    cached = [measured_cached_cost_usd(r) for r in items]
    billed = [r.get("provider_cost_usd") for r in items]
    q = sum(r["quality"] for r in items) / n
    cost = sum(r["cost_usd"] for r in items) / n
    return {
        "n": n, "quality": q,
        "halluc": sum(1 for r in items if r["hallucinated"]) / n,
        "lat_mean": statistics.fmean(lat), "lat_median": statistics.median(lat),
        "cost": cost,
        "cost_cached": (sum(cached) / n) if all(c is not None for c in cached) else None,
        "billed": (sum(billed) / n) if all(b is not None for b in billed) else None,
        "in_tok": sum(r["input_tokens"] for r in items) / n,
        "out_tok": sum(r["output_tokens"] for r in items) / n,
        "reasoning": sum(int(r.get("reasoning_tokens") or 0) for r in items) / n,
        "cached_in": sum(int(r.get("cached_input_tokens") or 0) for r in items) / n,
        "retried": sum(1 for r in items if int(r.get("attempts") or 1) > 1),
        "q_per_usd": (q / cost) if cost else float("nan"),
    }


def complete_case(rows: list[dict], models: list[str]) -> tuple[list[dict], list[str]]:
    """Keep only tasks where EVERY model has at least one valid row in BOTH arms.

    Added 2026-09-13, after the run and before any decision output was
    computed, because the run ended with missing data the rule had not
    anticipated: the Anthropic account ran out of credit part-way through
    claude-sonnet-5, so its last tasks errored. Averaging each model over its
    own task set would compare different questions — a model missing the chat
    tasks would be judged on an easier or harder mix. Restricting every model
    to the shared tasks is the conservative reading; the decision rule itself
    is unchanged. Returns (rows, dropped task ids).
    """
    have: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        have[(r["model_key"], r["arm"])].add(r["task_id"])
    all_tasks = {r["task_id"] for r in rows}
    shared = set(all_tasks)
    for m in models:
        shared &= have[(m, "control")] & have[(m, "policy")]
    return [r for r in rows if r["task_id"] in shared], sorted(all_tasks - shared)


def main(paths: list[str], exclude: frozenset[str] = frozenset()) -> None:
    classes_by_task = standard_task_ids()
    raw = [r for r in load_rows(paths) if r["model_key"] not in exclude]
    in_scope = [r for r in raw if classes_by_task.get(r["task_id"]) == "standard"]
    out_of_scope = sorted({r["task_id"] for r in raw if classes_by_task.get(r["task_id"]) != "standard"})
    rows = [normalise(r) for r in in_scope if is_valid(r)]
    models = [m for m in (*STANDARD_CHAIN, *LOW_REFERENCE) if any(r["model_key"] == m for r in rows)]
    rows, dropped = complete_case(rows, models)
    chain = tuple(m for m in STANDARD_CHAIN if m in models)

    print("INPUTS:", ", ".join(paths), f"  EXCLUDED MODELS: {sorted(exclude) or 'none'}")
    print(f"rows {len(raw)}  in standard scope {len(in_scope)}  valid (complete-case) {len(rows)}  "
          f"tasks {len({r['task_id'] for r in rows})}  excluded (not standard): {out_of_scope or 'none'}")
    print(f"complete-case: tasks dropped because some model lacks valid rows in an arm: {dropped or 'none'}")

    print("\n== Integrity (every non-zero must be explained before a figure is used) ==")
    for m in models:
        mine = [r for r in in_scope if r["model_key"] == m]
        print(f"  {m:16s} calls {len(mine):4d}  errors {sum(1 for r in mine if r.get('error')):3d}  "
              f"truncated {sum(1 for r in mine if r.get('truncated')):3d}  "
              f"empty {sum(1 for r in mine if r.get('empty')):3d}  "
              f"checker_errors {sum(1 for r in mine if r.get('checker_errors')):3d}  "
              f"retried {sum(1 for r in mine if int(r.get('attempts') or 1) > 1):3d}  "
              f"latency>{SUSPENDED_HOST_LATENCY_S:.0f}s {sum(1 for r in mine if r['latency_s'] > SUSPENDED_HOST_LATENCY_S):3d}")
    hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in in_scope:
        if r.get("system_sha256"):
            hashes[(r["task_id"], r["arm"])].add(r["system_sha256"])
    drift = {k: v for k, v in hashes.items() if len(v) != 1}
    print(f"  system message identical across models and repeats: "
          f"{'YES' if not drift else 'NO — ' + str(sorted(drift))} ({len(hashes)} task/arm cells)")

    print("\n== Per model and arm ==")
    print(f"  {'model':16s} {'arm':8s} {'n':>4s} {'quality':>7s} {'halluc':>6s} {'lat_mean':>8s} {'lat_med':>7s} "
          f"{'$/task list':>11s} {'$/task cache':>12s} {'billed':>10s} {'in':>6s} {'out':>5s} {'reas':>5s} "
          f"{'cached':>6s} {'retried':>7s} {'q per $':>8s}")
    stats: dict[tuple[str, str], dict] = {}
    for m in models:
        for arm in ("control", "policy"):
            items = cell(rows, m, arm)
            if not items:
                continue
            s = stats[(m, arm)] = summary(items)
            fmt = lambda v, w, p: f"{v:{w}.{p}f}" if v is not None else f"{'—':>{w}s}"  # noqa: E731
            print(f"  {m:16s} {arm:8s} {s['n']:4d} {s['quality']:7.3f} {s['halluc']:6.1%} {s['lat_mean']:8.2f} "
                  f"{s['lat_median']:7.2f} {s['cost']:11.6f} {fmt(s['cost_cached'], 12, 6)} "
                  f"{fmt(s['billed'], 10, 6)} {s['in_tok']:6.0f} {s['out_tok']:5.0f} {s['reasoning']:5.0f} "
                  f"{s['cached_in']:6.0f} {s['retried']:7d} {s['q_per_usd']:8.0f}")

    def pair(x: str, y: str, field: str, arm: str = DECISION_ARM, category: str | None = None) -> dict:
        return paired_bootstrap(task_means(cell(rows, y, arm, category), field),
                                task_means(cell(rows, x, arm, category), field))

    print("\n== Policy effect within each model (policy - control) ==")
    for m in models:
        bs = paired_bootstrap(task_means(cell(rows, m, "control"), "quality"),
                              task_means(cell(rows, m, "policy"), "quality"))
        print(f"  {m:16s} quality {bs['delta']:+.3f} [{bs['lo']:+.3f}, {bs['hi']:+.3f}] n={bs['n']} {verdict(bs)}")

    print(f"\n== Pairwise, {DECISION_ARM} arm (delta = row model - column model) ==")
    for i, x in enumerate(models):
        for y in models[i + 1:]:
            parts = []
            for field, p in (("quality", 3), ("hallucinated", 3), ("cost_usd", 5), ("latency_s", 2)):
                bs = pair(x, y, field)
                parts.append(f"{field} {bs['delta']:+.{p}f} [{bs['lo']:+.{p}f},{bs['hi']:+.{p}f}]"
                             f"{'*' if verdict(bs) == 'significant' else ''}")
            print(f"  {x:16s} - {y:16s} " + "  ".join(parts))
    print("  (* = 95% interval excludes zero)")

    categories = sorted({r["category"] for r in rows})
    print(f"\n== Per class, {DECISION_ARM} arm, mean quality ==")
    print("  " + f"{'class':10s} {'tasks':>5s}" + "".join(f"{m:>17s}" for m in models))
    for c in categories:
        n_tasks = len({r["task_id"] for r in rows if r["category"] == c})
        line = f"  {c:10s} {n_tasks:5d}"
        for m in models:
            items = cell(rows, m, DECISION_ARM, c)
            line += f"{(sum(r['quality'] for r in items) / len(items)) if items else float('nan'):17.3f}"
        print(line)

    if not chain:
        return
    mean_q = {m: stats[(m, DECISION_ARM)]["quality"] for m in chain}
    mean_c = {m: stats[(m, DECISION_ARM)]["cost"] for m in chain}
    qpair = lambda x, y: pair(x, y, "quality")  # noqa: E731

    def justified_classes(y: str, best: str) -> list[str]:
        found = []
        for c in categories:
            if c not in ROUTABLE:
                continue
            bs = pair(y, best, "quality", category=c)
            best_items = cell(rows, best, DECISION_ARM, c)
            if not best_items:
                continue
            q_best = sum(r["quality"] for r in best_items) / len(best_items)
            if classify(bs) == "MATERIALLY BETTER" and q_best < QUALITY_BAR:
                found.append(c)
        return found

    best = best_economic_model(chain, mean_q, mean_c, qpair)
    print("\n== Pre-registered decision (standard tasks, policy arm) ==")
    print(f"  cost order (measured, list price): "
          + " < ".join(f"{m} ${mean_c[m]:.5f}" for m in sorted(chain, key=mean_c.get)))
    print(f"  best economic model: {best or 'NONE — tier INCONCLUSIVE'}")
    for m, (v, why) in tier_verdicts(chain, mean_q, mean_c, qpair, justified_classes).items():
        print(f"  {m:16s} q={mean_q[m]:.3f} ${mean_c[m]:.5f}  {v:12s} {why}")

    print("\n== Per class: class best economic model vs overall best ==")
    for c in categories:
        n_tasks = len({r["task_id"] for r in rows if r["category"] == c})
        if n_tasks < MIN_TASKS_FOR_A_VERDICT:
            print(f"  {c:10s} {n_tasks} tasks — INCONCLUSIVE")
            continue
        cq = {m: summary(cell(rows, m, DECISION_ARM, c))["quality"] for m in chain}
        cc = {m: summary(cell(rows, m, DECISION_ARM, c))["cost"] for m in chain}
        cbest = best_economic_model(chain, cq, cc, lambda x, y, c=c: pair(x, y, "quality", category=c))
        note = "no localized change"
        if best and cbest and cbest != best:
            bs = pair(cbest, best, "quality", category=c)
            if cq[best] < QUALITY_BAR and classify(bs) == "MATERIALLY BETTER":
                note = f"JUSTIFIED LOCAL ROUTE to {cbest} ({bs['delta']:+.3f} [{bs['lo']:+.3f},{bs['hi']:+.3f}])"
            else:
                note = (f"class best {cbest} differs, but {best} is "
                        f"{'SUFFICIENT' if cq[best] >= QUALITY_BAR else 'not sufficient'} here and the "
                        f"gain is {classify(bs)} — no localized change")
        print(f"  {c:10s} {n_tasks} tasks  class best: {cbest or 'NONE'}  "
              + "  ".join(f"{m}={cq[m]:.3f}" for m in chain) + f"  -> {note}")

    if best:
        print(f"\n== Low-tier references vs {best} (step 4) ==")
        for ref in LOW_REFERENCE:
            if ref not in models:
                continue
            bs = pair(ref, best, "quality")
            cost_bs = pair(ref, best, "cost_usd")
            q_ref = stats[(ref, DECISION_ARM)]["quality"]
            suffices = q_ref >= QUALITY_BAR and non_inferior(bs)
            print(f"  {ref:16s} q={q_ref:.3f}  delta {bs['delta']:+.3f} [{bs['lo']:+.3f},{bs['hi']:+.3f}]  "
                  f"cost {cost_bs['delta']:+.5f} ({'cheaper' if cheaper(cost_bs) else 'not significantly cheaper'})"
                  f"  -> {'REQUIRES A CALIBRATION DECISION' if suffices else 'standard tier is buying quality'}")


if __name__ == "__main__":
    exclude = frozenset(
        k for a in sys.argv[1:] if a.startswith("--exclude=") for k in a.split("=", 1)[1].split(",") if k)
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(files or default_paths(), exclude=exclude)
