"""The benchmark's cost and scoring arithmetic.

Two measurement faults were found here on 2026-09-13, both of the kind that
produce a confident, wrong economic finding without any error:

1. Reasoning tokens were added ON TOP of the completion count, although every
   provider that reports them already includes them in that count. Reasoning
   models were billed twice for thinking, overstating their cost per task.
2. An empty answer was scored like any other, so silence passed every
   "does not say X" criterion.

The usage blocks below are the real shapes returned by the live APIs when the
fault was diagnosed. No model is called.
"""
from __future__ import annotations

import pytest

from bench.analyze_model_comparison import is_empty, normalise, paired_bootstrap
from bench.policy_benchmark.runner import (
    CallResult,
    ModelSpec,
    call_cost_usd,
    is_truncated,
    parse_anthropic_response,
    parse_openai_response,
    run,
)
from bench.run_policy_benchmark import OFFICIAL_RESULTS, results_path_for

SPEC = ModelSpec(key="m", endpoint="https://example.invalid", model_id="m",
                 api_key_env="K", usd_per_1m_input=1.0, usd_per_1m_output=10.0)


# ── Reasoning is inside the completion count, never added to it ───────────

def test_glm_reasoning_is_not_billed_twice():
    body = {"choices": [{"message": {"content": "391"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 204, "total_tokens": 234,
                      "completion_tokens_details": {"reasoning_tokens": 67},
                      "prompt_tokens_details": {"cached_tokens": 0}}}
    call = parse_openai_response(body, 1.0)
    assert call.output_tokens == 204 and call.reasoning_tokens == 67
    # total_tokens == prompt + completion proves reasoning is already inside.
    assert call.input_tokens + call.output_tokens == body["usage"]["total_tokens"]
    assert call_cost_usd(SPEC, call) == pytest.approx((30 * 1.0 + 204 * 10.0) / 1e6)


def test_deepseek_reasoning_is_not_billed_twice_and_cache_hits_are_read():
    body = {"choices": [{"message": {"content": "391"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 48, "completion_tokens": 58, "total_tokens": 106,
                      "completion_tokens_details": {"reasoning_tokens": 56},
                      "prompt_cache_hit_tokens": 32, "prompt_cache_miss_tokens": 16}}
    call = parse_openai_response(body, 1.0)
    assert call.cached_input_tokens == 32
    assert call_cost_usd(SPEC, call) == pytest.approx((48 * 1.0 + 58 * 10.0) / 1e6)


def test_anthropic_thinking_is_inside_output_tokens():
    body = {"stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "391"}],
            "usage": {"input_tokens": 50, "output_tokens": 115,
                      "output_tokens_details": {"thinking_tokens": 57},
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    call = parse_anthropic_response(body, 1.0)
    assert call.text == "391", "thinking blocks must never leak into the scored answer"
    assert call.reasoning_tokens == 57
    assert call_cost_usd(SPEC, call) == pytest.approx((50 * 1.0 + 115 * 10.0) / 1e6)


def test_anthropic_input_tokens_count_the_cached_parts_too():
    """Anthropic's input_tokens is only the uncached remainder. Left alone it
    would make a cached call look like a 10-token prompt next to OpenAI's
    prompt_tokens, which is the full size."""
    body = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}
    call = parse_anthropic_response(body, 1.0)
    assert call.input_tokens == 1210
    assert call.cached_input_tokens == 1000
    assert call.cache_write_tokens == 200


def test_openai_cached_tokens_are_read():
    body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1082, "completion_tokens": 7,
                      "prompt_tokens_details": {"cached_tokens": 1024}}}
    assert parse_openai_response(body, 1.0).cached_input_tokens == 1024


@pytest.mark.parametrize("reason,truncated", [
    ("max_tokens", True), ("length", True), ("end_turn", False), ("stop", False), ("", False),
])
def test_truncation_is_recognised_on_both_dialects(reason, truncated):
    assert is_truncated(reason) is truncated


# ── Silence is not a pass ─────────────────────────────────────────────────

def test_an_empty_answer_scores_zero_and_claims_no_hallucination(monkeypatch):
    monkeypatch.setattr("bench.policy_benchmark.runner._read_env", lambda *a, **k: {"K": "x"})
    monkeypatch.setattr("bench.policy_benchmark.runner.call_model",
                        lambda *a, **k: CallResult("   ", 100, 40, 0.5, reasoning_tokens=40))
    from bench.policy_benchmark import runner
    one_task = runner.TASKS[:1]
    monkeypatch.setattr("bench.policy_benchmark.runner.TASKS", one_task)

    outcomes = run([SPEC], repeats=1, out_dir="bench/results")
    assert outcomes
    for o in outcomes:
        assert o.empty is True
        assert o.quality == 0.0
        assert o.hallucinated is False
        assert all(v is False for v in o.passed.values())


def test_the_analysis_applies_the_same_rule_to_rows_written_before_it_existed():
    legacy = {"model_key": "glm-5.3-flash", "arm": "policy", "task_id": "t", "category": "tool",
              "input_tokens": 1000, "output_tokens": 300, "reasoning_tokens": 300,
              "quality": 0.5, "hallucinated": False, "cost_usd": 999.0, "latency_s": 1.0}
    assert is_empty(legacy)
    fixed = normalise(legacy)
    assert fixed["quality"] == 0.0
    # The stored, double-counted cost is replaced by the recomputed one.
    assert fixed["cost_usd"] == pytest.approx((1000 * 0.15 + 300 * 0.50) / 1e6)


# ── Statistics and file safety ────────────────────────────────────────────

def test_the_bootstrap_is_reproducible_and_honest_about_a_constant_gap():
    a = {f"t{i}": 0.5 for i in range(10)}
    b = {f"t{i}": 0.6 for i in range(10)}
    first, second = paired_bootstrap(a, b), paired_bootstrap(a, b)
    assert first == second
    assert first["delta"] == pytest.approx(0.1)
    assert first["lo"] == pytest.approx(0.1) and first["hi"] == pytest.approx(0.1)


def test_the_bootstrap_only_pairs_shared_tasks():
    bs = paired_bootstrap({"a": 1.0, "b": 1.0}, {"b": 0.0, "c": 0.0})
    assert bs["n"] == 1 and bs["delta"] == pytest.approx(-1.0)


@pytest.mark.parametrize("delta,lo,hi,n,expected", [
    (+0.10, +0.02, +0.18, 10, "MATERIALLY BETTER"),
    (+0.03, +0.01, +0.05, 10, "BETTER, NOT MATERIAL"),
    (-0.08, -0.15, -0.02, 10, "WORSE"),
    (+0.09, -0.01, +0.20, 10, "NO SIGNIFICANT DIFFERENCE"),
    (+0.30, +0.10, +0.50, 2, "INCONCLUSIVE"),
])
def test_the_pre_registered_verdicts(delta, lo, hi, n, expected):
    from bench.analyze_model_comparison import classify
    assert classify({"delta": delta, "lo": lo, "hi": hi, "n": n}) == expected


def test_paying_more_is_justified_only_when_quality_is_not_already_sufficient():
    """A material gain over an incumbent that already clears the bar buys
    nothing TRINDATRON needs — sufficient quality is already paid for."""
    from bench.analyze_model_comparison import cost_justified
    assert cost_justified("MATERIALLY BETTER", incumbent_quality=0.70) is True
    assert cost_justified("MATERIALLY BETTER", incumbent_quality=0.95) is False
    assert cost_justified("BETTER, NOT MATERIAL", incumbent_quality=0.70) is False
    assert cost_justified("NO SIGNIFICANT DIFFERENCE", incumbent_quality=0.50) is False


def test_the_caching_projection_only_discounts_the_policy_prefix():
    from bench.analyze_model_comparison import projected_cached_cost_usd
    row = {"model_key": "claude-sonnet-5", "input_tokens": 1500, "output_tokens": 100}
    # 1400 policy tokens over a 100-token control: those 1400 bill at the
    # cached rate ($0.20/1M), the remaining 100 and all output at list.
    assert projected_cached_cost_usd(row, control_input_tokens=100) == pytest.approx(
        (100 * 2.00 + 1400 * 0.20 + 100 * 10.00) / 1e6)
    assert projected_cached_cost_usd(
        {"model_key": "glm-5.3-flash", "input_tokens": 1, "output_tokens": 1}, 0) is None


def test_a_single_model_run_can_never_overwrite_the_official_results():
    for key in ("claude-sonnet-5", "gpt-4o-mini", "glm-5.3-flash"):
        assert results_path_for(key) != OFFICIAL_RESULTS
    assert results_path_for("glm-5.3-flash") == "bench/results/model_glm-5_3-flash.json"
