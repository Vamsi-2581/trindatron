# Model and policy benchmark

Does the TRINDATRON System Policy make a model behave better, and which model
is the cheapest one that still clears the quality bar for a class of work?

**Results live in [`MODEL_COMPARISON.md`](MODEL_COMPARISON.md), the official
source of record.** This file describes the method and how to run it. It
carries no figures, so it can never disagree with the record.

Run it:

```
uv run python bench/run_policy_benchmark.py --repeats=5
uv run python bench/run_policy_benchmark.py --repeats=5 --only=claude-sonnet-5
uv run python bench/analyze_model_comparison.py
```

- The first sweeps the economy models and writes `results/model_comparison.json`.
- `--only=KEY` runs just the named models, one file each
  (`results/model_<key>.json`), and refuses to overwrite `model_comparison.json`.
- The analyzer recomputes every published number from the raw rows.

The standard suite — work the router itself classifies `standard`:

```
uv run python bench/run_policy_benchmark.py --suite=standard --repeats=5
uv run python bench/analyze_standard_tier.py
```

- Runs the four standard-tier models (`xai-grok`, `claude-haiku`,
  `claude-sonnet-5`, `gpt-4o`) plus `gpt-4o-mini` and `mistral-small` as a
  low-tier reference, each to `results/standard_<key>.json`.
- `analyze_standard_tier.py` applies the decision rule in its own docstring.
- `--tasks=ID,...` runs only the named tasks and writes
  `results/standard_<key>.resume-<UTC stamp>.json` beside the main file, which
  the analyzer reads with it — for completing a run that lost calls to
  something outside the model, such as an exhausted credit balance.
- `analyze_standard_tier.py --exclude=KEY` repeats the analysis without a model.

Nothing under `bench/` is imported by `src/`. It adds no provider to
production and uses only models this project can already reach. Result files
are gitignored: they are regenerated, not versioned.

---

## Method

**One variable.** Arm A and arm B differ only by whether the assembled system
policy is sent. Same model, same user message, same token ceiling, same tasks,
same process, same session.

**The control is not "nothing".** Arm A sends a minimal, honest product
instruction (`You are TRINDATRON, a helpful AI assistant.`) plus, for tool
tasks, the same tool list in plain prose. Comparing against an empty system
message would measure *the presence of any instruction*, and comparing against
a tool-less control would let arm B win tool tasks merely by knowing which
tools existed.

**Deterministic grading.** Every criterion is a Python function of the response
text. No model judges another model: a model-as-judge injects its own bias into
the single number the experiment turns on, and cannot be reproduced later.

**Traps are the point.** Several tasks are designed so the correct answer is a
refusal, an admission of ignorance, or a declined fabrication. The expensive
production failure is a confident invention, not a clumsy sentence, and a
benchmark made only of answerable questions cannot see it.

**Sampling.** `temperature=0` wherever the provider accepts it.
`claude-sonnet-5` rejects the parameter with a 400 and exposes no equivalent,
so it runs at its default sampling — an asymmetry stated in every result that
involves it.

**Reasoning headroom.** Each task's token cap gets +2000 on top, so a model
that thinks before answering is not scored on a truncated reply. Truncation
and empty answers are recorded per call and checked before any figure is used.

### Quality score

Per task: the weighted fraction of criteria met, then a penalty.

```
quality = (weighted criteria passed / total weight) - 0.25 if hallucinated
```

Hallucination criteria carry double weight *and* trigger the penalty, so
inventing a figure is not scored as "one point missing". An empty answer
scores zero.

### Cost

Input tokens x input rate + output tokens x output rate, at catalog list price.
Reasoning tokens are never added on top: every provider that reports them
already counts them inside the completion total.

### Statistics

A **paired bootstrap over tasks** (20,000 resamples, 95% interval, fixed seed).
Paired because both sides face the same tasks, so task difficulty is a shared
nuisance term that pairing removes. Repeats of one task are averaged first:
five samples of one prompt are not five independent observations. A difference
is only called real when the interval excludes zero.

### The standard suite

**Scope is decided by the router, not by us.** Every task in
`policy_benchmark/tasks_standard.py` is pinned to `standard` by the real
`compute_complexity`, fed exactly what the router receives: the capability,
the last user message and whether tools are attached. A test fails if any
task drifts out of the regime. Tasks that first scored just below the
threshold were extended only with material their real input would carry,
never with trigger words; the suite's docstring records which and by how much.

**Checkers are proven before they are trusted.** Every task has a reference
answer that must score 1.0 and a flawed answer that must lose a named
criterion. Before the run, one policy-arm pass of the two low-tier references
— never a standard-tier model — was read by hand to find checkers that
misread real phrasing. Code answers are parsed with `ast` and never executed.

**The rule came first.** `analyze_standard_tier.py` carries the decision rule
in its docstring and was committed before the suite made a single
standard-tier call, so the commit order is the proof.

**What the rows carry beyond the main suite:** retry count per call, the
provider's own billed cost where it reports one (xAI), the names of any
criterion whose checker raised, and a hash of the system message, so
"identical policy for every model" is verified from the data.

**Caching is probed separately**, production-shaped: one system message,
different user messages, the provider's own usage counters. The benchmark
itself sends no `cache_control`, exactly like production.

**The output ceiling is production's.** Every standard call gets PRO's
per-request limit, read from the router's `PLAN_LIMITS`: the tightest plan on
which the whole standard chain is reachable. A truncation at that ceiling is
what a PRO user would receive, so it is scored, not excluded.

**Missing data is handled complete-case.** If a model loses calls to something
outside itself, every model is judged on the tasks all of them answered in
both arms, so no model is averaged over a different set of questions.

---

## Measurement bugs found and fixed

Recorded because each one changed a conclusion, and each would otherwise have
been reported as a model finding. All are regression-tested
(`tests/unit/test_policy_benchmark.py`, `tests/unit/test_benchmark_accounting.py`,
`tests/unit/test_standard_benchmark.py`).

1. **The uncertainty detector failed 100% in both arms.** The models were
   declining correctly; the phrase list could not see it.
2. **The API-fabrication check missed the fabrication**, matching only
   `torch.flush_gradient_cache` while models wrote
   `torch.autograd.flush_gradient_cache()`.
3. **Token caps truncated reasoning models** until the headroom above existed.
4. **Latency double-counted retry backoff**, producing negative values.
5. **Reasoning tokens were billed twice**, overstating the cost of every model
   that reasons.
6. **Empty answers scored as passes**, reporting silence as honesty.
7. **The output ceiling starved a thinking model.** The main suite's cap plus
   2,000 left claude-sonnet-5 3,400 tokens on a planning task; it spent them all
   thinking and answered nothing. The standard suite uses PRO's production
   ceiling.
8. **A sleeping host is not a slow model.** The machine idle-slept mid-run and
   one call recorded 2,767 s against a 180 s socket timeout. That run was
   discarded, and the analyzer now counts calls slower than the timeout.
