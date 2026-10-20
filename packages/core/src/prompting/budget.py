"""ContextBudget — a ceiling on the system prompt, with a stated drop order.

Why this exists at all
----------------------
The system prompt is paid for on every call and competes with the user's actual
request for the model's attention. Before this, it could grow without limit:
project instructions, agent instructions, an unbounded number of skills, and
twenty memories, all concatenated with no ceiling and no idea what it cost.

What it deliberately is not
---------------------------
Not a token-accurate accountant. It estimates at roughly four characters per
token, which is close enough to decide "is this section worth its space" and
avoids tying a provider-independent layer to one vendor's tokenizer.

Not a compressor. Nothing here summarises or rewrites a section — a budget that
silently paraphrased a project's instructions would be changing the request to
make it fit. Sections are kept whole or dropped whole, and a drop is reported.

The drop order is the actual policy
-----------------------------------
When something has to go, it goes from the bottom: memory first (background,
and the request usually stands without it), then tool policy, then task
context. The system policy is never dropped — a request that cannot afford it
is one where behaviour would be unconstrained precisely when context is
already under strain.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Order of sacrifice, least damaging first. Anything not named here is
#: non-droppable, which currently means the policy alone.
_DROP_ORDER: tuple[str, ...] = ("memory", "tool_policy", "task_context")


@dataclass(frozen=True)
class ContextBudget:
    """A soft ceiling on system-prompt size, in approximate tokens."""

    # 3,000 since 2026-09-29 (was 2,500): an agent turn with web tools (their reading rules) in a conversation
    # that already created media (the <artifacts> block) measured 2,526 tokens and lost the person's memory —
    # the first section dropped. test_agent_memory_survives_creations_and_web_research_rules holds this.
    max_tokens: int = 3_000

    def fit(
        self, sections: list[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
        """Return (kept sections in original order, names dropped).

        Drops whole sections, lowest-value first, until the estimate fits or
        nothing droppable remains. Returning the dropped names rather than
        logging them keeps the decision visible to the caller and to tests —
        a budget that silently removed context would be indistinguishable from
        a bug that lost it.
        """
        def total(items: list[tuple[str, str]]) -> int:
            return sum(len(text) for _, text in items) // 4

        kept = list(sections)
        dropped: list[str] = []

        for name in _DROP_ORDER:
            if total(kept) <= self.max_tokens:
                break
            if any(n == name for n, _ in kept):
                kept = [(n, t) for n, t in kept if n != name]
                dropped.append(name)

        return kept, tuple(dropped)


DEFAULT_BUDGET = ContextBudget()
