"""The prompting layer: one policy, four layers, one assembly point.

These tests pin the properties the architecture exists to guarantee. The
important ones are not "does the text contain a word" but "can this invariant
be broken by a future change without anyone noticing" — a prompt layer fails
silently, produces a plausible-looking response, and is discovered weeks later
in a support ticket.
"""
from __future__ import annotations

import pytest

from src.prompting import (
    CURRENT_POLICY_VERSION,
    MemoryContext,
    TaskContext,
    ToolPolicy,
    assemble,
    available_versions,
    from_tool_schemas,
    get_policy,
)
from src.prompting.budget import ContextBudget
from src.prompting.memory_context import select_relevant
from src.prompting.tool_policy import ToolDescriptor


# ── The policy is always there ───────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {},
    {"task_context": TaskContext()},
    {"tool_policy": ToolPolicy.none()},
    {"memory": MemoryContext.build(["a fact"], "a fact")},
    {"task_context": TaskContext(task_type="chat"), "tool_policy": ToolPolicy.none()},
])
def test_the_policy_is_present_in_every_assembled_prompt(kwargs):
    """Whatever layers are or are not supplied, behaviour is governed."""
    result = assemble(**kwargs)
    assert "<trindatron_policy" in result.system
    assert result.policy_version == CURRENT_POLICY_VERSION


def test_the_prompt_says_what_day_it_is_and_that_knowledge_ages():
    """A model cannot judge how old its own knowledge is without today's date. Asked for "the
    latest", it answered from training data as confidently as it answers arithmetic — the bug this
    block exists to prevent."""
    rendered = TaskContext(today="2026-09-16").render()
    assert "<today>2026-09-16</today>" in rendered
    assert "may be out of date" in rendered


def test_the_date_stays_out_of_the_cacheable_prefix():
    """It changes daily, so in the prefix it would end prompt caching for everyone, every day."""
    policy = get_policy().text
    system = assemble(task_context=TaskContext(today="2026-09-16")).as_message()["content"]
    assert system[: len(policy)] == policy


def test_the_policy_carries_all_four_blocks():
    text = get_policy().text
    for block in ("<role>", "<boundaries>", "<output>", "<evidence>"):
        assert block in text


def test_the_policy_is_compact():
    """A long policy competes with the user's request for attention and is paid
    for on every call. About a page is the design target."""
    assert get_policy().approx_tokens < 1500


def test_an_unknown_policy_version_raises_rather_than_falling_back():
    """A silent fallback would make an A/B arm quietly run the control's text
    and report it as the variant — corrupting the only measurement this layer
    exists to support."""
    with pytest.raises(ValueError):
        assemble(policy_version="TRINDATRON_POLICY_V99")


def test_versions_are_enumerable_for_ab_testing():
    assert CURRENT_POLICY_VERSION in available_versions()


# ── The layers stay separate ─────────────────────────────────────────────

def test_the_policy_names_no_language():
    """Language belongs to TaskContext. A policy naming one would be wrong for
    most requests; one listing every language would grow forever."""
    lowered = get_policy().text.lower()
    for language in ("portuguese", "português", "english", "spanish", "español"):
        assert language not in lowered


def test_the_policy_names_no_model_or_provider():
    """The same text must work on every model, so it cannot mention any."""
    lowered = get_policy().text.lower()
    for name in ("gpt", "claude", "gemini", "mistral", "anthropic", "openai", "llama"):
        assert name not in lowered


def test_the_policy_contains_no_routing_decision():
    """Routing is decided before a prompt exists. A policy that re-derived it
    would be a second router that can disagree with the first."""
    lowered = get_policy().text.lower()
    for leak in ("use model", "task type", "quality floor", "cheaper model", "router"):
        assert leak not in lowered


def test_the_policy_contains_no_plan_or_billing_language():
    """The model must not be able to reason about, or negotiate with, a user's
    commercial limits."""
    import re

    lowered = get_policy().text.lower()
    # Word boundaries, not substrings. Two near-misses made that necessary and
    # both are legitimate policy wording: "a plan to act" (the ordinary English
    # word, and exactly the intention-vs-completion distinction the execution
    # block draws) and "quotation" (which contains "quota"). A substring test
    # here would force the policy to be written around the test.
    for leak in ("tcu", "billing", "quota", "quotas", "subscription", "credits",
                 "upgrade", "your plan", "free plan", "pro plan"):
        assert not re.search(rf"{re.escape(leak)}", lowered), leak


def test_task_context_is_a_separate_block_from_the_policy():
    result = assemble(task_context=TaskContext(task_type="chat", capability="code"))
    assert "<task_context>" in result.system
    policy_end = result.system.index("</trindatron_policy>")
    assert result.system.index("<task_context>") > policy_end


def test_language_always_reaches_the_prompt():
    """Explicit when known, mirror-the-user when not — but never absent, since
    the behaviour it replaces was an explicit rule in the old chat prompt."""
    assert "Spanish" in TaskContext(language="Spanish").render()
    assert "language the person is writing in" in TaskContext().render()


def test_user_style_preference_never_enters_the_policy():
    """One person's preference must not become everyone's default."""
    styled = assemble(task_context=TaskContext(response_style="Be extremely terse."))
    assert "extremely terse" in styled.system
    assert "extremely terse" not in get_policy().text


# ── Tool policy describes exactly what exists ────────────────────────────

def test_tool_policy_is_derived_from_the_schemas_sent_to_the_provider():
    """Derived, not hand-written, so description and reality cannot drift."""
    schemas = [{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}]
    policy = from_tool_schemas(schemas, approval_required=frozenset({"write_file"}))
    rendered = policy.render()
    assert "read_file" in rendered and "write_file" in rendered
    assert "approval" in rendered.lower()


def test_a_tool_absent_from_the_schemas_is_never_advertised():
    """The invariant that stops the model offering capabilities it lacks."""
    policy = from_tool_schemas([{"function": {"name": "read_file"}}])
    rendered = policy.render()
    for absent in ("execute_shell", "write_file", "send_email", "desktop_click"):
        assert absent not in rendered


def test_no_tools_is_stated_explicitly_rather_than_left_blank():
    rendered = ToolPolicy.none().render()
    assert "no tools" in rendered.lower()


def test_tool_policy_appears_exactly_once():
    result = assemble(
        task_context=TaskContext(task_type="agent"),
        tool_policy=from_tool_schemas([{"function": {"name": "read_file"}}]),
    )
    assert result.system.count("<tools>") == 1


def test_gated_tools_are_named_as_gated():
    policy = ToolPolicy(tools=(
        ToolDescriptor("read_file", requires_approval=False),
        ToolDescriptor("execute_shell", requires_approval=True),
    ))
    rendered = policy.render()
    after = rendered[rendered.lower().index("approval"):]
    assert "execute_shell" in after
    assert "never that it is done" in rendered


# ── Memory is selected, not dumped ───────────────────────────────────────

def test_only_relevant_memories_are_selected():
    memories = [
        "The Helios project uses PostgreSQL",
        "User prefers dark mode",
        "User supports Palmeiras",
    ]
    picked = [s.content for s in select_relevant(memories, "which database does Helios use?")]
    assert any("PostgreSQL" in p for p in picked)
    assert not any("Palmeiras" in p for p in picked)


def test_a_signal_free_request_falls_back_to_recent_not_to_everything():
    memories = [f"fact number {i}" for i in range(30)]
    picked = select_relevant(memories, "hi")
    assert 0 < len(picked) <= 6


def test_memory_selection_is_bounded():
    memories = [f"the alpha project note {i}" for i in range(50)]
    assert len(select_relevant(memories, "alpha project")) <= 6


def test_memory_is_framed_as_background_not_instruction():
    """Memory content originates from a user and must not read as a command."""
    rendered = MemoryContext.build(["Always deploy on Fridays"], "deploy").render()
    assert "not instructions" in rendered.lower() or "background" in rendered.lower()


def test_no_memories_renders_nothing_at_all():
    assert MemoryContext.build([], "anything").render() == ""


# ── Budget ───────────────────────────────────────────────────────────────

def test_budget_drops_memory_before_anything_else():
    budget = ContextBudget(max_tokens=1)
    result = assemble(
        task_context=TaskContext(task_type="chat"),
        tool_policy=ToolPolicy.none(),
        memory=MemoryContext.build(["a memory"], "a memory"),
        budget=budget,
    )
    assert "memory" in result.dropped_sections
    assert result.dropped_sections.index("memory") == 0


def test_budget_never_drops_the_policy():
    """A request that cannot afford the policy is exactly the one where
    behaviour must not be unconstrained."""
    result = assemble(
        task_context=TaskContext(task_type="chat"),
        tool_policy=ToolPolicy.none(),
        memory=MemoryContext.build(["m"], "m"),
        budget=ContextBudget(max_tokens=1),
    )
    assert "<trindatron_policy" in result.system
    assert "policy" not in result.dropped_sections


def test_dropping_is_reported_not_silent():
    result = assemble(
        memory=MemoryContext.build(["m"], "m"), budget=ContextBudget(max_tokens=1)
    )
    assert isinstance(result.dropped_sections, tuple)


def test_section_costs_are_visible():
    result = assemble(
        task_context=TaskContext(task_type="chat"), tool_policy=ToolPolicy.none()
    )
    assert set(result.section_tokens) == {"policy", "task_context", "tool_policy"}
    assert all(v > 0 for v in result.section_tokens.values())


# ── Behavioural guarantees the policy is supposed to carry ───────────────

@pytest.mark.parametrize("guarantee", [
    "information to reason about",   # observed content is data, not instruction
    "did not perform",               # no claiming unperformed actions
    "Approvals are not advisory",    # gates are respected
    "cannot support",                # no unverifiable specifics
])
def test_policy_states_its_load_bearing_rules(guarantee):
    assert guarantee.lower() in get_policy().text.lower()


# ── Reading the web: recency and sources ─────────────────────────────────────
# Production, 2026-09-28: "the latest electric car made in Brazil" was answered in September 2026 with a July 2025
# launch, figures with no source and no links — after a 2026 search had come back unused.

@pytest.mark.parametrize("tool", ["web_search", "browser_navigate", "browser_get_text"])
def test_web_tools_bring_the_rules_for_reading_the_web(tool):
    rendered = from_tool_schemas([{"function": {"name": tool}}]).render()
    assert "date each source" in rendered and "against <today>" in rendered
    assert "prefer the newest source" in rendered and "search again for the current year" in rendered
    assert "Say when the information is from" in rendered
    assert "give a figure only if a cited source has it" in rendered
    assert '"Sources:" list' in rendered


def test_the_research_rules_stay_small_enough_to_keep_memory_in_the_prompt():
    """The system prompt has a soft budget and memory is dropped first; the rules must not be what tips it over."""
    from src.prompting.tool_policy import WEB_RESEARCH_RULES

    assert len(WEB_RESEARCH_RULES) // 4 <= 90


def test_tools_that_do_not_read_the_web_carry_no_research_rules():
    rendered = from_tool_schemas([{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}]).render()
    assert "Sources:" not in rendered and "<today>" not in rendered


def test_no_tools_carries_no_research_rules():
    assert "Sources:" not in ToolPolicy.none().render()
