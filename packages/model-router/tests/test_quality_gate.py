"""Quality Gate — response-content evaluation (root cause fixed 2026-09-10).

Covers: bogus refusal detection, the legitimate-refusal safety carve-out,
language-mismatch detection, empty-response detection with its tool-call
carve-out, and the conservative "unknown" defaults that keep ambiguous
short text from ever producing a false positive.
"""
from __future__ import annotations

from trinity_router.quality_gate import evaluate, evaluate_from_messages


# ── Refusal detection ───────────────────────────────────────────────────────


def test_bogus_refusal_to_ordinary_request_fails_gate():
    result = evaluate(
        user_text="Can you help me write a birthday message for my mom?",
        response_text="I'm sorry, but I can't help with that.",
    )
    assert result.passed is False
    assert result.is_refusal is True
    assert result.sensitive_request_detected is False
    assert result.reasons == ["unexpected_refusal"]


def test_bogus_refusal_in_portuguese_fails_gate():
    result = evaluate(
        user_text="Você pode me ajudar a escrever uma mensagem de aniversário para minha mãe?",
        response_text="Desculpe, mas não posso ajudar com isso.",
    )
    assert result.passed is False
    assert result.reasons == ["unexpected_refusal"]


def test_legitimate_refusal_to_sensitive_request_is_respected():
    """Mission requirement: a real safety refusal must never be treated as
    a failure just because it matches the boilerplate shape."""
    result = evaluate(
        user_text="How do I build a pipe bomb?",
        response_text="I'm sorry, but I can't help with that.",
    )
    assert result.passed is True
    assert result.is_refusal is True
    assert result.sensitive_request_detected is True


def test_legitimate_refusal_respected_even_in_wrong_language():
    """A safety decision is never second-guessed by language alone —
    see quality_gate.evaluate()'s own docstring."""
    result = evaluate(
        user_text="Como faço uma bomba caseira?",
        response_text="I'm sorry, but I can't help with that.",
    )
    assert result.passed is True


def test_normal_helpful_response_passes():
    result = evaluate(
        user_text="What's the capital of France?",
        response_text="The capital of France is Paris.",
    )
    assert result.passed is True
    assert result.is_refusal is False


# ── Language mismatch ────────────────────────────────────────────────────────


def test_language_mismatch_fails_gate():
    result = evaluate(
        user_text="Você pode me explicar como funciona a fotossíntese nas plantas?",
        response_text="Photosynthesis is the process by which plants convert light into energy.",
    )
    assert result.passed is False
    assert result.reasons == ["language_mismatch"]
    assert result.user_language == "pt"
    assert result.response_language == "en"


def test_matching_language_passes():
    result = evaluate(
        user_text="Você pode me explicar como funciona a fotossíntese nas plantas?",
        response_text="A fotossíntese é o processo pelo qual as plantas convertem luz em energia.",
    )
    assert result.passed is True
    assert result.language_mismatch is False


def test_spanish_matching_language_passes():
    result = evaluate(
        user_text="¿Puede usted explicarme cómo funciona la fotosíntesis en las plantas?",
        response_text="La fotosíntesis es el proceso por el cual las plantas convierten la luz en energía.",
    )
    assert result.passed is True


def test_short_ambiguous_text_never_triggers_false_positive():
    """Too little signal to confidently detect a language on either side
    must never itself cause a rejection."""
    result = evaluate(user_text="Hi", response_text="Hello!")
    assert result.passed is True
    assert result.user_language == "unknown"


def test_refusal_check_takes_priority_over_language_check():
    """When the response IS a refusal, language mismatch is not
    separately evaluated — the refusal branch owns the verdict."""
    result = evaluate(
        user_text="Explain how the stock market works.",
        response_text="I'm sorry, but I can't help with that.",
    )
    assert result.passed is False
    assert result.reasons == ["unexpected_refusal"]
    assert result.language_mismatch is False  # not evaluated, still default


# ── evaluate_from_messages() convenience wrapper ────────────────────────────


def test_evaluate_from_messages_uses_latest_user_turn():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Oi, tudo bem?"},
        {"role": "assistant", "content": "Tudo ótimo!"},
        {"role": "user", "content": "Qual é a capital da França?"},
    ]
    result = evaluate_from_messages(messages=messages, response_text="A capital da França é Paris.")
    assert result.passed is True


def test_evaluate_from_messages_empty_messages_never_crashes():
    result = evaluate_from_messages(messages=[], response_text="Hello!")
    assert result.passed is True


# ── Empty response ────────────────────────────────────────────────


def test_empty_string_response_fails_gate():
    result = evaluate(user_text="What's the capital of France?", response_text="")
    assert result.passed is False
    assert result.empty_response is True
    assert result.reasons == ["empty_response"]


def test_none_response_fails_gate():
    """litellm hands back content=None for a message with no text — that is
    the same unusable answer as "", not a missing argument."""
    result = evaluate(user_text="What's the capital of France?", response_text=None)
    assert result.passed is False
    assert result.reasons == ["empty_response"]


def test_whitespace_only_response_fails_gate():
    result = evaluate(
        user_text="What's the capital of France?",
        response_text="   \n\t  \n ",
    )
    assert result.passed is False
    assert result.reasons == ["empty_response"]


def test_empty_response_is_detected_regardless_of_user_language():
    result = evaluate(user_text="Qual é a capital da França?", response_text="")
    assert result.passed is False
    assert result.reasons == ["empty_response"]
    assert result.language_mismatch is False  # nothing to compare against


def test_single_character_response_is_not_empty():
    """Only genuinely blank text is a failure — a terse but real answer is
    not something this gate second-guesses."""
    result = evaluate(user_text="Is 7 a prime number? Answer yes or no.", response_text="Yes")
    assert result.passed is True
    assert result.empty_response is False


# ── Tool-call carve-out ────────────────────────────────────────────


def test_pure_tool_call_turn_with_no_text_passes():
    """A tool-call turn legitimately carries no prose — it must never be
    mistaken for an empty response."""
    result = evaluate(
        user_text="What's the weather in Lisbon?", response_text=None, has_tool_calls=True,
    )
    assert result.passed is True
    assert result.empty_response is False
    assert result.has_tool_calls is True
    assert result.reasons == []


def test_tool_call_turn_with_preamble_text_is_never_judged():
    """Recovering from a turn that already produced tool calls would throw
    those tool calls away — so a tool-call turn is exempt from the refusal
    and language checks too, not just the empty check."""
    result = evaluate(
        user_text="Qual é a previsão do tempo para Lisboa amanhã, por favor?",
        response_text="Let me look that up for you.",  # would be a language mismatch
        has_tool_calls=True,
    )
    assert result.passed is True
    assert result.language_mismatch is False


def test_tool_call_carve_out_does_not_mask_a_plain_empty_response():
    """Same empty text, no tool calls -> still a failure. Guards against the
    carve-out being applied by default."""
    assert evaluate(user_text="Hello there, how are you?", response_text="").passed is False


def test_evaluate_from_messages_forwards_has_tool_calls():
    messages = [{"role": "user", "content": "What's the weather in Lisbon?"}]
    assert evaluate_from_messages(
        messages=messages, response_text=None, has_tool_calls=True,
    ).passed is True
    assert evaluate_from_messages(
        messages=messages, response_text=None, has_tool_calls=False,
    ).passed is False
