"""Prompt assembly — the one place a system prompt is built.

Before this module there were five system prompts, in five services, that had
drifted apart: the identity rule existed in four different wordings, the
prompt-injection defence in two of the five, and the language rule in one. Each
was individually reasonable and collectively they meant TRINDATRON behaved
differently depending on which code path a request happened to take.

Order, and why it is this one
-----------------------------
    policy          constant, identical for every model and every request
    task context    what this request is, in which language, under what
                    instructions
    tool policy     what may be reached for, and what waits for approval
    memory          the few prior facts that bear on this request

Stable before variable, and general before specific. The policy leads because
it is the part that must not be displaced; memory trails because it is
background rather than instruction, and because anything that arrived from a
user is safest furthest from the governing text.

What assembly does NOT do
-------------------------
It does not choose a model, read a plan, or consult cost. Those decisions are
finished before a prompt exists (see trinity_router) and re-deriving any of
them here would create a second opinion that can disagree with the first.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.observability.metrics import core_metrics
from src.prompting.budget import ContextBudget, DEFAULT_BUDGET
from src.prompting.memory_context import MemoryContext
from src.prompting.policy import Policy, get_policy
from src.prompting.task_context import TaskContext
from src.prompting.tool_policy import ToolPolicy


_assembled = core_metrics.counter(
    "prompt_assembled_total",
    "System prompts assembled, labeled by policy version — the signal an A/B "
    "arm or a rollback is actually measured on.",
)
_prompt_tokens = core_metrics.histogram(
    "prompt_system_tokens",
    "Approximate system-prompt size, labeled by policy version.",
)
_sections_dropped = core_metrics.counter(
    "prompt_section_dropped_total",
    "Context sections dropped by the budget, labeled by section.",
)


@dataclass(frozen=True)
class AssembledPrompt:
    """The system message, plus what went into it.

    The breakdown is carried rather than discarded because every question worth
    asking later — did the policy actually ship, what did memory cost on this
    call, which version produced this behaviour — is unanswerable from the
    finished string alone.
    """

    system: str
    policy_version: str
    approx_tokens: int
    section_tokens: dict[str, int]
    dropped_sections: tuple[str, ...] = ()

    def as_message(self) -> dict:
        return {"role": "system", "content": self.system}


def assemble(
    *,
    task_context: TaskContext | None = None,
    tool_policy: ToolPolicy | None = None,
    memory: MemoryContext | None = None,
    policy_version: str | None = None,
    budget: ContextBudget | None = None,
) -> AssembledPrompt:
    """Build the system message for one request.

    Every layer is optional except the policy. A caller that passes nothing
    else still gets correct, consistent behaviour — which is what makes this
    safe to adopt one service at a time.
    """
    policy: Policy = get_policy(policy_version)
    budget = budget or DEFAULT_BUDGET

    sections: list[tuple[str, str]] = [("policy", policy.text)]
    if task_context is not None:
        rendered = task_context.render()
        if rendered:
            sections.append(("task_context", rendered))
    if tool_policy is not None:
        sections.append(("tool_policy", tool_policy.render()))
    if memory is not None:
        rendered = memory.render()
        if rendered:
            sections.append(("memory", rendered))

    kept, dropped = budget.fit(sections)

    system = "\n\n".join(text for _, text in kept)
    # Recorded so the version in use is observable in production rather than
    # inferable only from source. Without this, an A/B arm or a rollback could
    # not be attributed to anything after the fact.
    _assembled.inc(policy_version=policy.version)
    _prompt_tokens.observe(len(system) // 4, policy_version=policy.version)
    for name in dropped:
        _sections_dropped.inc(section=name)
    section_tokens = {name: len(text) // 4 for name, text in kept}
    return AssembledPrompt(
        system=system,
        policy_version=policy.version,
        approx_tokens=len(system) // 4,
        section_tokens=section_tokens,
        dropped_sections=dropped,
    )
