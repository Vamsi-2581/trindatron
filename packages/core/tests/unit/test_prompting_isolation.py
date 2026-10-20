"""What the prompting layer must NOT be able to reach.

The layer's value is that it is the same everywhere; its danger is that it sits
on the path of every request. These tests pin the blast radius: assembling a
prompt must not touch money, entitlements, or the router, and the layer must
not become a second place where security is "enforced" — because a model is
the one participant in the system that can be argued with.
"""
from __future__ import annotations

import inspect

import pytest

from src.prompting import TaskContext, ToolPolicy, assemble, get_policy


def test_assembly_imports_nothing_from_billing_or_entitlements():
    """Static guarantee: the module graph itself cannot reach money."""
    import src.prompting.assembly as assembly
    import src.prompting.budget as budget
    import src.prompting.policy as policy
    import src.prompting.task_context as task_context
    import src.prompting.tool_policy as tool_policy

    forbidden = ("billing", "entitlement", "ledger", "credit", "subscription",
                 "payment", "usage")
    for module in (assembly, budget, policy, task_context, tool_policy):
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for name in forbidden:
                    assert name not in stripped.lower(), (
                        f"{module.__name__} imports {name!r}: the prompting layer "
                        "must not be able to read or change money."
                    )


def test_assembling_a_prompt_writes_nothing():
    """assemble() is pure: same inputs, same output, no side effects. A prompt
    builder that mutated state would make an A/B test unrepeatable."""
    kwargs = dict(
        task_context=TaskContext(task_type="chat", language="English"),
        tool_policy=ToolPolicy.none(),
    )
    first = assemble(**kwargs)
    second = assemble(**kwargs)
    assert first.system == second.system
    assert first.policy_version == second.policy_version


def test_assembly_takes_no_database_session():
    """The layer that every service imports must stay testable without a DB,
    and must not be able to query one mid-assembly."""
    params = inspect.signature(assemble).parameters
    for name in params:
        assert "db" not in name.lower()
        assert "session" not in name.lower()


def test_the_policy_does_not_claim_to_be_a_security_control():
    """Enforcement lives in the permission layer, the entitlement check, the
    tool allowlist and the approval gate. The policy makes good behaviour
    likely; it must never be described, here or in the text, as what makes bad
    behaviour impossible."""
    from src import prompting

    doc = (prompting.__doc__ or "").lower()
    assert "not a security boundary" in doc


def test_changing_the_policy_cannot_change_a_tool_list():
    """The model is told what exists; it does not decide what exists."""
    schemas_seen = ToolPolicy.none()
    assert schemas_seen.tools == ()
    # There is no setter, no mutation path, and no way for policy text to add
    # a tool — the dataclass is frozen and built from provider schemas.
    with pytest.raises(Exception):
        schemas_seen.tools = ("execute_shell",)  # type: ignore[misc]


def test_policy_text_is_immutable():
    policy = get_policy()
    with pytest.raises(Exception):
        policy.text = "something else"  # type: ignore[misc]
