"""The benchmark harness: same model, same input, policy on versus policy off.

Methodology
-----------
The only difference between arm A and arm B is whether the assembled
TRINDATRON system message is sent. The user message, the model, the
temperature, the token ceiling and the task list are identical, and both arms
run in the same process against the same live endpoints. Arm A is not "no
system message at all" — it is the minimal, neutral system message a product
would otherwise send, because comparing a policy against *nothing* would
measure the presence of any instruction rather than the value of this one.

Determinism
-----------
temperature=0 wherever the provider accepts it, and every criterion is a
deterministic function of the response text. Models still vary run to run,
which is why REPEATS defaults above one and the reported figure is a mean
across repeats — a single sample per cell cannot distinguish a real effect
from sampling noise.

Cost
----
Token counts come from the provider's own usage block, never from an estimate,
and cost is computed from the catalog prices already validated in
trinity_router.providers. That keeps the economic claim in the same units the
rest of the product bills in. See call_cost_usd() for the one accounting rule
that was once wrong.

This module lives under bench/ and is imported by nothing in src/. It is an
experiment, not a runtime dependency, and adds no provider to production.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from bench.policy_benchmark.tasks import TASKS, BenchTask

#: The neutral control. A bare, honest product instruction with none of the
#: policy's behavioural structure — what a team ships before thinking about
#: this problem, and therefore the baseline worth beating.
CONTROL_SYSTEM = "You are TRINDATRON, a helpful AI assistant."

#: Extra completion budget granted on top of each task's own cap, so a model
#: that thinks before answering is not judged on a truncated reply.
#:
#: Measured 2026-09-12 and the reason this exists: GLM-5.3-Flash counts its
#: reasoning inside max_tokens, and on a 400-token cap it spent 399 of them
#: thinking and returned an EMPTY answer — scoring 0.40 on a task it scores
#: 1.00 on with room. DeepSeek-Flash hit its cap on 42.5% of calls. The
#: non-reasoning incumbents hit it on 1.8% and 4.4%, so the caps were
#: silently penalising exactly the models under evaluation.
#:
#: Granting headroom costs nothing for a model that stops early — it emits a
#: stop token regardless — and it does not weaken any brevity criterion,
#: because those grade the ANSWER TEXT with word counts, not the cap.
REASONING_HEADROOM = 2000

#: Samples per (model, arm, task). Above one so a difference can be told apart
#: from sampling noise; small enough to keep a full sweep affordable.
REPEATS = 3

#: Provider stop reasons that mean "the ceiling cut the answer off". A
#: truncated reply is recorded as such, because a score computed on half an
#: answer is a finding about the token cap, not about the model.
_TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "length"})


@dataclass(frozen=True)
class ModelSpec:
    """A model reachable right now, with the price the catalog already carries."""

    key: str
    endpoint: str
    model_id: str
    api_key_env: str
    usd_per_1m_input: float
    usd_per_1m_output: float
    #: Anthropic's Messages API differs enough in shape to need its own branch.
    dialect: str = "openai"
    #: Modalities this model accepts. A task needing one that is absent is
    #: recorded UNSUPPORTED, never scored zero.
    modalities: frozenset[str] = frozenset({"text"})
    #: Where the provider documents time-of-day pricing, the off-peak factor.
    #: Reported, never silently applied — the headline figure stays pessimistic.
    offpeak_multiplier: float = 1.0
    #: False only for a model that REJECTS `temperature` (claude-sonnet-5
    #: answers HTTP 400). Every model that accepts it runs at 0 — including
    #: claude-haiku-4-5 on the Anthropic dialect, verified live 2026-09-13 —
    #: so the one sampling asymmetry is the one the provider forces.
    accepts_temperature: bool = True
    #: CP12: True only for the two Groq models. CP11 measured Groq's own
    #: server-side validation rejecting the model's OWN response with HTTP 400
    #: ("Tool choice is none, but model called a tool") when a prompt looked
    #: tool-shaped and this harness — which never sends a `tools` schema for
    #: ANY model — simply omitted the key, exactly like every other provider
    #: here always has. Scoped to Groq only: the other five providers
    #: (OpenAI, Mistral, Z.ai, DeepSeek, xAI) have run this same omission for
    #: months without it, and router.py's own production code documents `tools`
    #: omitted (None) as the universally-safe value for all of them.
    declare_empty_tools: bool = False


@dataclass
class CallResult:
    text: str
    #: TOTAL prompt tokens, cached or not, on every dialect. Anthropic reports
    #: the uncached remainder separately from cache reads and writes; they are
    #: summed here so the field means the same thing for every provider.
    input_tokens: int
    #: Total completion tokens as billed. On every provider measured this
    #: ALREADY includes reasoning/thinking tokens — see call_cost_usd().
    output_tokens: int
    latency_s: float
    error: str = ""
    #: The reasoning/thinking share of output_tokens. A breakdown, never an
    #: addend.
    reasoning_tokens: int = 0
    #: Portion of input_tokens the provider served from its prompt cache.
    cached_input_tokens: int = 0
    #: Anthropic only: portion of input_tokens written to the cache this call.
    cache_write_tokens: int = 0
    stop_reason: str = ""
    #: HTTP attempts the call took; 1 when the first try returned. Failed
    #: attempts are not billed, but they are counted so a flaky endpoint is
    #: visible instead of averaged away.
    attempts: int = 1
    #: The provider's OWN billed cost, when it reports one. xAI returns
    #: usage.cost_in_usd_ticks (1 tick = 1e-10 USD — verified 2026-09-13: a
    #: 203-token prompt with 128 cached tokens and 1 output token billed
    #: 1,218,500 ticks, exactly $1.25/$0.20/$2.50 per 1M). Used to audit
    #: call_cost_usd(), never instead of it.
    provider_cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class TaskOutcome:
    task_id: str
    category: str
    arm: str
    model_key: str
    repeat: int
    passed: dict[str, bool] = field(default_factory=dict)
    hallucinated: bool = False
    quality: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    error: str = ""
    #: True when the model cannot accept this task's modality. Recorded
    #: separately from a failure: "cannot see images" is a capability fact,
    #: and averaging it into quality would make a text-only model look bad at
    #: reasoning.
    unsupported: bool = False
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""
    #: The token ceiling cut the reply off. Kept so truncation can be counted
    #: per model — the failure that invalidated a whole earlier run.
    truncated: bool = False
    #: A successful call whose visible answer was empty. Scored zero: silence
    #: satisfies every "does not say X" criterion, which is how an earlier run
    #: reported a 0.0% hallucination rate for a model that was mostly silent.
    empty: bool = False
    #: Length of the visible answer, as a number only. The text itself is
    #: never persisted.
    response_chars: int = 0
    attempts: int = 1
    provider_cost_usd: float | None = None
    #: Criteria whose checker RAISED. They score as failed, never as passed,
    #: and are named here so a checker bug is a visible, non-empty field
    #: rather than a quietly lower model score.
    checker_errors: list[str] = field(default_factory=list)
    #: First 16 hex chars of sha256(system message). Identical across models
    #: for the same task and arm, which is how "same policy" is verified from
    #: the rows themselves. A hash, never the text.
    system_sha256: str = ""
    #: Short excerpt, off by default. Stored ONLY when diagnosing a criterion
    #: that never passes in either arm (which usually means a broken checker,
    #: not a universal model failure). The persisted report keeps metrics
    #: only — see save_report().
    response: str = ""


def _read_env(path: str = ".env") -> dict[str, str]:
    """Credentials stay in the local env file and never reach a log, a result
    row, or the on-disk report."""
    env: dict[str, str] = {}
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _openai_user_content(text: str, attachments: tuple[str, ...]):
    """OpenAI-style multimodal content blocks.

    A plain string is sent when there are no attachments: some providers are
    stricter about the block form than others, and there is no reason to use
    it for a text-only call.
    """
    if not attachments:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}]
    for uri in attachments:
        if uri.startswith("data:application/pdf"):
            # OpenAI takes PDFs as a file block, not an image_url.
            blocks.append({
                "type": "file",
                "file": {"filename": "document.pdf", "file_data": uri},
            })
        else:
            blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


def _anthropic_user_content(text: str, attachments: tuple[str, ...]):
    if not attachments:
        return text
    blocks: list[dict] = []
    for uri in attachments:
        header, _, b64 = uri.partition(",")
        media_type = header.split(":", 1)[1].split(";", 1)[0]
        kind = "document" if media_type == "application/pdf" else "image"
        blocks.append({
            "type": kind,
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    blocks.append({"type": "text", "text": text})
    return blocks


def parse_openai_response(body: dict, latency_s: float) -> CallResult:
    """OpenAI-compatible Chat Completions (OpenAI, Mistral, Z.ai, DeepSeek)."""
    choices = body.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    # OpenAI, Mistral and Z.ai report prompt_tokens_details.cached_tokens;
    # DeepSeek also reports it and additionally prompt_cache_hit_tokens.
    cached = prompt_details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0
    ticks = usage.get("cost_in_usd_ticks")
    return CallResult(
        text,
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
        latency_s,
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        cached_input_tokens=int(cached),
        stop_reason=str(choices[0].get("finish_reason") or ""),
        provider_cost_usd=(int(ticks) / 1e10) if ticks is not None else None,
    )


def parse_anthropic_response(body: dict, latency_s: float) -> CallResult:
    """Anthropic Messages API.

    Only `text` blocks form the answer; `thinking` blocks are reasoning and
    are billed inside output_tokens. Anthropic's `input_tokens` is the
    UNCACHED remainder — total prompt size is input + cache_creation +
    cache_read — so the three are summed to keep input_tokens comparable with
    the OpenAI dialect, where prompt_tokens is already the total.
    """
    text = "".join(
        b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
    )
    usage = body.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    thinking = int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    return CallResult(
        text,
        int(usage.get("input_tokens", 0) or 0) + cache_read + cache_write,
        int(usage.get("output_tokens", 0) or 0),
        latency_s,
        reasoning_tokens=thinking,
        cached_input_tokens=cache_read,
        cache_write_tokens=cache_write,
        stop_reason=str(body.get("stop_reason") or ""),
    )


def call_cost_usd(spec: ModelSpec, call: CallResult) -> float:
    """List-price cost of one call: input at the input rate, output at the
    output rate. Nothing else is added.

    Reasoning tokens are NOT added on top of output_tokens. An earlier version
    of this harness did add them, and that double-counted reasoning for every
    model that reasons. Verified 2026-09-13 against the live APIs:

      glm-5.3-flash    completion 204, reasoning 67,  total = prompt + completion
      deepseek-flash   completion 58,  reasoning 56,  total = prompt + completion
      claude-sonnet-5  output 115, thinking 57 — with thinking disabled the same
                       prompt produced 82 output tokens for a longer answer

    so on all three the reasoning is already inside the completion count. The
    non-reasoning models (gpt-4o-mini, mistral-small) report zero reasoning
    and were never affected.

    This is deliberately the UNCACHED list price. The benchmark does not ask
    for caching, and a prompt-cache discount depends on traffic shape, so it
    is reported separately from these figures rather than folded into them.
    """
    return (
        call.input_tokens / 1e6 * spec.usd_per_1m_input
        + call.output_tokens / 1e6 * spec.usd_per_1m_output
    )


def is_truncated(stop_reason: str) -> bool:
    return stop_reason in _TRUNCATION_STOP_REASONS


def call_model(spec: ModelSpec, system: str, user: str, max_tokens: int,
               env: dict[str, str], attachments: tuple[str, ...] = ()) -> CallResult:
    key = env.get(spec.api_key_env, "")
    if not key:
        return CallResult("", 0, 0, 0.0, error=f"missing {spec.api_key_env}")

    if spec.dialect == "anthropic":
        # temperature=0 only where the model accepts it (spec.accepts_temperature).
        # claude-sonnet-5 rejects it outright — HTTP 400 "`temperature` is
        # deprecated for this model", re-verified 2026-09-13 — and top_p/top_k
        # are removed on the same model family, so there is no equivalent to
        # substitute. It runs at the provider's default sampling while every
        # other model, claude-haiku-4-5 included, runs at 0. That asymmetry is
        # unavoidable, is stated in the results, and works against the model
        # rather than for it: it carries sampling variance the others do not.
        #
        # No `thinking` parameter. On claude-sonnet-5, omitting it runs
        # ADAPTIVE thinking; on claude-haiku-4-5 it runs without extended
        # thinking. Production's litellm call sends no thinking parameter
        # either, so this is exactly what production gets. Measuring anything
        # else would measure a configuration nobody ships.
        payload = {
            "model": spec.model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {"role": "user", "content": _anthropic_user_content(user, attachments)}
            ],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "trindatron-bench/1.0",
        }
    else:
        payload = {
            "model": spec.model_id,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _openai_user_content(user, attachments)},
            ],
        }
        if spec.declare_empty_tools:
            payload["tools"] = []
            payload["tool_choice"] = "none"
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json",
                  # CP10: Groq's Cloudflare WAF returns a bare 403 (Cloudflare
                  # error 1010, a bot-signature block) for urllib's default
                  # User-Agent ("Python-urllib/3.x") — confirmed live
                  # 2026-09-19, every other provider in this file tolerates
                  # the default. A named UA fixes it without touching auth,
                  # payload or scoring; every other provider gets the same
                  # header for consistency, harmlessly.
                  "User-Agent": "trindatron-bench/1.0"}
    if spec.accepts_temperature:
        payload["temperature"] = 0

    req = urllib.request.Request(spec.endpoint, data=json.dumps(payload).encode(),
                                 headers=headers)
    # Bounded retry on transient failures. Free and low-tier endpoints
    # rate-limit hard —
    # GLM-4.7-Flash was measured refusing 2 of 5 back-to-back probes — and
    # without this a rate-limited model loses a large fraction of its cells
    # and gets compared on a much smaller sample than its rivals. Retrying a
    # refusal does not change what is measured, only how much of it survives;
    # every other status still fails immediately, and the wait is excluded
    # from the reported latency for the same reason.
    # Latency is the duration of the attempt that actually returned, never the
    # wall time across retries. An earlier version adjusted a running start
    # instant instead and produced NEGATIVE latencies (-70s was observed),
    # because the correction double-counted the backoff sleep.
    attempts, body, wait, call_started = 0, None, 4.0, time.monotonic()
    while True:
        attempts += 1
        call_started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", {}).get("message", "")[:120]
            except Exception:
                pass
            # 429 is an explicit rate limit. Z.ai also returns a bare 400
            # under load — measured: the identical request succeeding on a
            # manual retry seconds later — so transient 400s and 5xx get the
            # same bounded retry. A genuinely malformed request still fails,
            # it just fails five times first, which costs seconds and keeps a
            # flaky endpoint from being scored as a bad model.
            if e.code in (400, 429, 500, 502, 503, 529) and attempts < 5:
                time.sleep(wait)
                wait *= 1.7
                continue
            return CallResult("", 0, 0, time.monotonic() - call_started,
                              error=f"{e.code} {detail}", attempts=attempts)
        except Exception as e:  # noqa: BLE001
            return CallResult("", 0, 0, time.monotonic() - call_started,
                              error=type(e).__name__, attempts=attempts)

    latency = time.monotonic() - call_started
    if spec.dialect == "anthropic":
        result = parse_anthropic_response(body, latency)
    else:
        result = parse_openai_response(body, latency)
    result.attempts = attempts
    return result


def score(task: BenchTask, text: str) -> tuple[dict[str, bool], float, bool]:
    """Grade one response. Returns (per-criterion, quality in [0,1], hallucinated).

    Quality is the weighted fraction of criteria met, then reduced by a
    hallucination penalty. The penalty is applied on top of an already-failed
    criterion deliberately: inventing a figure is not merely "one point
    missing", it is the failure mode that makes an answer worse than no
    answer, and the score should say so.
    """
    passed, quality, hallucinated, _errors = score_with_errors(task, text)
    return passed, quality, hallucinated


def score_with_errors(task: BenchTask, text: str) -> tuple[dict[str, bool], float, bool, list[str]]:
    """score(), plus the names of criteria whose checker raised.

    A raising checker still scores as a failure — it must never pass — but a
    failure nobody can see is how a broken checker once read as a universal
    model failure. The names are returned so run() can record them.
    """
    results: dict[str, bool] = {}
    errors: list[str] = []
    earned = 0.0
    total = 0.0
    hallucinated = False
    for criterion in task.criteria:
        try:
            ok = bool(criterion.check(text))
        except Exception:  # noqa: BLE001 — a broken checker must not pass silently
            ok = False
            errors.append(criterion.name)
        results[criterion.name] = ok
        total += criterion.weight
        if ok:
            earned += criterion.weight
        elif criterion.is_hallucination_check:
            hallucinated = True

    quality = (earned / total) if total else 0.0
    if hallucinated:
        quality = max(0.0, quality - 0.25)
    return results, quality, hallucinated, errors


def build_policy_system(task: BenchTask) -> str:
    """Arm B's system message, assembled by the real product code.

    Imported from src so the experiment measures what ships. A benchmark that
    re-typed the policy would be testing a copy.
    """
    from src.prompting import TaskContext, ToolPolicy, assemble
    from src.prompting.tool_policy import ToolDescriptor

    if task.tools_available:
        tool_policy = ToolPolicy(
            tools=tuple(
                ToolDescriptor(name=n, requires_approval=n in task.approval_required)
                for n in task.tools_available
            )
        )
    else:
        tool_policy = ToolPolicy.none()

    return assemble(
        task_context=TaskContext(task_type=task.category),
        tool_policy=tool_policy,
    ).system


def build_control_system(task: BenchTask) -> str:
    """Arm A. The tool list is still described, in plain prose, so the two arms
    differ ONLY by the policy — otherwise arm B would be winning tool tasks
    merely by being the only arm that knew which tools existed."""
    if not task.tools_available:
        return CONTROL_SYSTEM
    line = " Available tools: " + ", ".join(task.tools_available) + "."
    if task.approval_required:
        line += " These require user approval: " + ", ".join(task.approval_required) + "."
    return CONTROL_SYSTEM + line


def run(models: list[ModelSpec], repeats: int = REPEATS,
        out_dir: str = "bench/results", keep_excerpts: bool = False,
        tasks: tuple[BenchTask, ...] | None = None,
        max_tokens: int | None = None) -> list[TaskOutcome]:
    """Sweep every (model, task, arm, repeat).

    `tasks` defaults to the main suite; the standard suite passes its own.
    `max_tokens`, when given, is the output ceiling for EVERY call, replacing
    task.max_tokens + REASONING_HEADROOM — the standard suite uses the
    production plan ceiling (see run_policy_benchmark.standard_output_ceiling).
    A task whose modality the model does not accept is recorded UNSUPPORTED
    and never sent — it would only buy a confusing error and a charge.
    """
    env = _read_env()
    outcomes: list[TaskOutcome] = []
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    task_list = TASKS if tasks is None else tasks

    for spec in models:
        if not env.get(spec.api_key_env):
            # No credential: every cell for this model is BLOCKED, stated once
            # rather than as forty identical errors. Nothing is invented.
            print(f"{spec.key:18s} BLOCKED — {spec.api_key_env} is not set; no calls made",
                  flush=True)
            for task in task_list:
                outcomes.append(TaskOutcome(
                    task_id=task.task_id, category=task.category, arm="blocked",
                    model_key=spec.key, repeat=0, error=f"missing {spec.api_key_env}",
                ))
            continue

        for task in task_list:
            if task.requires not in spec.modalities:
                for arm in ("control", "policy"):
                    outcomes.append(TaskOutcome(
                        task_id=task.task_id, category=task.category, arm=arm,
                        model_key=spec.key, repeat=0, unsupported=True,
                    ))
                print(f"{spec.key:18s} {'--':8s} {task.task_id:32s} "
                      f"UNSUPPORTED ({task.requires})", flush=True)
                continue

            for arm, system in (("control", build_control_system(task)),
                                ("policy", build_policy_system(task))):
                for repeat in range(repeats):
                    ceiling = max_tokens if max_tokens is not None else task.max_tokens + REASONING_HEADROOM
                    call = call_model(spec, system, task.prompt, ceiling, env, task.attachments)
                    outcome = TaskOutcome(
                        task_id=task.task_id, category=task.category, arm=arm,
                        model_key=spec.key, repeat=repeat,
                        attempts=call.attempts,
                        system_sha256=hashlib.sha256(system.encode("utf-8")).hexdigest()[:16],
                    )
                    if not call.ok:
                        outcome.error = call.error
                    elif not call.text.strip():
                        # Silence is not a pass. Every criterion is marked
                        # failed and no hallucination is claimed: nothing was
                        # said, so nothing was invented either.
                        outcome.empty = True
                        outcome.passed = {c.name: False for c in task.criteria}
                        outcome.quality = 0.0
                    else:
                        passed, quality, hallucinated, errors = score_with_errors(task, call.text)
                        outcome.passed = passed
                        outcome.quality = quality
                        outcome.hallucinated = hallucinated
                        outcome.checker_errors = errors
                        if keep_excerpts:
                            outcome.response = call.text[:300]
                    outcome.input_tokens = call.input_tokens
                    outcome.output_tokens = call.output_tokens
                    outcome.reasoning_tokens = call.reasoning_tokens
                    outcome.cached_input_tokens = call.cached_input_tokens
                    outcome.cache_write_tokens = call.cache_write_tokens
                    outcome.stop_reason = call.stop_reason
                    outcome.truncated = is_truncated(call.stop_reason)
                    outcome.response_chars = len(call.text)
                    outcome.latency_s = call.latency_s
                    outcome.cost_usd = call_cost_usd(spec, call)
                    outcome.provider_cost_usd = call.provider_cost_usd
                    outcomes.append(outcome)
                    print(
                        f"{spec.key:18s} {arm:8s} {task.task_id:32s} "
                        f"q={outcome.quality:.2f} h={outcome.hallucinated!s:5s} "
                        f"in={outcome.input_tokens:6d} out={outcome.output_tokens:4d} "
                        f"{outcome.latency_s:5.1f}s "
                        f"{'TRUNCATED ' if outcome.truncated else ''}"
                        f"{'EMPTY ' if outcome.empty else ''}"
                        f"{'CHECKER_ERROR=' + ','.join(outcome.checker_errors) + ' ' if outcome.checker_errors else ''}"
                        f"{'attempts=' + str(outcome.attempts) + ' ' if outcome.attempts > 1 else ''}"
                        f"{outcome.error}",
                        flush=True,
                    )
    return outcomes


def save_report(outcomes: list[TaskOutcome], path: str) -> None:
    """Persist METRICS ONLY.

    Model responses are deliberately not written to disk: a benchmark report
    is a record of measurements, and transcripts are the part most likely to
    carry content that should not sit in a repository. The per-criterion
    booleans preserve everything the analysis needs.
    """
    from dataclasses import asdict

    rows = []
    for o in outcomes:
        row = asdict(o)
        row.pop("response", None)
        rows.append(row)
    Path(path).write_text(json.dumps(rows, indent=1), encoding="utf-8")
