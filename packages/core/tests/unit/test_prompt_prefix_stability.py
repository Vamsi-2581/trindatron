"""The assembled prompt must start with a byte-identical, request-independent
prefix, because that prefix is what the provider caches.

Measured 2026-09-12 against the live APIs: gpt-4o-mini served 1024 of 1082
prompt tokens from cache, mistral-small 1072 of 1105, from the second call
onward. Neither needed a request parameter — both cache automatically for a
stable prefix over their minimum length.

That makes prefix stability a COST property, not just a tidiness one. Anything
request-specific that drifts into the head of the prompt — a name, a locale, a
timestamp, a memory — silently ends caching for every request that follows it,
and nothing fails: the bill simply goes up. These tests are the alarm for that,
since no runtime error would ever be raised.

No model is called here.
"""
from __future__ import annotations

from src.prompting import ToolPolicy, assemble
from src.prompting.memory_context import MemoryContext
from src.prompting.policy import get_policy
from src.prompting.task_context import TaskContext


def _system(**kwargs) -> str:
    return assemble(**kwargs).as_message()["content"]


# -- 1. The stable prefix stays stable ------------------------------------

def test_every_assembled_prompt_starts_with_the_identical_policy_text():
    policy = get_policy().text
    variants = [
        {},
        {"task_context": TaskContext(language="pt-BR")},
        {"task_context": TaskContext(language="en")},
        {"tool_policy": ToolPolicy.none()},
        {"memory": MemoryContext.build([], query="q")},
    ]
    for kwargs in variants:
        assert _system(**kwargs).startswith(policy), (
            f"{kwargs!r} displaced the cacheable prefix"
        )


def test_the_prefix_is_byte_identical_across_repeated_assembly():
    """Same inputs must produce the same bytes. Any nondeterminism here — a
    timestamp, a set iteration, a uuid — would break caching on every call."""
    runs = {_system(task_context=TaskContext(language="pt-BR")) for _ in range(20)}
    assert len(runs) == 1


def test_two_different_requests_share_the_whole_policy_prefix():
    a = _system(task_context=TaskContext(language="pt-BR"))
    b = _system(task_context=TaskContext(language="en"))
    policy = get_policy().text
    assert a != b, "the test would be vacuous if the two prompts were identical"
    common = _common_prefix(a, b)
    assert len(common) >= len(policy), (
        "the shared prefix is shorter than the policy — the cacheable region "
        "shrank, which raises input cost on every request"
    )


def _common_prefix(a: str, b: str) -> str:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return a[:i]


# -- 2. Variable content must stay OUT of the prefix ----------------------

def test_request_specific_content_never_appears_inside_the_policy_region():
    policy = get_policy().text
    marker_language = "pt-BR"
    system = _system(task_context=TaskContext(language=marker_language))
    prefix = system[: len(policy)]
    assert marker_language not in prefix
    assert prefix == policy


def test_memory_content_never_reaches_the_prefix():
    """Memory is per-user. If it ever landed in the prefix it would be both a
    cost regression and a privacy problem — one user's prefix is not another's
    to share."""
    policy = get_policy().text
    secret_ish = "USER-SPECIFIC-MEMORY-MARKER"
    system = _system(
        memory=MemoryContext.build([secret_ish], query=secret_ish)
    )
    assert secret_ish not in system[: len(policy)]


def test_the_policy_is_the_first_section_not_merely_present():
    """`in` is not good enough: a policy that appears after a variable block
    is not cacheable, even though every existing 'policy is present' test
    would still pass."""
    system = _system(
        task_context=TaskContext(language="pt-BR"),
        tool_policy=ToolPolicy.none(),
        memory=MemoryContext.build(["m"], query="m"),
    )
    assert system.index(get_policy().text) == 0


def test_the_policy_clears_the_provider_cache_minimum():
    """Both providers measured only cache prefixes at or above roughly 1024
    tokens. A policy trimmed below that would stop caching entirely — the
    saving from fewer tokens would be swamped by losing the discount on all
    of them."""
    assert get_policy().approx_tokens >= 1024
