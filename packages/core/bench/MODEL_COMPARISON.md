# Which model is cheap enough, and good enough?

> **OFFICIAL SOURCE OF RECORD** — 2026-09-12 benchmark, premium comparator
> and standard-tier suite completed 2026-09-13. This document is current and
> authoritative. There is no successor file and no second document; anything
> that claims to supersede this one is stale.
>
> Every number below is recomputed from the raw rows. The low-complexity
> figures come from `bench/analyze_model_comparison.py`, reading
> `bench/results/model_comparison.json` (the economy run) and
> `bench/results/model_claude-sonnet-5.json` (the premium run, on the same
> tasks, checkers, policy and headroom). The standard-tier figures come from
> `bench/analyze_standard_tier.py`, reading `bench/results/standard_<key>.json`.
>
> Figures from earlier, intermediate runs are not used anywhere in this
> document. They are recorded once, in "Discarded results", purely as history.

Measured 2026-09-12/13. 39 tasks × 2 arms × 5 repeats per model, deterministic
checkers, paired bootstrap over tasks for significance. 1,878 scored rows across
five models, zero errored calls, zero truncated calls in the premium run.

Standard tier, measured 2026-09-13: 17 tasks the router classifies `standard` ×
2 arms × 5 repeats, six models, 1,020 rows. claude-sonnet-5's run is incomplete
— the Anthropic account ran out of credit at its call 124 — so the tier is
analysed on the 12 tasks every model answered. See "The standard tier".

The question is not "which model is best" and not "which is cheapest". It is:
**what is the cheapest model that still clears the bar for a given task class.**

---

## Credentials

| provider | key present | consequence |
|---|---|---|
| OpenAI (gpt-4o-mini, gpt-4o) | yes | benchmarked, full run; gpt-4o on the standard suite |
| Mistral | yes | benchmarked, full run |
| Anthropic (claude-sonnet-5, claude-haiku-4-5) | yes — **credit balance exhausted 2026-09-13 ~15:10 UTC** | benchmarked; claude-haiku's standard run complete, claude-sonnet-5's standard run stopped at call 124 of 170 |
| Z.ai (GLM-5.3-Flash) | yes | benchmarked, full run |
| DeepSeek | yes | benchmarked, full run |
| Z.ai (GLM-4.7-Flash) | yes | **THROUGHPUT-BLOCKED — see below** |
| Gemini | yes | catalog verified live, not benchmarked |
| xAI (grok-4.20 non-reasoning) | yes | **benchmarked, standard suite** |
| Groq | yes, valid and working | see the retraction below |

**GLM-4.7-Flash produced no measurement.** The free tier refused 2 of 5
back-to-back probes. A free tier is a quota, not capacity, and a model that
cannot sustain a benchmark run cannot serve a plan either. It is recorded as
BLOCKED, never as a zero — the distinction matters, because a zero would
average into comparisons as though the model had tried and failed.

---

## Verified facts (provider documentation, not aggregators)

| model | id | $/1M in | $/1M out | cached in | ctx | vision | video | tools | reasoning effort | weights |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | `claude-sonnet-5` | 2.00 | 10.00 | 0.20 (needs `cache_control`) | 1M | yes | no | yes | yes | closed |
| GLM-5.3-Flash | `glm-5.3-flash` | 0.15 | **0.50** | **0.03** | 1M | yes | yes | yes | yes | MIT |
| GLM-4.7-Flash | `glm-4.7-flash` | **0.00** | **0.00** | — | 200K | no | no | yes | no | MIT |
| DeepSeek V4.1-Flash | `deepseek-flash` | 0.30 peak / 0.15 off | 1.20 peak / 0.60 off | **0.003** off | 1M | yes | no | yes | yes | MIT |
| gpt-4o-mini | `gpt-4o-mini` | 0.15 | 0.60 | **0.075** | 128K | yes | no | yes | **no** | closed |
| mistral-small | `mistral-small-latest` | 0.15 | 0.60 | **0.015** | 262K | yes | no | yes | no | Apache-2.0 |
| gemini-3.5-flash-lite | `gemini/gemini-3.5-flash-lite` | 0.30 | 2.50 | — | 1M | yes | yes | yes | yes | closed |

Endpoints: Z.ai `https://api.z.ai/api/paas/v4/chat/completions` (OpenAI-compatible,
Bearer). DeepSeek `https://api.deepseek.com` (OpenAI-compatible, Bearer).
Anthropic `https://api.anthropic.com/v1/messages`.

**claude-sonnet-5 request surface, verified live 2026-09-13:** `temperature`
is rejected with HTTP 400 ("`temperature` is deprecated for this model"), and
`top_p`/`top_k` are removed on the same model family. Omitting `thinking` runs
**adaptive thinking**; thinking tokens are reported in
`output_tokens_details.thinking_tokens` and are already inside `output_tokens`.
The minimum cacheable prefix is 1,024 tokens.

**DeepSeek peak window:** 01:00–04:00 and 06:00–10:00 UTC, Mon–Fri. Off-peak is
half price. Recorded at PEAK above deliberately — a cost model that assumes the
discount is wrong for half the working week.

**Two corrections to the mission's premise, both material:**

1. **GLM-5.3-Flash is not free.** It is the cheapest *paid* model in the GLM-5
   series. The free tier sits on the previous generation — `glm-4.7-flash` and
   `glm-4.5-flash` are priced at $0.00 with 200K context and working tool
   calling. That is the genuinely free option, and it is a quota, not capacity.
2. **Neither candidate is dramatically cheaper on list price.** GLM-5.3-Flash
   is 17% cheaper than gpt-4o-mini on output and identical on input. DeepSeek
   at peak is *twice* gpt-4o-mini, and merely equal off-peak. The real gap is
   **cached input**: $0.03 (GLM) and $0.003 (DeepSeek off-peak) against
   gpt-4o-mini's $0.075 — 2.5× and 25×.

The cached-input column stopped being theoretical on 2026-09-12: prompt caching
was measured working in production's own call path for gpt-4o-mini and
mistral-small, and the catalog prices it. See "Consistency with the code".

**litellm discrepancies** (litellm is a convenience, not an authority):
`deepseek/deepseek-flash` is absent from its cost map (only the legacy
`deepseek-v4-flash` exists), and it prices that legacy id at $0.44/$1.32 against
the provider's documented $0.30/$1.20. `zai/glm-5.3-flash` is present and
matches the official price exactly.

---

## Discarded results

Eleven sets of numbers were produced and then thrown away. They are named here so
nobody rediscovers them in an old revision and treats them as findings.

| discarded | why | replaced by |
|---|---|---|
| GLM 0.65, DeepSeek 0.77 overall quality | The token cap counted reasoning tokens. GLM spent 399 of 400 on reasoning and returned an empty answer; DeepSeek hit the cap on 42.5% of calls against gpt-4o-mini's 1.8%. ~1,110 calls discarded and re-run with `REASONING_HEADROOM=2000`. | the Results table |
| DeepSeek "0.0% hallucination" | It was measuring *silence*. Empty answers cannot hallucinate. | **5.6%** |
| Agent class 0.917 / 0.879 | Intermediate run, superseded by the 39-task final run. | **mistral-small 0.943, gpt-4o-mini 0.857** |
| Overall 0.937 / 0.927, and "the policy effect is significant for mistral-small" | Intermediate run. On the final data mistral-small's policy effect is **not** statistically significant. | the bootstrap table |
| Groq "4–8× production cost leak" | A measurement error in the probe, not a production fault. Retracted in full below. | no leak exists |
| claude-sonnet-5 0.869 / 0.845 (`expensive.json`) | A different 14-task set, recorded 2026-09-11 before the reasoning headroom existed. Not comparable. | the premium run |
| claude-sonnet-5 0.935 / 0.939 (`premium.json`) | A 39-task run recorded 2026-09-12 00:24 — before the headroom, on a model that thinks by default. **66 of 390 calls (17%) were cut off at the task cap**, against 6 of 390 for gpt-4o-mini on the same tasks. Its cost also double-counted thinking. | **0.921 / 0.919** |
| GLM and DeepSeek cost per task (policy 0.0006716 / 0.0011669) and "1.8× / 3.2× gpt-4o-mini" | The harness added reasoning tokens on top of the completion count, although both providers already include them there (verified live: `total_tokens = prompt + completion`). Reasoning was billed twice. | **0.0004554 / 0.0008580; 1.24× / 2.33×** |
| GLM policy 0.883, GLM `tool` 0.917, DeepSeek control 0.838, and DeepSeek's policy effect "+0.043, not significant" | Empty answers were scored like any other, so silence passed every "does not say X" criterion. This affected 1 GLM call and 6 DeepSeek calls; gpt-4o-mini and mistral-small had none. | **0.880, 0.883, 0.819, and +0.061 significant** |
| The first standard run, all six models (2026-09-13, 12:43–13:00 UTC) | The output ceiling was the main suite's task cap + 2,000 — 3,400 tokens on the migration task. claude-sonnet-5's adaptive thinking spent all of it: 11 of its first 69 calls came back truncated, 9 of them empty. Production's PRO ceiling is 8,192. Kept only as a reproducibility check for the five models the ceiling never bound (their largest reply used 48% of it). | the standard results, at 8,192 |
| The second standard launch (13:03–13:49 UTC) | The host idle-slept 10 seconds after launch (Windows Kernel-Power events 506/507). One in-flight call recorded 2,767 s against a 180 s socket timeout. Its latencies are not measurements. | the final standard run, with the host kept awake |

Checker bugs fixed before the final run: `_admits_uncertainty` failed 100% of
the time in *both* arms (its phrase list was too narrow), and the
API-fabrication check missed `torch.autograd.flush_gradient_cache()` — the exact
invention it existed to catch. A further fix corrected negative latencies caused
by double-counting retry backoff. The last two rows above were fixed on
2026-09-13 (commit `34c4d36`), and the analyzer applies both fixes to every row,
old or new.

The host-sleep failure was checked against the published low-complexity runs
too: no call in `model_comparison.json` or `model_claude-sonnet-5.json` took
longer than 60 s (the slowest, GLM, 34.2 s), so their latencies stand.

---

## Results — final run (39 tasks, 5 repeats)

List price, no caching:

| model | arm | quality | hallucination | in tok | out tok | reasoning tok | latency mean / median | $/task |
|---|---|---|---|---|---|---|---|---|
| gpt-4o-mini | control | 0.888 | 5.6% | 1276 | 66 | 0 | 1.22 / 0.97 s | 0.0002309 |
| gpt-4o-mini | **policy** | **0.927** | **4.1%** | 2285 | 42 | 0 | 1.21 / 1.14 s | 0.0003677 |
| mistral-small | control | 0.884 | 7.8% | 408 | 130 | 0 | 1.37 / 0.93 s | 0.0001392 |
| mistral-small | **policy** | **0.921** | **4.4%** | 1432 | 41 | 0 | **0.87 / 0.75 s** | **0.0002397** |
| claude-sonnet-5 | control | 0.921 | 5.1% | 494 | 199 | 11 | 3.23 / 2.20 s | 0.0029778 |
| claude-sonnet-5 | policy | 0.919 | 5.1% | 1883 | 139 | 9 | 2.67 / 1.88 s | 0.0051561 |
| glm-5.3-flash | control | 0.887 | 9.4% | 280 | 436 | 317 | 7.13 / 5.43 s | 0.0002599 |
| glm-5.3-flash | policy | 0.880 | 7.2% | 1290 | 524 | 432 | 8.70 / 5.64 s | 0.0004554 |
| deepseek-flash | control | 0.819 | 5.6% | 325 | 496 | 421 | 3.44 / 2.30 s | 0.0006931 |
| deepseek-flash | policy | 0.880 | 5.6% | 1342 | 380 | 257 | 3.04 / 2.12 s | 0.0008580 |

`document` (PDF) is UNSUPPORTED for mistral-small, GLM and DeepSeek — recorded
as a delivery-format limit, never scored zero. GLM returned 400 code 1210
"Failed to parse the file" on 10/10 attempts against a PDF that gpt-4o-mini and
claude-sonnet-5 read correctly, so the file is valid and the endpoint simply
does not take base64 PDFs.

**The one unavoidable protocol difference:** claude-sonnet-5 runs at its default
sampling, because it rejects `temperature` and exposes no substitute. Everything
else is identical, including the policy text, which was byte-identical across
all five repeats of every task. The asymmetry gives Sonnet sampling variance the
other models do not have; with 5 repeats averaged per task, it did not produce a
measurable disadvantage.

**List price ≠ cost per task.** GLM lists output 17% cheaper than gpt-4o-mini's,
yet costs **1.24× more per task**: 432 of its 524 output tokens are reasoning.
DeepSeek costs 2.33× more for the same reason. Sonnet is the opposite case — its
list price is 13–17× gpt-4o-mini's and its measured cost per task is 14.0×,
because it thinks on only 76 of 390 calls (about 50 tokens each).

### Statistically established (paired bootstrap, 20,000 resamples, 95%)

| model | effect | delta | 95% CI | verdict |
|---|---|---|---|---|
| gpt-4o-mini | policy → quality | **+0.039** | [+0.009, +0.079] | **significant** |
| deepseek-flash | policy → quality | **+0.061** | [+0.027, +0.103] | **significant** |
| mistral-small | policy → quality | +0.037 | [−0.014, +0.100] | not significant |
| claude-sonnet-5 | policy → quality | −0.002 | [−0.031, +0.027] | not significant |
| glm-5.3-flash | policy → quality | −0.007 | [−0.053, +0.042] | not significant |
| gpt-4o-mini | policy → output tokens | −24 | [−51, −7] | **significant** |
| mistral-small | policy → output tokens | −89 | [−156, −35] | **significant** |
| claude-sonnet-5 | policy → output tokens | −60 | [−89, −34] | **significant** |
| deepseek-flash | policy → output tokens | −117 | [−226, −18] | **significant** |
| glm-5.3-flash | policy → output tokens | **+88** | [+5, +179] | **significant** |

The honest reading is narrower than the headline deltas suggest. The policy's
quality effect is established **for gpt-4o-mini and deepseek-flash only**. For
Sonnet it is indistinguishable from zero — the premium model gains nothing in
quality from the policy — but it does shorten Sonnet's answers, as it does for
every model except GLM.

---

## claude-sonnet-5 — definitive measurement and decision

### Integrity

390 of 390 calls valid. Zero errors, zero unsupported, every call ended with
`end_turn`, **zero truncated, zero empty answers**, no negative latency. Policy
text identical across repeats and tasks. Nothing was re-run or excluded.

### Head to head, paired over the same tasks

Delta = claude-sonnet-5 minus the incumbent.

| vs | arm | quality | hallucination rate | cost/task | latency |
|---|---|---|---|---|---|
| gpt-4o-mini | **policy** | −0.008 [−0.062, +0.038] n.s. | +0.010 [−0.031, +0.062] n.s. | **+$0.00479** [+0.00404, +0.00564] | **+1.46 s** [+1.02, +1.96] |
| gpt-4o-mini | control | +0.033 [−0.023, +0.098] n.s. | −0.005 [−0.056, +0.036] n.s. | **+$0.00275** [+0.00192, +0.00364] | **+2.01 s** [+1.48, +2.59] |
| mistral-small | **policy** | −0.008 [−0.044, +0.025] n.s. | +0.011 [+0.000, +0.028] n.s. | **+$0.00479** [+0.00407, +0.00563] | **+1.87 s** [+1.38, +2.44] |
| mistral-small | control | +0.031 [−0.020, +0.087] n.s. | −0.022 [−0.078, +0.017] n.s. | **+$0.00274** [+0.00195, +0.00362] | **+1.97 s** [+1.51, +2.48] |

Production sends the policy, so the **policy** rows are the decision-relevant
ones. Per task against gpt-4o-mini: 31 of 39 tie, Sonnet is better on 4 and
worse on 4. **No quality difference is significant, and every cost and latency
difference is.**

### The decision rule, and its verdict per class

The rule was written into `analyze_model_comparison.py` before the premium run
was analysed. A candidate is MATERIALLY BETTER only if its paired delta is at
least +0.05 **and** the 95% interval excludes zero. Paying for it is JUSTIFIED
only if it is materially better **and** the incumbent is below the 0.90 quality
bar; if the incumbent already clears the bar, sufficient quality is already
bought and the cheaper model wins.

Policy arm, claude-sonnet-5 against the model production routes each class to:

| class | tasks | production primary | primary | Sonnet | delta [95% CI] | verdict | justified | routable |
|---|---|---|---|---|---|---|---|---|
| **overall** | 39 | gpt-4o-mini | 0.927 | 0.919 | −0.008 [−0.062, +0.038] | no significant difference | no | — |
| agent | 7 | mistral-small | 0.943 | 0.945 | +0.002 [−0.155, +0.143] | no significant difference | no | yes |
| chat | 5 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | yes |
| coding | 3 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | yes |
| research | 3 | gpt-4o-mini | 1.000 | 0.900 | −0.100 [−0.200, +0.000] | no significant difference | no | yes |
| uncertainty | 5 | gpt-4o-mini | 0.700 | 0.507 | −0.193 [−0.487, +0.027] | no significant difference | no | no |
| image | 4 | gpt-4o-mini | 0.917 | 1.000 | +0.083 [+0.000, +0.250] | no significant difference | no | no |
| document | 3 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | no |
| structured | 3 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | no |
| tool | 3 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | no |
| long_context | 3 | gpt-4o-mini | 1.000 | 1.000 | 0.000 | no significant difference | no | no |

**No class justifies Sonnet.** The only positive delta of any size (image,
+0.083) is not significant and has no routable capability behind it. Research
and uncertainty point the other way, also without significance. With 3–7 tasks
per class, a small real advantage could hide inside these intervals — which is
exactly why the rule refuses to spend money on one.

### Caching does not change the economics

Measured live 2026-09-13 with the policy as the system prompt: **without**
`cache_control` (what production sends today) Sonnet cached 0 tokens; **with**
it, 1,454 of ~1,468 prompt tokens were served from cache from the second call.
Applying each model's cached rate to its policy prefix, in steady state:

| model | policy $/task, list | policy $/task, cached (projection) |
|---|---|---|
| mistral-small | 0.0002397 | 0.0001014 |
| gpt-4o-mini | 0.0003677 | 0.0002920 |
| claude-sonnet-5 | 0.0051561 | 0.0026559 |

Sonnet stays **14.0× → 9.1×** gpt-4o-mini and **21.5× → 26.2×** mistral-small.
Caching halves Sonnet's cost and still leaves it an order of magnitude above
either incumbent. (The projection assumes a warm prefix and excludes cache-write
premiums; it is not a measurement of production traffic.)

### What the benchmark measured — and what it did not

Under the router's own `compute_complexity`, **38 of the 39 tasks score
`ultra_low` or `low`** in both arms. That is the regime where production routes
gpt-4o-mini and mistral-small, and where Sonnet is never selected.

Production uses Sonnet in exactly two places: **third** in the `standard`-floor
chains of PRO, MAX and TEAM (after `xai-grok` and `claude-haiku`, for agent,
code and automation), and **primary on the legacy `select_provider_chain()`
path**, used only when complexity cannot be computed. It is the primary of 0 of
314 dynamic routing cells.

The one task that reaches `standard`, `agent_medium_plan`, points in Sonnet's
favour: Sonnet 1.000, mistral-small 0.933, gpt-4o-mini 0.733 (policy arm) — at
$0.01127 per task, 36–40× the incumbents, and 9.7 s. That is **one task**: a
direction, not evidence.

### Decision: **A) KEEP EXACTLY AS IS**

- **In the measured, low-complexity regime**, Sonnet is statistically equal to
  gpt-4o-mini and mistral-small, 14–21× more expensive and 1.5–1.9 s slower.
  The cheapest model is already what production routes there, so there is
  nothing to change.
- **In the standard-complexity regime**, where Sonnet actually serves, the
  benchmark has one task. It supports neither removing nor demoting Sonnet —
  and the one data point it has leans towards Sonnet.
- No catalog, router, priority or chain was changed.

The earlier label "EVIDÊNCIA INCOMPLETA" is retired because the gap it named —
Sonnet had not run the final task set — is closed. A narrower, precise gap
replaces it: **standard-complexity work is unmeasured**, for Sonnet and for the
two models ahead of it in those chains.

**Update, same day:** that gap is now measured, on 12 of 17 standard tasks for
Sonnet — see "The standard tier" below. The decision above stands.

---

## The standard tier — measured 2026-09-13

Everything above this section is the low-complexity regime. This one is the
work the router sends to `standard`. There, START runs `xai-grok →
claude-haiku`, PRO adds `claude-sonnet`, MAX and TEAM add `gpt-4o`, and FREE is
not entitled. Simulated with the real `_build_chain_decision`, the chain is
identical for all seven capabilities.

Reproduced by `bench/analyze_standard_tier.py`.

### The suite

17 tasks in `bench/policy_benchmark/tasks_standard.py` — agent 4, code 4,
research 3, work 3, chat 3 — each pinned to `standard` (scores 50–67) by the
router's real `compute_complexity` and a test. Every task has a unique,
checkable answer: a computed invoice total, an exact program output, the one
vendor that satisfies five constraints, specific record ids. 10 of 17 carry a
fabrication trap. Code answers are parsed with `ast`, never executed.

Five tasks first scored 44–49 (`low`). They were extended only with material
their real input carries — an RFP pack's security note, reader comments under
a blog post, a reference month, the forwarded emails of a thread, the rest of a
design doc. No instruction wording changed. Two observations about the
heuristic itself, not about any model: the word "class" in "economy class"
adds +15 "code" to an expense task, exactly as it would in production; and a
five-constraint vendor elimination needs ~2,250 characters to leave `low`.

### Protocol

- **Pre-registered.** The decision rule, the suite and the checkers were
  committed (`bc922e3`, 12:43 UTC) before any standard-tier model was called.
- **Checkers calibrated on the low-tier references only.** One policy-arm pass
  of gpt-4o-mini and mistral-small was read by hand before the rule was frozen:
  8 false negatives, 2 false positives, 2 criteria the prompts never asked for
  and 1 unchecked instruction were fixed. Every task has a reference answer
  that must score 1.0 and a flawed one that must lose a named criterion.
- **Output ceiling = PRO's production limit, 8,192 tokens** (`a964a10`), read
  from `PLAN_LIMITS`. temperature=0 for every model that accepts it,
  claude-haiku-4-5 included; claude-sonnet-5 rejects it.
- **Same policy, verified from the rows.** The system-message hash is identical
  across models and repeats in all 34 task/arm cells.

### Integrity, and the data that is missing

| model | calls | errors | truncated | empty | checker errors | latency > 180 s |
|---|---|---|---|---|---|---|
| xai-grok | 170 | 0 | 0 | 0 | 0 | 0 |
| claude-haiku | 170 | 0 | 0 | 0 | 0 | 0 |
| claude-sonnet-5 | 170 | **47** | 6 | 0 | 0 | 0 |
| gpt-4o | 170 | 0 | 0 | 0 | 0 | 0 |
| gpt-4o-mini | 170 | 0 | 0 | 0 | 0 | 0 |
| mistral-small | 170 | 0 | 0 | 0 | 0 | 0 |

**Sonnet's 47 errors are not the model's.** Every one is HTTP 400 "Your credit
balance is too low to access the Anthropic API", from call 124 onward; no call
succeeded after the first. Its last five tasks — all three chat tasks and two
of three work tasks — have no valid policy-arm rows. Averaging each model over
its own task set would compare different questions, so the tier is analysed
**complete-case**: every model on the 12 tasks all six answered in both arms
(agent 4, code 4, research 3, work 1). The five complete models are also
reported on all 17.

**The six truncations are production behaviour, not a harness artefact.** All
are on `std_agent_zero_downtime_migration`, where Sonnet's adaptive thinking
averages ~5,300 tokens: 6 of its 10 calls end at the 8,192 ceiling, still
scoring 1.00, at ~$0.08 and ~80 s each. A PRO user would receive exactly that.

**Temperature 0 is not deterministic.** Policy arm, 17 tasks, the discarded
first run against the final one: claude-haiku 0.807 → 0.807 (every task
identical); gpt-4o-mini 0.772 → 0.771; xai-grok 0.830 → 0.845; gpt-4o
0.848 → 0.834; mistral-small 0.826 → **0.757**, with class means moving by up to
0.26. The paired bootstrap covers tasks and repeats within one run, not this
drift between runs.

### Results — complete-case, 12 tasks

| model | arm | quality | halluc. | latency mean / median | $/task list | $/task, cache as incurred | out tok | thinking tok | quality per $ |
|---|---|---|---|---|---|---|---|---|---|
| xai-grok | control | 0.835 | 8.3% | 4.21 / 4.05 s | 0.001732 | 0.001358 | 441 | 0 | 482 |
| xai-grok | **policy** | 0.883 | 0.0% | 3.36 / 3.09 s | **0.002752** | **0.001604** | 347 | 0 | 321 |
| claude-haiku | control | 0.860 | 8.3% | 6.56 / 5.90 s | 0.003222 | 0.003222 | 570 | 0 | 267 |
| claude-haiku | **policy** | 0.856 | 8.3% | 5.22 / 4.52 s | 0.003511 | 0.003511 | 411 | 0 | 244 |
| claude-sonnet-5 | control | 0.975 | 1.7% | 21.89 / 14.99 s | 0.022752 | 0.022752 | 2180 | 1104 | 43 |
| claude-sonnet-5 | **policy** | **0.971** | 3.3% | 20.72 / 13.73 s | 0.024014 | 0.024014 | 2027 | 1156 | 40 |
| gpt-4o | control | 0.880 | 8.3% | 4.65 / 4.29 s | 0.005529 | 0.005529 | 471 | 0 | 159 |
| gpt-4o | **policy** | 0.895 | 8.3% | 3.38 / 3.01 s | 0.006868 | 0.005660 | 351 | 0 | 130 |
| gpt-4o-mini | control | 0.808 | 13.3% | 4.13 / 3.71 s | 0.000356 | 0.000356 | 511 | 0 | 2269 |
| gpt-4o-mini | policy | 0.833 | 11.7% | 3.59 / 2.81 s | 0.000443 | 0.000363 | 402 | 0 | 1882 |
| mistral-small | control | 0.903 | 1.7% | 3.35 / 3.39 s | 0.000374 | 0.000335 | 530 | 0 | 2415 |
| mistral-small | policy | 0.795 | 0.0% | 2.29 / 1.95 s | 0.000390 | 0.000225 | 300 | 0 | 2041 |

xAI returns its own bill per call (`cost_in_usd_ticks`). Over 170 calls it
matched the computed cached cost exactly (ratio 1.0000), so xai-grok's "as
incurred" column is a billed figure. That column reflects five identical
repeats sent back to back, which flatters every automatic cache: it is what
this benchmark paid, not what production traffic would.

Sonnet costs **8.7×** xai-grok, 6.8× claude-haiku and 3.5× gpt-4o per task, and
is ~6× slower on average. Thinking is 57% of its output. No policy effect is
significant in any model on these tasks.

### Statistically established (policy arm, paired bootstrap over the 12 tasks)

| comparison | quality | cost/task | latency |
|---|---|---|---|
| claude-sonnet-5 − xai-grok | **+0.088** [+0.008, +0.180] | **+$0.02126** | **+17.4 s** |
| claude-sonnet-5 − claude-haiku | **+0.115** [+0.017, +0.265] | **+$0.02050** | **+15.5 s** |
| claude-sonnet-5 − gpt-4o | **+0.075** [+0.036, +0.119] | **+$0.01715** | **+17.3 s** |
| xai-grok − claude-haiku | +0.027 [−0.138, +0.226] n.s. | **−$0.00076** | **−1.9 s** |
| xai-grok − gpt-4o | −0.013 [−0.124, +0.094] n.s. | **−$0.00412** | +0.0 s n.s. |
| claude-haiku − gpt-4o | −0.040 [−0.170, +0.037] n.s. | **−$0.00336** | **+1.8 s** |
| xai-grok − gpt-4o-mini | +0.050 [−0.078, +0.188] n.s. | **+$0.00231** | −0.2 s n.s. |
| xai-grok − mistral-small | +0.087 [−0.050, +0.261] n.s. | **+$0.00236** | **+1.1 s** |

No hallucination-rate difference is significant.

Sensitivity — the five complete models on all 17 tasks, policy arm: xai-grok
0.845, gpt-4o 0.834, claude-haiku 0.807, gpt-4o-mini 0.771, mistral-small
0.757. None reaches 0.90, no quality difference between them is significant,
and chat (0.67–0.78) and work (0.38–0.83; claude-haiku 0.452) are weak for all.

### Per capability — where a routing decision has to be made

| class | tasks | xai-grok | claude-haiku | claude-sonnet-5 | gpt-4o | gpt-4o-mini | mistral-small |
|---|---|---|---|---|---|---|---|
| agent | 4 | **0.919** | 0.906 | 0.994 | 0.903 | 0.805 | 0.865 |
| code | 4 | 0.890 | **1.000** | 1.000 | 0.979 | 1.000 | 0.857 |
| research | 3 | 0.785 | 0.833 | 0.900 | 0.787 | 0.668 | 0.837 |
| work | 1 | 1.000 | 0.143 | 0.971 | 0.857 | 0.771 | 0.143 |
| chat (17-task run, no Sonnet) | 3 | 0.758 | 0.744 | — | 0.665 | 0.679 | 0.775 |

Against the current primary, xai-grok, **no candidate is significant in any
class**: Sonnet +0.075 [0.000, +0.156] in agent, +0.110 [0.000, +0.236] in code,
+0.115 [−0.100, +0.444] in research; claude-haiku +0.110 [0.000, +0.236] in code.
Three or four tasks cannot convict a model on one bad answer, and code's gap
is one task (`std_code_predict_output`, xai-grok 0.69). gpt-4o is WORSE than
Sonnet in agent (−0.091 [−0.179, −0.020]) and in research (−0.113 [−0.200,
−0.050]).

xai-grok's agent result replicates across both runs (0.905, 0.919). Its code
result straddles the bar (0.904, 0.890).

### Prompt caching, per provider

Probed the way production calls: one real assembled policy as the system
message, a different user message on each call, the provider's own usage
counters.

| model | mechanism | policy reused across different user messages | identical prompt repeated | what production gets |
|---|---|---|---|---|
| xai-grok | automatic | **no** — 128 tokens, xAI's own preamble; larger hits only on a prompt already seen | 97.9% | best effort; `x-grok-conv-id` did not change it |
| gpt-4o | automatic, ≥1,024 tokens | not observed | 0 of 4 in one probe, 6 of 6 at 86% in the next | best effort, unstable |
| claude-haiku-4-5 | `cache_control` only, minimum **4,096** tokens | **0, with or without `cache_control`** — the ~1,470-token policy is under the minimum | 95%, with a prefix above 4,096 tokens | cannot cache the policy at its current size |
| claude-sonnet-5 | `cache_control` only, minimum 1,024 | 0 without; 1,470 tokens (77–85%) with | — | nothing: production sends no `cache_control` |

Caching Sonnet's policy would save at most ~$0.0026 of its $0.0240 per standard
task (~11%): its cost is output, and thinking is output. Not a reason to add
the parameter now.

### The pre-registered rule's verdict — and why it is not implemented

Step 2 of the rule, which is tier-wide, produced: best economic model
**claude-sonnet-5**; xai-grok, claude-haiku and gpt-4o **LAB** ("below 0.90,
WORSE than claude-sonnet-5, which substitutes"). Step 3, per class, found no
justified localized route. Step 4 placed both low-tier references
significantly below Sonnet.

The step 2 verdict is recorded verbatim and **not** acted on:

1. **It pools capabilities.** It decides on the 12-task mean, and no class on
   its own shows a significant difference. Routing is per capability, and a
   capability must never be routed by an overall score — a constraint that
   preceded the rule. A tier-wide step was a flaw in the rule's design,
   recorded here rather than edited away after the fact.
2. **Sonnet's evidence is incomplete.** No chat task and one work task: "which
   substitutes" is shown on 12 of 17.
3. **It would leave one provider.** With xai-grok, claude-haiku and gpt-4o in
   LAB, every standard chain would be Anthropic only (claude-sonnet →
   claude-opus). The Anthropic account ran out of credit during this very run.
4. **It would multiply standard cost by ~8.7× and latency by ~6×**, including for
   capabilities where a cheaper model already clears the bar (agent:
   xai-grok 0.919).

### Decision: no routing or catalog change

| model | slot | verdict | why |
|---|---|---|---|
| xai-grok | 1 (START+) | **KEEP** | Cheapest in the tier ($0.00275 list, $0.00160 billed). Quality indistinguishable from claude-haiku and gpt-4o, overall and per class. Clears the bar on agent (replicated). Below it pooled (0.883; 0.845 on 17 tasks) — the open question below. |
| claude-haiku | 2 (START+) | **KEEP — fallback** | Not a substitute for Sonnet: significantly worse pooled (−0.115) and below the bar (0.856); ties it only on code. Weak on work (0.452, identical in both runs). Its value is a second provider for START. It cannot cache the policy. |
| claude-sonnet-5 | 3 (PRO+) | **KEEP** | The only model at or above 0.90 on the measured standard tasks in the policy arm (0.971), significantly better than every other model pooled, significant in no single class, at 8.7× xai-grok's cost. It earns its fallback slot; promotion is unproven. |
| gpt-4o | 4 (MAX/TEAM) | **KEEP — fallback** | Not needed for quality: no better than xai-grok or claude-haiku, WORSE than Sonnet in agent and research, at 2–2.5× their cost. Needed for availability: the only OpenAI model in the chain — without it MAX and TEAM fall back to claude-opus and run three Anthropic models. |

1. **Does Sonnet deserve the third slot?** Yes — it is the only model that
   clears the bar on these tasks.
2. **Can Haiku replace it?** No. It is significantly worse pooled, below the
   bar, and weak on work.
3. **Is Grok the better economic choice?** Among the three cheaper models, yes:
   22% cheaper per task than claude-haiku and 60% cheaper than gpt-4o, at
   statistically equal quality. It is not proven sufficient.
4. **Is gpt-4o necessary?** For provider diversity, yes. For quality, no.
5. **Redundancy?** claude-haiku and claude-sonnet-5 share a provider, so PRO's
   standard chain has two providers, not three. No removal is justified — no
   non-inferior substitute from another provider exists — but it is a real
   availability risk.
6. **Too expensive for its value?** claude-sonnet-5 would be, as a primary:
   $0.024 per task, and ~$0.08 and ~80 s on long planning. In third place it is
   paid only after two failures.

### Chains, before and after

| plan | before | after |
|---|---|---|
| FREE | not entitled (standard > low) | unchanged |
| START | xai-grok → claude-haiku | unchanged |
| PRO | xai-grok → claude-haiku → claude-sonnet | unchanged |
| MAX, TEAM | xai-grok → claude-haiku → claude-sonnet → gpt-4o | unchanged |

Identical for chat, research, files, work, code, automation and agent.
claude-opus is eligible but falls beyond every plan's attempt limit.

### What this measurement leaves open

- **REQUIRES A DECISION — the standard primary is not proven sufficient.**
  Pooled, only Sonnet clears 0.90, at 8.7× the cost. Before any promotion,
  complete Sonnet's five missing tasks and add enough standard tasks — about
  eight per class — for per-capability intervals to decide.
- **Complete the Sonnet run** once the Anthropic account has credit. This
  writes a `.resume-*.json` file that the analyzer reads beside the main one:
  `uv run python bench/run_policy_benchmark.py --suite=standard --only=claude-sonnet-5 --repeats=5 --tasks=std_work_expense_policy,std_work_bank_reconciliation,std_chat_customer_thread,std_chat_design_doc_retention,std_chat_asyncio_crash`
- **A hypothesis, not pre-registered:** xai-grok is not significantly better
  than gpt-4o-mini (+0.050) or mistral-small (+0.087) on these tasks, at 6–7× the
  cost. The cheaper standard models may not be buying quality over the low tier.
  REQUIRES A CALIBRATION DECISION, with more tasks.
- **Availability:** PRO's standard chain spans two providers.

---

## Per-class reading

Most cells score 1.00 in every model. Where everything is saturated, the
benchmark provides no evidence to prefer one model on quality, and "cheapest"
then just means "fewest tokens".

Policy arm, final run:

| class | gpt-4o-mini | mistral-small | claude-sonnet-5 | glm-5.3-flash | deepseek-flash | discriminating? |
|---|---|---|---|---|---|---|
| **agent** | 0.857 | **0.943** | **0.945** | 0.895 | 0.869 | **yes — produced the one routing change; Sonnet ties mistral-small** |
| chat | 1.000 | 1.000 | 1.000 | 0.973 | 1.000 | no — at ceiling |
| coding | 1.000 | 0.978 | 1.000 | 1.000 | 1.000 | no — at ceiling |
| research | 1.000 | 0.967 | 0.900 | 0.867 | 0.833 | partly |
| structured | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | no — at ceiling |
| long_context | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | no — at ceiling |
| tool | 1.000 | 1.000 | 1.000 | 0.883 | 0.889 | partly |
| image | 0.917 | 0.967 | 1.000 | 1.000 | 1.000 | partly |
| document (PDF) | 1.000 | UNSUPPORTED | 1.000 | UNSUPPORTED | UNSUPPORTED | capability fact, not a score |
| **uncertainty** | **0.700** | 0.570 | 0.507 | 0.460 | 0.486 | **yes — and everyone fails** |

### The agent class, in full

The same agent row as above, broken out by arm — a drill-down, not a second set
of figures.

| model | control | policy |
|---|---|---|
| claude-sonnet-5 | 0.926 | **0.945** |
| **mistral-small** | **0.943** | **0.943** |
| glm-5.3-flash | 0.848 | 0.895 |
| deepseek-flash | 0.779 | 0.869 |
| gpt-4o-mini | 0.686 | 0.857 |

mistral-small leads gpt-4o-mini by **+0.086** with the policy, at the same catalog
price ($0.15/$0.60) and the same quality class. claude-sonnet-5 matches
mistral-small (+0.002, interval [−0.155, +0.143]) at 21.5× the cost, which is why
mistral-small stays the agent primary.

One caveat that cuts against the mistral-small finding, so it is stated rather
than buried: `agent_tool_selection` graded models for naming a tool its prompt
never asked them to name. It penalised both arms equally, so the policy deltas
are unaffected, but **gpt-4o-mini's absolute agent score is understated** and the
true gap is narrower than 0.086. The phrasing is fixed for future runs.

---

## An unfixed failure, in every model

`uncertainty_nonexistent_api` asks for a PyTorch method that does not exist.
**With the policy, every model invented it on all 5 calls** — typically
`torch.autograd.flush_gradient_cache()`, confidently, with a plausible
signature. That includes claude-sonnet-5: premium capability did not prevent it,
and neither did the policy's evidence block. Without the policy the picture is
the same except for DeepSeek, which fabricated once and returned an empty answer
the other four times — silence, which scores zero, not honesty.

Instruction-level mitigation does not solve this failure mode, and
`uncertainty` is consequently the one class where nothing available is good
enough. Best class result anywhere: **0.700** (gpt-4o-mini + policy). Nothing
clears 0.90.

---

## RETRACTED: the "production cost leak" reported here was a measurement error

An earlier revision of this document claimed the Groq credential was dead
(`403 error code: 1010`) and that every START-plan chat was therefore costing
4-8x its intended price. **That claim was wrong and is retracted in full.**

Verified 2026-09-12 against the live provider:

| how the request was made | result |
|---|---|
| `urllib` with its default `Python-urllib/3.12` User-Agent | **403 `error code: 1010`** |
| `urllib` with `User-Agent: curl/8.4.0`, same key, same endpoint | **200 OK** |
| `litellm.acompletion()` — the path production actually uses | **200 OK** |
| `ModelRouter.complete()`, FREE plan, trivial prompt | **200 OK on `groq/openai/gpt-oss-20b`** |
| `ModelRouter.complete()`, START plan, trivial prompt | **200 OK on `groq/openai/gpt-oss-20b`** |

Cloudflare error 1010 is a *browser-signature* block, not an authentication
failure. Groq sits behind Cloudflare, and the bare `Python-urllib` User-Agent
is on its blocklist. The credential was valid the whole time; the audit's
probe tool was what got refused.

Two lessons worth keeping:

  * A 403 is not automatically an auth failure. The body said `1010`, which is
    a Cloudflare code, not a Groq API error — and that distinction was
    available in the original output and went unread.
  * A diagnostic must exercise the SAME client stack as production. Probing
    with `urllib` while production runs on litellm/httpx measured the probe,
    not the system.

There is no cost leak. FREE and START ultra_low requests reach
`groq-llama4-scout` exactly as the routing intends, at $0.075/$0.30.

---

## Local inference

Both paid LAB candidates are MIT, so self-hosting is *legally* clear. It is not
practical.

| | params | checkpoint | realistic hardware |
|---|---|---|---|
| GLM-5.3-Flash | 320B-A18B MoE | ~200 GB at Q4_K_M | ~205 GB @ 4K ctx, ~338 GB @ 128K. 1× H200 (141 GB) only at aggressive quant; 8× H100/H200 for the FP8 checkpoint |
| DeepSeek V4.1-Flash | 552B + 196B Engram | 510 GB | ~7× H100 or 4× H200. Runtime support immature: no `deepseek_v41` in llama.cpp mainline, vLLM PRs unmerged |

MoE saves compute, not memory — all experts sit resident because the router
cannot know which will fire until it sees the token. An 8×H100 node is roughly
$25–30k/month rented. Against $0.15/1M via API, local inference is not a cost
strategy at any plausible volume; it is a data-residency or latency strategy.

**Verdict: not viable now.** Revisit only if a much smaller MIT model clears
the quality bar for a specific class.

---

## Recommendation and substitution map

| model | status | verdict | reasoning |
|---|---|---|---|
| gpt-4o-mini | ACTIVE | **KEEP — generalist** | 0.927 overall with the policy, a statistically established policy effect, PDF support, and the best result anywhere on `uncertainty` (0.700). Primary for chat, code, research, files, work, automation. |
| mistral-small | ACTIVE | **KEEP — agent specialist** | 0.943 on the agent class against gpt-4o-mini's 0.857 at identical price and quality class; ties claude-sonnet-5 there at 1/21 of the cost. The cheapest ($0.00024) and fastest (0.87 s) model measured. No PDF support. Primary for `capability="agent"` only. |
| claude-sonnet-5 | ACTIVE | **KEEP EXACTLY AS IS — premium, standard tier** | Low complexity: statistically equal to both incumbents in every class, 14–21× their cost, 1.5–1.9 s slower, no quality gain from the policy. Standard suite (12 tasks, policy arm): the only model above 0.90 (0.971), significantly better than every other model pooled but in no single capability, at 8.7× xai-grok's cost. Keeps its third slot; promotion unproven. |
| xai-grok | ACTIVE | **KEEP — standard primary** | Cheapest standard model per task ($0.00275; xAI's own bill confirms the cached cost). Quality indistinguishable from claude-haiku and gpt-4o; clears 0.90 on agent, not pooled (0.883). |
| claude-haiku | ACTIVE | **KEEP — standard fallback** | The second provider on START. Significantly worse than Sonnet pooled, weak on work (0.452), and unable to cache the policy (4,096-token minimum). |
| gpt-4o | ACTIVE | **KEEP — standard fallback, for availability** | The only OpenAI model in the standard chain. No quality advantage over the cheaper standard models; WORSE than Sonnet in agent and research. |
| groq-llama4-scout | ACTIVE | **KEEP** | Functional. Cheapest entry in the catalog at $0.075/$0.30, serving FREE and START ultra_low requests exactly as the routing intends. |
| GLM-5.3-Flash | **LAB** | **DO NOT PROMOTE — measured** | 0.880 overall (−0.047 vs gpt-4o-mini), 1.24× the cost per task, 7× the latency (8.7 s), and the only model whose answers the policy made longer. The paper case (cached input 2.5× cheaper, 1M ctx, video, MIT) is real but not what the router optimises for. |
| DeepSeek V4.1-Flash | **LAB** | **DO NOT PROMOTE — measured** | 0.880 overall (−0.047), 2.33× the cost per task at peak, and 6 empty answers in the control arm. Only compelling on cached input (25× off-peak) with off-peak scheduling, neither of which exists. |
| GLM-4.7-Flash | **LAB** | **UNMEASURED — THROUGHPUT-BLOCKED** | No score exists, and no score is implied. The free tier refused 2 of 5 back-to-back probes, so the run could not complete. Absence of evidence, not evidence of inferiority. |

**Substitution map — what the benchmark actually authorised:**

| from | to | class | authorised? |
|---|---|---|---|
| gpt-4o-mini | mistral-small | `capability="agent"` only | **YES — implemented** (commit `f9e96dc`) |
| gpt-4o-mini | mistral-small | chat, code, research, document | **NO** — at ceiling, or gpt-4o-mini ahead; no evidence |
| gpt-4o-mini or mistral-small | claude-sonnet-5 | any low-complexity class | **NO** — no significant quality difference in any class, 14–21× the cost, slower |
| claude-sonnet-5 | gpt-4o-mini or mistral-small | the standard tier | **NO** — on the standard suite both are significantly worse than Sonnet (−0.138, −0.175) |
| xai-grok | claude-sonnet-5 as standard primary | any standard capability | **NO** — significant only pooled, in no single class; 8.7× the cost; Sonnet's run incomplete |
| xai-grok, claude-haiku, gpt-4o | removal from standard routing (LAB) | the standard tier | **NO** — the pre-registered step 2 verdict; it pools capabilities and would leave Anthropic as the only standard provider |
| claude-sonnet-5 | claude-haiku | standard slot 3 | **NO** — significantly worse pooled, below the bar |
| gpt-4o | removal | MAX/TEAM standard slot 4 | **NO** — the only OpenAI fallback; no non-inferior substitute from another provider |
| gpt-4o-mini | GLM-5.3-Flash | any | **NO** — worse and more expensive per task |
| gpt-4o-mini | DeepSeek | any | **NO** — worse and 2.33× the cost |
| anything | GLM-4.7-Flash | any | **NO** — never measured |

Nothing in LAB was promoted. `trinity_router.lab.LAB_CATALOG` is a separate
registry that routing cannot read, isolated structurally rather than by a flag
and pinned by `packages/model-router/tests/test_lab_isolation.py`.

### LAB economics, corrected — analysis only, nothing promoted

With reasoning billed once and silence scored zero, the policy-arm cost per
task on the low-complexity suite is **$0.0004554** for GLM-5.3-Flash (1.24×
gpt-4o-mini) and **$0.0008580** for DeepSeek V4.1-Flash at peak (2.33×), both at
quality 0.880 — worse and dearer than the incumbents there. The substitution
map stands.

The standard tier changes the arithmetic, not the verdict. Standard models
cost $0.00275 (xai-grok) to $0.02401 (claude-sonnet-5) per task. Priced at
xai-grok's measured standard token profile (1,508 input, 347 visible output)
plus each LAB model's own reasoning volume from the main suite (~432 tokens
GLM, ~257 DeepSeek), GLM would cost ~$0.0006 and DeepSeek ~$0.0012 per standard
task: ~4.5× and ~2.3× below xai-grok, ~39× and ~20× below Sonnet. A
projection, not a measurement.

**REQUIRES NEW LAB EXPERIMENT:** run both on the standard suite. The economic
case exists only if either clears 0.90 there — neither did on the easier suite
— and GLM's 8.7 s mean latency and DeepSeek's empty answers are known risks.
No production change follows from this.

---

## The distinctions this document exists to preserve

Each of these is written down because misreading it already produced a wrong
conclusion once.

| distinction | why it matters here |
|---|---|
| **overall score ≠ per-capability decision** | mistral-small *loses* overall (0.921 vs 0.927) and *wins* agent (0.943 vs 0.857). Only the second became a routing change. A model can be second-best everywhere and still be the right primary somewhere. |
| **benchmark ≠ promotion** | Being measured, even favourably, promotes nothing. GLM and DeepSeek were measured and remain in LAB. |
| **absence of evidence ≠ inferiority** | GLM-4.7-Flash has no number — not a low score, no score. Likewise the standard-complexity regime: one task, so nothing is known about it either way. |
| **measured regime ≠ production regime** | 38 of 39 tasks are low-complexity, where production never routes Sonnet. "Sonnet shows no advantage here" does not mean "Sonnet shows no advantage where it is used". |
| **list price ≠ cost per task** | GLM lists cheaper output than gpt-4o-mini and costs 1.24× more per task, because most of its output is reasoning. |
| **LAB ≠ production** | Structural isolation, not a flag. A model in `LAB_CATALOG` cannot be selected by any code path. |
| **pooled significance ≠ capability significance** | On the standard suite Sonnet beats every other model when the 12 tasks are pooled, and beats none significantly inside any single class. Routing is per capability, so only the second fact can move a route. |
| **benchmarked ≠ ACTIVE** | Eight models were benchmarked. Six are ACTIVE (gpt-4o-mini, mistral-small, claude-sonnet-5, xai-grok, claude-haiku, gpt-4o); two are in LAB and unreachable by routing. |
| **an unreachable alias ≠ a retired model** | `gemini-25-flash` was removed for being unreachable; `gemini/gemini-2.5-flash` is retired and stays on the denylist. Different facts, different remedies. |

---

## Consistency with the code (verified 2026-09-13)

Checked against the live `PROVIDER_CATALOG`, `LAB_CATALOG` and router, so this
document can be trusted as a description of the system and not only of a run.
Everything below is the state as it stands today.

| claim in this document | what the code says | status |
|---|---|---|
| mistral-small is the primary for `capability="agent"` | `capability_priority=(("agent", 35),)` on the mistral-small entry | consistent |
| gpt-4o-mini is the generalist primary | first candidate for chat, code, research, files, work, automation | consistent |
| claude-sonnet-5 serves the standard tier only | alias `claude-sonnet`, $2.00/$10.00, `quality_class="standard"`; third in standard-floor chains for PRO/MAX/TEAM agent, code and automation; never a dynamic primary; primary on the legacy selector path | consistent — unchanged by this decision |
| the benchmark measures what production sends to Sonnet | the router sends no `temperature`, no `thinking` and no `cache_control`: adaptive thinking, uncached | consistent |
| the standard chain | FREE not entitled; START xai-grok → claude-haiku; PRO + claude-sonnet; MAX/TEAM + gpt-4o — identical for all seven capabilities, simulated with the real router | consistent — unchanged by this decision |
| the standard suite runs at a production ceiling | `standard_output_ceiling()` reads `PLAN_LIMITS["pro"].max_output_tokens_per_request` (8,192) | consistent |
| claude-haiku and xai-grok serve routing | both sit in every standard chain; `providers.py` and `selector.py` comments still call claude-haiku "reserved for intent classifier" and xai-grok "not yet in any chain" — true of the legacy selector only | routing consistent; **comments stale** |
| GLM-5.3, GLM-4.7 and DeepSeek are LAB only | all three in `LAB_CATALOG`, none in `PROVIDER_CATALOG`; isolation pinned by `test_lab_isolation.py` | consistent |
| Groq is functional | `groq-llama4-scout` → `groq/openai/gpt-oss-20b` at $0.075/$0.30, reachable on FREE and START | consistent |
| cached input is priced | gpt-4o-mini $0.075, mistral-small $0.015; claude-sonnet-5 has no cached price, because production never enables its cache | consistent |
| the routable Google model | logical alias `gemini-25-flash-lite` → model_id `gemini/gemini-3.5-flash-lite` | consistent |
| no retired or LAB model reachable | checked across every plan and capability, low and standard complexity | consistent |
| catalog size | 9 entries | consistent |

**The three Gemini states, kept distinct** — they have been confused before,
and each carries a different remedy:

| | example | meaning |
|---|---|---|
| logical alias | `gemini-25-flash-lite` | the internal key routing and `selector.py` refer to. Its name is historical and deliberately not renamed. |
| real model_id | `gemini/gemini-3.5-flash-lite` | what is actually sent to the provider. This is the value that must be correct. |
| retired model | `gemini/gemini-2.5-flash` | a model_id the provider 404s on. Listed in `RETIRED_MODEL_IDS` so it cannot re-enter the catalog. |

A fourth state existed briefly and no longer does: the alias `gemini-25-flash`
was **removed** from the catalog on 2026-09-13 for being unreachable by cost
rank — not retired, and not broken.

---

## Limitations

- **The main suite is low-complexity.** 38 of 39 tasks score `ultra_low` or
  `low`. The `standard` regime has its own suite, with its own limits below.
- **Standard suite: 3–4 tasks per class.** Per-capability intervals are wide;
  one bad task cannot convict a model, and one good one cannot promote it.
- **Sonnet's standard run is incomplete.** 47 calls were lost to an exhausted
  credit balance; chat is untested for it, and the tier is analysed on 12 tasks.
- **Temperature 0 is not deterministic** for OpenAI, xAI or Mistral: class means
  moved by up to 0.26 between two identical runs. claude-haiku was deterministic.
- **The standard ceiling is PRO's.** MAX and TEAM allow 16,384 output tokens, so
  Sonnet's six truncated calls there might finish — at even higher cost.
- **Sonnet runs at default sampling.** It rejects `temperature`; the other
  models run at 0.
- **Small classes.** 3–7 tasks per class. The intervals are wide, and a modest
  real difference inside one class could go undetected.
- **GLM-4.7-Flash was never called.** Throughput-blocked, not measured.
- **Reasoning effort was not varied.** GLM-5.3-Flash, DeepSeek and Sonnet expose
  it; gpt-4o-mini rejects it (`400 Unrecognized request argument`).
- **No video task.** The only candidate claiming video is GLM.
- **Most cells are at ceiling.** The measured effects come from the agent,
  uncertainty and research classes.
- **One unfair criterion.** `agent_tool_selection` understates gpt-4o-mini's
  absolute agent score. Fixed for future runs.
- **Tool calling is measured as selection and reporting**, not live execution.
- **Costs are at list price.** The economy runs predate per-call cache tracking,
  so their production cost is lower than shown; the caching figures above are a
  projection, not a measurement of production traffic.
