"""
Complexity Engine — Adaptive Intelligence Router.

Estimates how demanding a request is, deterministically and cheaply, so
the router can pick the CHEAPEST model that is still CAPABLE of handling
it — never the reverse. No LLM call is made here (that would defeat the
whole point: routing must be near-free compared to the model call it's
choosing between). Every signal below is computed from data the router
ALREADY has on hand (capability, messages, tools) — no new I/O, no new
external dependency.

complexity_score = base_capability_score
                  + length_signal
                  + reasoning_signal
                  + code_signal
                  + tool_signal
                  + multi_step_signal
                  + context_signal

...then clamped to [0, 100]. Each term is an explicit, named, independently
testable constant — not a scatter of inline magic numbers — so the whole
formula can be recalibrated later (mission requirement: "o algoritmo
precisa ser calibrável no futuro") by editing the weights in ONE place.

Deliberately NOT exhaustive: the mission explicitly warns against "não
exagerar no número de heurísticas" — this uses seven signals, not twenty.
Anything not covered here (e.g. true semantic ambiguity, multi-artifact
analysis) either shows up indirectly through length/multi-step signals or
is left for a future, explicitly-calibrated addition — not invented now.

The resulting score maps to two things:
  - `band`: a human-readable label (trivial/simple/moderate/complex/expert)
    for observability/logs — this is the mission's own conceptual scale
    (0-20/21-40/41-60/61-80/81-100), purely descriptive.
  - `required_quality_class`: the ACTUAL minimum bar fed into
    providers.get_eligible_models()/select_cheapest_capable() — mapped
    onto the EXISTING four-tier providers.QUALITY_CLASS_ORDER
    (ultra_low/low/standard/premium), not a new, fifth tier invented just
    to mirror the five score bands 1:1. The current catalog has no model
    between "standard" and "premium" to justify a fifth rung, and adding
    one with no real model to back it would be exactly the kind of
    invented capability the mission says not to do.

V1 scope note: this bar is enforced against ONE curated candidate pool
per (task_type, plan, capability) — the same pool
selector.select_provider_chain() has always produced — not against every
model in PROVIDER_CATALOG or every provider that could theoretically
exist. See router.py's own V1 scope note for how this bar is used to
filter (not just reorder) that pool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Weights — the ONLY place these numbers live. Calibrate here, not inline.
# ---------------------------------------------------------------------------

# Base floor per capability (Capability.value strings — this module has no
# dependency on Core's capability.classifier, so it takes the raw string
# already forwarded on RoutingRequest.capability rather than importing an
# enum from a different package).
_BASE_CAPABILITY_SCORE: dict[str, int] = {
    "chat": 0,
    "files": 10,
    "research": 10,
    "work": 15,
    "automation": 20,
    "code": 20,
    "agent": 30,
    "image": 10,
    "video": 10,
    "audio": 10,
}
_DEFAULT_BASE_CAPABILITY_SCORE = 0  # unrecognized/None capability — treat as CHAT-like

_MAX_LENGTH_SIGNAL = 35  # raised from 20 (2026-09-07) — see below
_CHARS_PER_LENGTH_POINT = 150  # every 150 chars of the user's latest message = +1, capped

# Calibration note (2026-09-07, Execution Mode / Quality Independence etapa):
# tool_signal (below) is only ever nonzero when task_type=AGENT (the caller
# attached real tools) — by construction, a CHAT-dispatched request (no
# tools, ever, see chat_service.py) can never reach it. Before this change,
# capability="chat" (base=0) topped out at 20(length)+15(reasoning)+
# 15(code)+10(multi_step)=60 for a single message with no history — 15
# short of the "premium" threshold (75), making CHAT+premium structurally
# unreachable for a fresh request regardless of how demanding the text
# actually was. Raising ONLY this one cap to 35 closes exactly that gap
# (0+35+15+15+10=75) using signals that already exist — no new heuristic,
# no changed weight elsewhere, no moved threshold. It only affects
# messages long enough to have exceeded the OLD cap already (roughly
# >=3150 chars, i.e. len // 150 > 20) — anything shorter computes an
# identical length_signal to before, so short/medium messages (the
# overwhelming majority of real chat traffic) are completely unaffected.
# See test_complexity.py's regression suite for the exact boundary proof.

_REASONING_SIGNAL_WEIGHT = 15
_CODE_SIGNAL_WEIGHT = 15
_TOOL_SIGNAL_WEIGHT = 15
_MULTI_STEP_SIGNAL_WEIGHT = 10

_MAX_CONTEXT_SIGNAL = 15
_MESSAGES_PER_CONTEXT_POINT = 4  # every 4 prior messages in history = +1, capped

_MAX_SCORE = 100.0
_MIN_SCORE = 0.0

# Reasoning-heavy language (EN/PT-BR/ES) — deliberately coarse, mirrors
# capability/classifier.py's own regex-rule style (deterministic, no LLM).
_REASONING_PATTERN = re.compile(
    r"\b("
    r"why|explain|compare|analyz[e|ing]|prove|reason|step.by.step|"
    r"trade.?off|pros?\s+and\s+cons?|"
    r"por\s*qu[eê]|explique?|compar(?:e|ar)|analis(?:e|ar)|prov(?:e|ar)|raci[oó]cinio|"
    r"por\s*qu[eé]|explica(?:r)?|compar(?:a|ar)|analiz(?:a|ar)"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

_MULTI_STEP_PATTERN = re.compile(
    r"\b("
    r"step.by.step|multi.?step|first.*then|plan\s+and\s+execute|"
    r"passo\s+a\s+passo|primeiro.*depois|planeje?\s+e\s+execut(?:e|ar)|"
    r"paso\s+a\s+paso|primero.*despu[eé]s|planific(?:a|ar)\s+y\s+ejecut(?:a|ar)"
    r")\b",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)

_CODE_FENCE_PATTERN = re.compile(r"```")
_CODE_KEYWORD_PATTERN = re.compile(
    r"\b(function|class|def |import |traceback|stack\s*trace|exception|null\s*pointer|segfault)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Score -> band (observability label only, mission's own quintile scale)
# ---------------------------------------------------------------------------

_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 20, "trivial"),
    (21, 40, "simple"),
    (41, 60, "moderate"),
    (61, 80, "complex"),
    (81, 100, "expert"),
)

# ---------------------------------------------------------------------------
# Score -> required_quality_class (load-bearing, existing 4-tier vocabulary)
# Quartile mapping onto providers.QUALITY_CLASS_ORDER — see module docstring
# for why this is 4 rungs, not 5.
# ---------------------------------------------------------------------------

_QUALITY_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (25, "ultra_low"),
    (50, "low"),
    (75, "standard"),
    (101, "premium"),  # 101 so a clamped score of exactly 100 still matches (<=100 < 101)
)


@dataclass(frozen=True)
class ComplexityResult:
    """Deterministic complexity estimate for one routing decision.

    `signals` breaks the score down by contributing term — purely for
    observability (mission requirement 14: "por que o TRINDATRON escolheu
    esse modelo?"), never used for routing logic itself beyond the total.
    """
    score: float
    band: str
    required_quality_class: str
    signals: dict[str, float] = field(default_factory=dict)


def _band_for_score(score: float) -> str:
    for low, high, label in _BANDS:
        if low <= score <= high:
            return label
    return "expert"  # unreachable given clamping, but never silently None


def _required_quality_class_for_score(score: float) -> str:
    for threshold, quality_class in _QUALITY_THRESHOLDS:
        if score < threshold:
            return quality_class
    return "premium"  # unreachable given clamping to 100, but never silently None


def _last_user_message_content(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def compute_complexity(
    *,
    capability: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> ComplexityResult:
    """Compute a deterministic 0-100 complexity score for one request.

    No LLM call, no I/O — pure function of already-available data. Safe
    to call on every single routing decision (mission requirement 19:
    routing overhead must be negligible next to the model call itself).
    """
    text = _last_user_message_content(messages)

    base = _BASE_CAPABILITY_SCORE.get((capability or "").lower(), _DEFAULT_BASE_CAPABILITY_SCORE)

    length_signal = min(_MAX_LENGTH_SIGNAL, len(text) // _CHARS_PER_LENGTH_POINT)

    reasoning_signal = _REASONING_SIGNAL_WEIGHT if _REASONING_PATTERN.search(text) else 0

    has_code = bool(_CODE_FENCE_PATTERN.search(text) or _CODE_KEYWORD_PATTERN.search(text))
    code_signal = _CODE_SIGNAL_WEIGHT if has_code else 0

    tool_signal = _TOOL_SIGNAL_WEIGHT if tools else 0

    multi_step_signal = _MULTI_STEP_SIGNAL_WEIGHT if _MULTI_STEP_PATTERN.search(text) else 0

    # len(messages) includes the system prompt + current turn; only PRIOR
    # conversational turns count as "context to track" (messages - 1 for
    # the current user turn is a floor, never negative).
    prior_turns = max(0, len(messages) - 1)
    context_signal = min(_MAX_CONTEXT_SIGNAL, prior_turns // _MESSAGES_PER_CONTEXT_POINT)

    signals = {
        "base_capability_score": float(base),
        "length_signal": float(length_signal),
        "reasoning_signal": float(reasoning_signal),
        "code_signal": float(code_signal),
        "tool_signal": float(tool_signal),
        "multi_step_signal": float(multi_step_signal),
        "context_signal": float(context_signal),
    }
    raw_score = sum(signals.values())
    score = max(_MIN_SCORE, min(_MAX_SCORE, raw_score))

    return ComplexityResult(
        score=score,
        band=_band_for_score(score),
        required_quality_class=_required_quality_class_for_score(score),
        signals=signals,
    )
