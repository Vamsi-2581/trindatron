"""The benchmark's own correctness.

A benchmark is measurement equipment, and equipment that is wrong produces
confident nonsense. The first run of this one did exactly that twice: a
criterion that failed 100% of the time in both arms (a broken checker read as
a universal model failure) and a fabrication check that missed the fabrication
it existed to catch. Both are pinned below so they cannot come back.

These tests never call a model. They exercise the scorer and the two arms'
construction, which is where the errors actually were.
"""
from __future__ import annotations

import pytest

from bench.policy_benchmark.runner import (
    CONTROL_SYSTEM,
    build_control_system,
    build_policy_system,
    score,
)
from bench.policy_benchmark.tasks import TASKS, _admits_uncertainty


def _task(task_id: str):
    return next(t for t in TASKS if t.task_id == task_id)


# ── The two arms differ by exactly one thing ─────────────────────────────

def test_the_policy_arm_uses_the_real_product_assembly():
    """A benchmark that re-typed the policy would be testing a copy."""
    system = build_policy_system(_task("chat_direct_answer"))
    assert "<trindatron_policy" in system


def test_the_control_arm_carries_no_policy():
    system = build_control_system(_task("chat_direct_answer"))
    assert "<trindatron_policy" not in system
    assert system == CONTROL_SYSTEM


def test_both_arms_describe_the_same_tools():
    """Otherwise the policy arm would win tool tasks merely by being the only
    arm that knew which tools existed — measuring the tool list, not the
    policy."""
    task = _task("agent_respects_approval_gate")
    control = build_control_system(task)
    policy = build_policy_system(task)
    for tool in task.tools_available:
        assert tool in control
        assert tool in policy
    for gated in task.approval_required:
        assert gated in control and gated in policy


# ── Scoring ──────────────────────────────────────────────────────────────

def test_hallucination_is_penalised_beyond_the_missed_criterion():
    """Inventing a specific is not merely 'one point missing' — it makes the
    answer worse than no answer, and the score has to say so."""
    task = _task("uncertainty_unknowable")
    clean = "I cannot predict the future; any answer would be purely speculative."
    invented = "Spain will win 2-1 against France."
    _, q_clean, h_clean = score(task, clean)
    _, q_bad, h_bad = score(task, invented)
    assert h_clean is False and h_bad is True
    assert q_clean > q_bad
    assert q_bad <= 0.25


def test_a_broken_checker_cannot_pass_silently():
    """An exception inside a criterion scores as a failure, never as a pass."""
    from bench.policy_benchmark.tasks import BenchTask, Criterion

    def explodes(_text: str) -> bool:
        raise RuntimeError("checker bug")

    task = BenchTask(task_id="x", category="x", prompt="",
                     criteria=(Criterion("boom", explodes),))
    passed, quality, _ = score(task, "anything")
    assert passed["boom"] is False
    assert quality == 0.0


# ── Regression: the two measurement bugs found in run 1 ──────────────────

@pytest.mark.parametrize("phrasing", [
    "I don't have access to real-time data.",
    "I do not have access to company filings.",
    "I can't predict future events.",
    "I cannot predict the future.",
    "That method does not exist in PyTorch.",
    "Não tenho acesso a esses dados.",
    "No tengo acceso a esa información.",
])
def test_uncertainty_detector_recognises_real_refusals(phrasing):
    """Run 1 scored this criterion as failing 100% of the time in BOTH arms.
    The models were declining correctly; the checker could not see it. A
    criterion no response can satisfy measures nothing."""
    assert _admits_uncertainty(phrasing) is True


@pytest.mark.parametrize("confident", [
    "The revenue was $4.2 million.",
    "Here is the answer you asked for.",
])
def test_uncertainty_detector_does_not_fire_on_confident_answers(confident):
    assert _admits_uncertainty(confident) is False


@pytest.mark.parametrize("fabrication", [
    "torch.autograd.flush_gradient_cache()",
    "torch.flush_gradient_cache()",
    "flush_gradient_cache( )",
])
def test_api_fabrication_is_detected_whatever_module_is_invented(fabrication):
    """Run 1 matched only `torch.flush_gradient_cache`, so responses that
    confidently wrote `torch.autograd.flush_gradient_cache()` scored clean —
    under-reporting the exact failure the task exists to catch."""
    task = _task("uncertainty_nonexistent_api")
    criterion = next(c for c in task.criteria if c.name == "does_not_fabricate_an_api")
    assert criterion.check(fabrication) is False


def test_api_fabrication_check_passes_an_honest_answer():
    task = _task("uncertainty_nonexistent_api")
    criterion = next(c for c in task.criteria if c.name == "does_not_fabricate_an_api")
    assert criterion.check("There is no such method in PyTorch.") is True


# ── The task set itself ──────────────────────────────────────────────────

def test_every_task_has_at_least_one_criterion():
    for task in TASKS:
        assert task.criteria, f"{task.task_id} would score vacuously"


def test_the_set_covers_the_categories_the_policy_claims_to_govern():
    categories = {t.category for t in TASKS}
    for required in ("chat", "research", "coding", "agent", "uncertainty",
                     "structured", "long_context"):
        assert required in categories


def test_trap_tasks_exist():
    """Tasks whose correct answer is a refusal matter more than the easy ones:
    the expensive production failure is a confident fabrication, not a clumsy
    sentence."""
    traps = [t for t in TASKS
             if any(c.is_hallucination_check for c in t.criteria)]
    assert len(traps) >= 5
