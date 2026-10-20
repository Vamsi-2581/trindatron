"""The behavioural layer: one policy, four layers, one assembly point.

    SystemPolicy   constant, versioned, provider-independent  (policy.py)
    TaskContext    what this request is                        (task_context.py)
    ToolPolicy     what may be reached for                     (tool_policy.py)
    MemoryContext  the few prior facts that bear on it         (memory_context.py)
                                  |
                              assemble()                       (assembly.py)
                                  |
                            system message

Where this sits relative to the router
--------------------------------------
After it, always. Capability, task type, complexity, quality floor, candidate
models, cost, plan and provider health are all decided first; only then is a
prompt built for the model that was chosen. Nothing in this package selects a
model or re-derives a routing decision, and the policy is identical across
models by design — that is what makes a router able to swap one for another
without also swapping behaviour.

Security posture
----------------
This layer is not a security boundary and must never be treated as one. What
actually stops an action is the permission layer, the entitlement check, the
tool allowlist passed to the provider, the approval gate, and the storage and
authentication code behind them — all enforced in code, none of it reading
this text. A model is the one participant in the system that can be argued
with; it is therefore the wrong place to put a control that must hold.

What this layer does is make correct behaviour likely and consistent across
models. It makes nothing impossible.
"""
from __future__ import annotations

from src.prompting.assembly import AssembledPrompt, assemble
from src.prompting.budget import ContextBudget, DEFAULT_BUDGET
from src.prompting.memory_context import MemoryContext, select_relevant
from src.prompting.policy import (
    CURRENT_POLICY_VERSION,
    POLICY_V1,
    POLICY_V2,
    Policy,
    available_versions,
    get_policy,
)
from src.prompting.task_context import TaskContext
from src.prompting.tool_policy import ToolDescriptor, ToolPolicy, from_tool_schemas

__all__ = [
    "AssembledPrompt",
    "assemble",
    "ContextBudget",
    "DEFAULT_BUDGET",
    "MemoryContext",
    "select_relevant",
    "Policy",
    "get_policy",
    "available_versions",
    "CURRENT_POLICY_VERSION",
    "POLICY_V1",
    "POLICY_V2",
    "TaskContext",
    "ToolPolicy",
    "ToolDescriptor",
    "from_tool_schemas",
]
