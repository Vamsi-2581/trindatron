"""The conversation is told what the product can really do — so it neither invents limits nor promises what is missing."""
from __future__ import annotations

import re

from src.capability.matrix import ORDER, CapabilityState, prompt_lines
from src.prompting.task_context import TaskContext

VENDORS = re.compile(r"openai|gemini|google|xai|grok|groq|elevenlabs|anthropic|claude|veo|fal\b|whisper|docker", re.I)


def _matrix(**available: bool) -> dict[str, dict]:
    matrix = {}
    for name in ORDER:
        if name in ("video_editing", "video_to_video", "avatar_generation"):
            matrix[name] = CapabilityState(name, "not_integrated").view()
        else:
            ok = available.get(name, False)
            scope = "sandbox" if name in ("browser_control", "computer_control", "code_execution") else None
            matrix[name] = CapabilityState(name, "available" if ok else "unavailable", "ok" if ok else "down",
                                           scope=scope).view()
    return matrix


def test_every_capability_has_inputs_outputs_and_a_status_and_names_no_engine():
    matrix = _matrix(text=True, image_generation=True, computer_control=True)
    assert list(matrix) == list(ORDER) and len(ORDER) == 20
    for name, state in matrix.items():
        assert set(state) >= {"available", "status", "inputs", "outputs", "health", "fallback"}, name
    assert matrix["computer_control"]["scope"] == "sandbox"
    assert matrix["avatar_generation"]["status"] == "not_integrated"
    assert not VENDORS.search(str(matrix))


def test_available_capabilities_are_stated_with_how_to_ask_and_missing_ones_by_name():
    lines = prompt_lines(_matrix(text=True, vision=True, image_generation=True, video_generation=False))
    rendered = TaskContext(capabilities=lines).render()
    assert "<capabilities>" in rendered
    assert "- image_generation: ask to create an image, in any format or size" in rendered
    assert "- vision: attach an image and ask about it" in rendered
    missing = rendered.split("Not available right now:")[1]
    assert "video_generation (temporarily unavailable)" in missing
    assert "avatar_generation (TRINDATRON does not do this yet)" in missing
    assert "Never say TRINDATRON cannot do something listed as available" in rendered
    assert not VENDORS.search(rendered)


def test_an_outage_is_worded_as_the_products_state_never_as_the_models_own_limit():
    """Measured against a running Core: with image_generation temporarily unavailable, the answer came
    back as "não consigo criar imagens" — a first-person, permanent-sounding refusal. The block listed
    the capability correctly; what it never did was say how the sentence must read."""
    rendered = TaskContext(capabilities=prompt_lines(_matrix(text=True, image_generation=False))).render()
    assert "not available right now" in rendered
    assert 'never "I cannot create images"' in rendered
    assert "permanent limitation" in rendered


def test_every_available_capability_says_how_to_ask_for_it():
    """An available capability with an empty hint rendered as "- audio_input: ", which tells the model
    nothing and reads as a truncated list."""
    lines = prompt_lines(_matrix(**{name: True for name in ORDER}))
    for name, available, hint in lines:
        if available:
            assert hint.strip(), f"{name} is offered with no way to ask for it"


def test_a_capability_outside_the_plan_is_named_as_such_and_the_persons_device_is_never_offered():
    matrix = _matrix(text=True)
    matrix["computer_control"] = CapabilityState("computer_control", "plan", "down", scope="sandbox").view()
    rendered = TaskContext(capabilities=prompt_lines(matrix)).render()
    assert "computer_control (not included in this person's current plan)" in rendered
    assert "TRINDATRON does not connect to their device" in rendered


def test_a_conversation_turn_is_told_it_has_no_tools_and_never_controls_the_persons_device():
    lines = prompt_lines(_matrix(text=True, agent=True, computer_control=True))
    chat = TaskContext(capabilities=lines).render()
    agent = TaskContext(capabilities=lines, has_tools=True).render()
    assert "you have no tools yourself" in chat and "you have no tools yourself" not in agent
    assert "Never claim to control the person's own device" in chat
    assert "computer_control: agent mode operates a separate virtual computer (a separate environment" in chat


def test_without_capabilities_nothing_is_rendered_for_them():
    assert "<capabilities>" not in TaskContext().render()
