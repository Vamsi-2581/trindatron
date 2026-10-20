"""Entry point for the model/policy benchmark. Not imported by src/.

    uv run python bench/run_policy_benchmark.py --repeats=5
    uv run python bench/run_policy_benchmark.py --repeats=5 --with-premium
    uv run python bench/run_policy_benchmark.py --repeats=5 --only=claude-sonnet-5

`--only` runs just the named models and writes each to its own
`bench/results/model_<key>.json`. It never touches `model_comparison.json`,
which is the economy run the official report is computed from — a single
premium sweep must not be able to overwrite it.

Models with no credential are reported BLOCKED and never called. Nothing here
adds a provider to production: the lab candidates live in
trinity_router.lab.LAB_CATALOG, which routing cannot reach (see
packages/model-router/tests/test_lab_isolation.py).

Figures are analysed, and every published number reproduced, by
`bench/analyze_model_comparison.py`.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.policy_benchmark.runner import ModelSpec, run, save_report

TEXT = frozenset({"text"})
TEXT_VISION = frozenset({"text", "vision"})
TEXT_VISION_PDF = frozenset({"text", "vision", "pdf"})

# ── Lab candidates (this mission's subject) ──────────────────────────────
# Prices and ids come from trinity_router.lab, which records the provider's
# OWN documentation rather than an aggregator's copy of it.
GLM = ModelSpec(
    key="glm-5.3-flash",
    endpoint="https://api.z.ai/api/paas/v4/chat/completions",
    model_id="glm-5.3-flash",
    api_key_env="ZAI_API_KEY",
    usd_per_1m_input=0.15, usd_per_1m_output=0.50,
    # Vision yes, inline PDF no. Measured 2026-09-12: every document task
    # returned 400 code 1210 "Failed to parse the file", 10/10 attempts, on a
    # PDF that gpt-4o-mini and claude-sonnet-5 both read correctly. So the file
    # is valid and Z.ai simply does not accept a base64 PDF on this endpoint —
    # it likely wants its own file-upload API. That is a delivery-format limit,
    # and recording it as UNSUPPORTED is the honest treatment: scoring it zero
    # would blame the model for something the harness could not deliver.
    modalities=TEXT_VISION,
)
GLM_47 = ModelSpec(
    key="glm-4.7-flash",
    endpoint="https://api.z.ai/api/paas/v4/chat/completions",
    model_id="glm-4.7-flash",
    api_key_env="ZAI_API_KEY",
    # litellm prices this at $0.00/$0.00. Treated as zero here for the VALUE
    # column, but see MODEL_COMPARISON.md — "free at the API" still has to be
    # confirmed against the provider's own billing, and a free tier is a
    # quota rather than capacity: measured refusing 2 of 5 back-to-back probes.
    usd_per_1m_input=0.0, usd_per_1m_output=0.0,
    modalities=TEXT,
)
DEEPSEEK = ModelSpec(
    key="deepseek-flash",
    endpoint="https://api.deepseek.com/chat/completions",
    model_id="deepseek-flash",
    api_key_env="DEEPSEEK_API_KEY",
    # Peak rates, deliberately: assuming the off-peak discount would make the
    # cost model wrong for half the working week.
    usd_per_1m_input=0.30, usd_per_1m_output=1.20,
    modalities=TEXT_VISION,
    offpeak_multiplier=0.5,
)

# ── Incumbents already routed in production ──────────────────────────────
GPT4O_MINI = ModelSpec(
    key="gpt-4o-mini", endpoint="https://api.openai.com/v1/chat/completions",
    model_id="gpt-4o-mini", api_key_env="OPENAI_API_KEY",
    usd_per_1m_input=0.15, usd_per_1m_output=0.60,
    modalities=TEXT_VISION_PDF,
)
MISTRAL_SMALL = ModelSpec(
    key="mistral-small", endpoint="https://api.mistral.ai/v1/chat/completions",
    model_id="mistral-small-latest", api_key_env="MISTRAL_API_KEY",
    usd_per_1m_input=0.15, usd_per_1m_output=0.60,
    modalities=TEXT_VISION,
)

# ── Premium reference (already configured; no provider added for it) ─────
# Runs without temperature (the model rejects it with a 400) and without a
# thinking parameter (the model then runs adaptive thinking, which is exactly
# what production's call sends). See call_model() in the runner.
PREMIUM = ModelSpec(
    key="claude-sonnet-5", endpoint="https://api.anthropic.com/v1/messages",
    model_id="claude-sonnet-5", api_key_env="ANTHROPIC_API_KEY",
    usd_per_1m_input=2.00, usd_per_1m_output=10.00, dialect="anthropic",
    modalities=TEXT_VISION_PDF,
    accepts_temperature=False,
)

# ── The rest of the standard tier, exactly as production routes it ────────
# Keys are the trinity_router.providers aliases; ids and prices are that
# catalog's. The standard chain is ranked by blended list price:
# xai-grok -> claude-haiku -> claude-sonnet -> gpt-4o.
XAI_GROK = ModelSpec(
    key="xai-grok", endpoint="https://api.x.ai/v1/chat/completions",
    model_id="grok-4.20-0309-non-reasoning", api_key_env="XAI_API_KEY",
    usd_per_1m_input=1.25, usd_per_1m_output=2.50,
    modalities=TEXT,
)
CLAUDE_HAIKU = ModelSpec(
    key="claude-haiku", endpoint="https://api.anthropic.com/v1/messages",
    model_id="claude-haiku-4-5", api_key_env="ANTHROPIC_API_KEY",
    usd_per_1m_input=1.00, usd_per_1m_output=5.00, dialect="anthropic",
    modalities=TEXT_VISION_PDF,
)
GPT4O = ModelSpec(
    key="gpt-4o", endpoint="https://api.openai.com/v1/chat/completions",
    model_id="gpt-4o", api_key_env="OPENAI_API_KEY",
    usd_per_1m_input=2.50, usd_per_1m_output=10.00,
    modalities=TEXT_VISION_PDF,
)

# ── CP10: Groq free-tier candidates ───────────────────────────────────────
# Official console.groq.com/docs/rate-limits, read live 2026-09-19: BOTH
# models sit on the exact same Free-plan limits — 30 RPM, 1K RPD, 8K TPM,
# 200K TPD — despite the 6x parameter difference. Paid (Developer) rate is
# what usd_per_1m_* below carries for REFERENCE only, same convention as
# GLM_47 above: this run stays inside the free quota, so real cost is $0,
# never the paid list price. "Free" here means "a quota, not a price" — see
# CP9's own finding on GLM-4.7-Flash refusing 2 of 5 back-to-back probes.
# No credit card, no new account: GROQ_API_KEY is the same key already
# configured for the ACTIVE groq-llama4-scout catalog entry.
GROQ_OSS_20B = ModelSpec(
    key="groq-oss-20b", endpoint="https://api.groq.com/openai/v1/chat/completions",
    model_id="openai/gpt-oss-20b", api_key_env="GROQ_API_KEY",
    usd_per_1m_input=0.0, usd_per_1m_output=0.0,  # free-tier run; paid rate is $0.075/$0.30
    modalities=TEXT,
    declare_empty_tools=True,  # CP12 fix — see ModelSpec.declare_empty_tools's own comment
)
GROQ_OSS_120B = ModelSpec(
    key="groq-oss-120b", endpoint="https://api.groq.com/openai/v1/chat/completions",
    model_id="openai/gpt-oss-120b", api_key_env="GROQ_API_KEY",
    usd_per_1m_input=0.0, usd_per_1m_output=0.0,  # free-tier run; paid rate is $0.15/$0.60
    modalities=TEXT,
    declare_empty_tools=True,  # CP12 fix — see ModelSpec.declare_empty_tools's own comment
)

ECONOMY = [GLM, GLM_47, DEEPSEEK, GPT4O_MINI, MISTRAL_SMALL]

#: The standard suite's subjects, in production chain order, then the two
#: low-tier incumbents as a REFERENCE: they show whether the standard tier buys
#: anything on these tasks. They are never candidates for a standard slot —
#: the router's quality floor excludes them there.
STANDARD_TIER = [XAI_GROK, CLAUDE_HAIKU, PREMIUM, GPT4O]
LOW_REFERENCE = [GPT4O_MINI, MISTRAL_SMALL]

#: Every model this script knows, by key — what `--only` selects from.
ALL_MODELS: dict[str, ModelSpec] = {
    s.key: s for s in (*ECONOMY, PREMIUM, XAI_GROK, CLAUDE_HAIKU, GPT4O,
                      GROQ_OSS_20B, GROQ_OSS_120B)
}

#: The economy run the official report is computed from. `--only` must never
#: write here.
OFFICIAL_RESULTS = "bench/results/model_comparison.json"

SUITES = ("economy", "standard")


def standard_output_ceiling() -> int:
    """Output ceiling for every standard-suite call: PRO's own per-request
    limit, read from the router rather than copied, because PRO is the
    tightest plan on which claude-sonnet-5 is reachable at all.

    Why the main suite's cap is not used here. On 2026-09-13 the first
    standard run used task.max_tokens + REASONING_HEADROOM (3,400 tokens on
    std_agent_zero_downtime_migration), and claude-sonnet-5's adaptive
    thinking spent the entire budget and returned an EMPTY, TRUNCATED answer.
    Production never imposes that ceiling on it — PRO allows 8,192 and MAX
    and TEAM 16,384 — so scoring it would have measured the harness, not the
    model. That run was stopped and discarded, for every model. A ceiling a
    model never reaches cannot change its answer, so the higher value costs
    the non-reasoning models nothing.
    """
    from trinity_router.limits import get_plan_limits

    return get_plan_limits("pro").max_output_tokens_per_request


def results_path_for(key: str, suite: str = "economy") -> str:
    """Per-model output file. Dots become underscores, matching the files the
    2026-09-12 run already wrote (model_glm-5_3-flash.json). The standard
    suite writes standard_<key>.json, so it can never land on an economy file."""
    stem = key.replace(".", "_")
    if suite == "economy":
        return f"bench/results/model_{stem}.json"
    return f"bench/results/{suite}_{stem}.json"


def resume_path_for(key: str, suite: str, stamp: str) -> str:
    """Where a `--tasks` run writes: beside the main file, never over it.

    Exists because a run can end with missing data for reasons that have
    nothing to do with the model — on 2026-09-13 the Anthropic account ran out
    of credit part-way through claude-sonnet-5 — and the missing tasks must be
    completable later without re-running, and re-paying for, the valid ones.
    The standard analyzer reads these files together with the main one.
    """
    return results_path_for(key, suite).removesuffix(".json") + f".resume-{stamp}.json"


def summarise(outcomes) -> dict:
    cells = defaultdict(list)
    blocked: set[str] = set()
    unsupported = defaultdict(set)
    for o in outcomes:
        if o.arm == "blocked":
            blocked.add(o.model_key)
            continue
        if o.unsupported:
            unsupported[o.model_key].add(o.category)
            continue
        if not o.error:
            cells[(o.model_key, o.arm)].append(o)

    if blocked:
        print("\nBLOCKED (no credential, zero calls made): " + ", ".join(sorted(blocked)))
    for model, cats in sorted(unsupported.items()):
        print(f"UNSUPPORTED modality — {model}: {', '.join(sorted(cats))}")

    print("\n" + "=" * 96)
    print(f"{'model':18s} {'arm':8s} {'quality':>8s} {'halluc':>7s} {'in':>7s} "
          f"{'out':>6s} {'reas':>5s} {'lat_s':>6s} {'$/task':>11s} {'VALUE':>8s}")
    print("-" * 96)
    rows = {}
    for (model, arm), items in sorted(cells.items()):
        n = len(items)
        q = sum(o.quality for o in items) / n
        h = sum(1 for o in items if o.hallucinated) / n
        ti = sum(o.input_tokens for o in items) / n
        to = sum(o.output_tokens for o in items) / n
        tr = sum(o.reasoning_tokens for o in items) / n
        lat = sum(o.latency_s for o in items) / n
        cost = sum(o.cost_usd for o in items) / n
        # VALUE = quality per dollar, scaled so the numbers are readable.
        # Deliberately NOT "cheapest wins": a model with 0.2 quality at a
        # tenth of the price scores badly here, which is the point.
        value = (q / cost / 1000) if cost else 0.0
        rows[(model, arm)] = dict(quality=q, halluc=h, in_tok=ti, out_tok=to,
                                  reasoning=tr, latency=lat, cost=cost, value=value, n=n)
        print(f"{model:18s} {arm:8s} {q:8.3f} {h:7.1%} {ti:7.0f} {to:6.0f} "
              f"{tr:5.0f} {lat:6.1f} {cost:11.7f} {value:8.2f}")

    print("\nIntegrity (a non-zero here must be explained before any figure is used):")
    for model in sorted({m for m, _ in cells}):
        items = [o for (m, _), group in cells.items() if m == model for o in group]
        errors = sum(1 for o in outcomes if o.model_key == model and o.error and o.arm != "blocked")
        print(f"  {model:18s} errors={errors} "
              f"truncated={sum(1 for o in items if getattr(o, 'truncated', False))} "
              f"empty={sum(1 for o in items if getattr(o, 'empty', False))}")

    print("\nPolicy delta (control -> policy):")
    for model in sorted({m for m, _ in cells}):
        c, p = rows.get((model, "control")), rows.get((model, "policy"))
        if c and p:
            print(f"  {model:18s} quality {c['quality']:.3f} -> {p['quality']:.3f} "
                  f"[{p['quality']-c['quality']:+.3f}]   halluc {c['halluc']:.1%} -> "
                  f"{p['halluc']:.1%}   out_tok {c['out_tok']:.0f} -> {p['out_tok']:.0f}")

    print("\nPer-category quality by model (policy arm):")
    bycat = defaultdict(lambda: defaultdict(list))
    for o in outcomes:
        if o.arm == "policy" and not o.error and not o.unsupported:
            bycat[o.category][o.model_key].append(o.quality)
    models_seen = sorted({m for m, _ in cells})
    header = "  " + f"{'category':14s}" + "".join(f"{m:>18s}" for m in models_seen)
    print(header)
    for cat in sorted(bycat):
        line = "  " + f"{cat:14s}"
        for m in models_seen:
            v = bycat[cat].get(m)
            line += f"{(sum(v)/len(v) if v else float('nan')):>18.3f}"
        print(line)
    return rows


def _arg(name: str) -> str:
    prefix = f"--{name}="
    return next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith(prefix)), "")


if __name__ == "__main__":
    repeats = int(_arg("repeats") or 5)
    suite = _arg("suite") or "economy"
    if suite not in SUITES:
        sys.exit(f"unknown suite {suite!r}; known: {SUITES}")
    tasks = None
    ceiling = None
    if suite == "standard":
        from bench.policy_benchmark.tasks_standard import STANDARD_TASKS
        tasks = STANDARD_TASKS
        ceiling = standard_output_ceiling()

    wanted = [t.strip() for t in _arg("tasks").split(",") if t.strip()]
    if wanted:
        from bench.policy_benchmark.tasks import TASKS as MAIN_TASKS
        pool = tasks if tasks is not None else MAIN_TASKS
        unknown = sorted(set(wanted) - {t.task_id for t in pool})
        if unknown:
            sys.exit(f"unknown task id(s) for suite {suite!r}: {unknown}")
        tasks = tuple(t for t in pool if t.task_id in wanted)

    only = [k.strip() for k in _arg("only").split(",") if k.strip()]
    if suite == "standard" and not only:
        only = [s.key for s in (*STANDARD_TIER, *LOW_REFERENCE)]
    if only:
        unknown = [k for k in only if k not in ALL_MODELS]
        if unknown:
            sys.exit(f"unknown model key(s): {unknown}; known: {sorted(ALL_MODELS)}")
        for key in only:
            outcomes = run([ALL_MODELS[key]], repeats=repeats, tasks=tasks, max_tokens=ceiling)
            summarise(outcomes)
            path = results_path_for(key, suite)
            if wanted:
                import time
                path = resume_path_for(key, suite, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            if Path(path).resolve() == Path(OFFICIAL_RESULTS).resolve():
                sys.exit(f"refusing to overwrite {OFFICIAL_RESULTS}")
            save_report(outcomes, path)
            print(f"\nwrote {path} (metrics only); {OFFICIAL_RESULTS} was not touched")
        sys.exit(0)

    models = list(ECONOMY)
    if "--with-premium" in sys.argv:
        models.append(PREMIUM)

    outcomes = run(models, repeats=repeats)
    rows = summarise(outcomes)

    save_report(outcomes, OFFICIAL_RESULTS)
    Path("bench/results/summary.json").write_text(
        json.dumps({f"{m}|{a}": v for (m, a), v in rows.items()}, indent=1),
        encoding="utf-8",
    )
    print(f"\nwrote {OFFICIAL_RESULTS} and summary.json (metrics only)")
