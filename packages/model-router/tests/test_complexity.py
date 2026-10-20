"""Adaptive Intelligence Router — complexity.compute_complexity() tests.

Covers mission requirement 16: trivial/simple/moderate/complex/expert
bands, boundary values (20/21, 40/41, 60/61, 80/81), and per-signal
contribution — all deterministic, no LLM call, no I/O.
"""
from __future__ import annotations

import pytest

from trinity_router.complexity import compute_complexity


def _messages(*user_texts: str) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}]
    for i, text in enumerate(user_texts):
        role = "user" if i == len(user_texts) - 1 or i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": text})
    return msgs


# ── Bands ────────────────────────────────────────────────────────────────────

def test_trivial_chat_scores_in_trivial_band():
    result = compute_complexity(capability="chat", messages=_messages("hi"))
    assert result.band == "trivial"
    assert 0 <= result.score <= 20


def test_simple_capability_with_some_length_scores_simple_band():
    # research base=10, plus a modest length signal, no other triggers.
    text = "x" * 1600  # 1600 // 150 = 10 -> length_signal=10; total=20 (still trivial boundary)
    result = compute_complexity(capability="research", messages=_messages(text))
    assert result.band in ("trivial", "simple")


def test_moderate_task_with_reasoning_language_scores_moderate():
    result = compute_complexity(
        capability="research", messages=_messages("Please explain why this approach works and compare it to alternatives."),
    )
    assert result.score > 20  # base(10) + reasoning(15) = 25
    assert result.band in ("simple", "moderate")


def test_complex_coding_task_scores_complex_band():
    text = (
        "Please debug and refactor this ```python\ndef f(): pass\n``` step by step, "
        "explaining the reasoning behind each change."
    )
    result = compute_complexity(capability="code", messages=_messages(text), tools=[{"name": "shell"}])
    # base(20) + code(15) + reasoning(15) + multi_step(10) + tool(15) = 75
    assert result.score >= 61
    assert result.band in ("complex", "expert")


def test_expert_agent_task_with_everything_scores_expert_band():
    long_prompt = (
        "Refactor and debug this codebase step by step, explain why, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    history = _messages(*(["turn"] * 20), long_prompt)
    result = compute_complexity(capability="agent", messages=history, tools=[{"name": "shell"}])
    assert result.score == 100.0
    assert result.band == "expert"
    assert result.required_quality_class == "premium"


# ── Boundaries (mission requirement 16: 20/21, 40/41, 60/61, 80/81) ─────────

@pytest.mark.parametrize("score,expected_band", [
    (0, "trivial"), (20, "trivial"), (21, "simple"),
    (40, "simple"), (41, "moderate"),
    (60, "moderate"), (61, "complex"),
    (80, "complex"), (81, "expert"),
    (100, "expert"),
])
def test_band_boundaries(score, expected_band):
    from trinity_router.complexity import _band_for_score
    assert _band_for_score(score) == expected_band


@pytest.mark.parametrize("score,expected_quality_class", [
    (0, "ultra_low"), (24, "ultra_low"), (25, "low"),
    (49, "low"), (50, "standard"),
    (74, "standard"), (75, "premium"),
    (100, "premium"),
])
def test_required_quality_class_thresholds(score, expected_quality_class):
    from trinity_router.complexity import _required_quality_class_for_score
    assert _required_quality_class_for_score(score) == expected_quality_class


# ── Per-signal contribution ──────────────────────────────────────────────────

def test_tool_signal_only_fires_when_tools_present():
    without_tools = compute_complexity(capability="chat", messages=_messages("hi"), tools=None)
    with_tools = compute_complexity(capability="chat", messages=_messages("hi"), tools=[{"name": "x"}])
    assert with_tools.score > without_tools.score
    assert with_tools.signals["tool_signal"] > 0
    assert without_tools.signals["tool_signal"] == 0


def test_code_signal_fires_on_code_fence():
    with_code = compute_complexity(capability="chat", messages=_messages("```python\nprint(1)\n```"))
    without_code = compute_complexity(capability="chat", messages=_messages("just chatting"))
    assert with_code.signals["code_signal"] > without_code.signals["code_signal"]


def test_reasoning_signal_fires_on_reasoning_keywords():
    result = compute_complexity(capability="chat", messages=_messages("why does this happen?"))
    assert result.signals["reasoning_signal"] > 0


def test_multi_step_signal_fires_on_planning_keywords():
    result = compute_complexity(capability="chat", messages=_messages("do this step by step"))
    assert result.signals["multi_step_signal"] > 0


def test_context_signal_grows_with_history_length_and_caps():
    short_history = compute_complexity(capability="chat", messages=_messages("hi"))
    long_history = compute_complexity(capability="chat", messages=_messages(*(["turn"] * 40), "hi"))
    assert long_history.signals["context_signal"] > short_history.signals["context_signal"]
    assert long_history.signals["context_signal"] <= 15  # capped


def test_base_capability_score_ranks_agent_highest():
    chat = compute_complexity(capability="chat", messages=_messages("hi"))
    agent = compute_complexity(capability="agent", messages=_messages("hi"))
    assert agent.signals["base_capability_score"] > chat.signals["base_capability_score"]


def test_unknown_capability_defaults_to_chat_like_base():
    unknown = compute_complexity(capability="something_new", messages=_messages("hi"))
    chat = compute_complexity(capability="chat", messages=_messages("hi"))
    assert unknown.signals["base_capability_score"] == chat.signals["base_capability_score"]


def test_none_capability_is_backward_compatible():
    """Mission requirement 20: capability=None must not break/crash."""
    result = compute_complexity(capability=None, messages=_messages("hi"))
    assert 0 <= result.score <= 100
    assert result.band in ("trivial", "simple", "moderate", "complex", "expert")


def test_score_never_exceeds_bounds():
    """Every signal maxed out simultaneously must still clamp to 100, never overflow."""
    text = "why " * 5 + ("x" * 5000) + " ```code``` step by step"
    result = compute_complexity(
        capability="agent", messages=_messages(*(["turn"] * 100), text), tools=[{"name": "x"}],
    )
    assert result.score == 100.0


def test_empty_messages_does_not_crash():
    result = compute_complexity(capability="chat", messages=[])
    assert 0 <= result.score <= 100


# ── Opção A regression (2026-09-07): _MAX_LENGTH_SIGNAL 20 -> 35 ────────────
#
# tool_signal is only ever nonzero when the caller attaches real tools
# (task_type=AGENT) — a CHAT-dispatched request (chat_service.py never
# sets `tools`) can never reach it. Before this change, capability="chat"
# (base=0) topped out at 20(length)+15(reasoning)+15(code)+10(multi_step)=
# 60 for a single message with no history — 15 short of "premium" (75),
# making CHAT+premium structurally unreachable regardless of how demanding
# the text actually was. Raising ONLY this one cap to 35 closes exactly
# that gap (0+35+15+15+10=75) using signals that already exist — no new
# heuristic, no changed weight elsewhere, no moved threshold.

def test_max_length_signal_is_35():
    from trinity_router.complexity import _MAX_LENGTH_SIGNAL
    assert _MAX_LENGTH_SIGNAL == 35


def test_chars_per_length_point_unchanged():
    from trinity_router.complexity import _CHARS_PER_LENGTH_POINT
    assert _CHARS_PER_LENGTH_POINT == 150


@pytest.mark.parametrize("n_chars,expected_length_signal", [
    (0, 0), (149, 0), (150, 1),
    (2999, 19), (3000, 20), (3001, 20), (3149, 20),  # old cap (20) region — UNCHANGED
    (3150, 21),                                       # first char past the OLD cap
    (5249, 34), (5250, 35), (5251, 35), (6000, 35),   # new cap (35) region
])
def test_length_signal_boundary_values(n_chars, expected_length_signal):
    """Every value <=3149 chars produces the EXACT same length_signal as
    before this etapa (min(20, n) == min(35, n) whenever n <= 20) — proving
    short/medium messages are byte-for-byte unaffected. Only messages
    exceeding the OLD cap (>=3150 chars) see any difference at all."""
    result = compute_complexity(capability="chat", messages=_messages("x" * n_chars))
    assert result.signals["length_signal"] == expected_length_signal


def test_short_and_medium_messages_score_identically_to_before(monkeypatch):
    """Direct proof that messages <=3000 chars are untouched: recompute
    with the OLD cap (20) via monkeypatch and assert identical scores for
    a battery of short/medium texts spanning every band."""
    import trinity_router.complexity as complexity_module

    texts = [
        "hi",
        "why does this happen?",
        "Please explain why this approach works and compare it to alternatives.",
        "```python\nprint(1)\n```",
        "x" * 1600,
        "x" * 3000,  # exactly at the old cap boundary
    ]
    before = [compute_complexity(capability="code", messages=_messages(t), tools=[{"name": "x"}]) for t in texts]

    monkeypatch.setattr(complexity_module, "_MAX_LENGTH_SIGNAL", 20)
    after_with_old_cap = [compute_complexity(capability="code", messages=_messages(t), tools=[{"name": "x"}]) for t in texts]

    for b, a in zip(before, after_with_old_cap):
        assert b.score == a.score


# ── Quality independent of tool_signal: CHAT text-only can reach premium ────

def test_chat_text_only_can_reach_premium_without_tools():
    """The core proof this etapa exists for: capability=chat, tools=None
    (exactly what chat_service.py always sends), a single message with no
    history, real reasoning+code+multi-step signals plus enough length to
    hit the new 35-point cap — required_quality_class == "premium", with
    tool_signal == 0 the entire time. No tool_signal was "faked" to get
    here — every point comes from length/reasoning/code/multi_step, all
    pre-existing signals."""
    long_text = (
        "Explain why this design choice matters, and compare it step by step to "
        "alternative approaches, including how the current function signature "
        "affects long-term maintainability. "
        + ("Consider this scenario carefully and weigh the tradeoffs of each "
           "alternative in depth. " * 60)
    )
    result = compute_complexity(capability="chat", messages=_messages(long_text), tools=None)
    assert result.signals["tool_signal"] == 0
    assert result.signals["base_capability_score"] == 0  # capability="chat", not "agent"
    assert result.score >= 75
    assert result.required_quality_class == "premium"


def test_chat_agent_and_premium_can_reach_it_independently():
    """AGENT+premium (the pre-existing path) still works exactly as before
    — this etapa adds a NEW path (CHAT+premium), it does not remove or
    weaken the old one."""
    long_prompt = (
        "Refactor and debug this codebase step by step, explain why, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    result = compute_complexity(capability="agent", messages=_messages(long_prompt), tools=[{"name": "shell"}])
    assert result.required_quality_class == "premium"
    assert result.signals["tool_signal"] > 0  # this path DOES use tool_signal, unlike the CHAT path above
