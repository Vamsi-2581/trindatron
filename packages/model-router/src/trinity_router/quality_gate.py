"""
Quality Gate — Adaptive Intelligence Router response-quality recovery.

Root cause (2026-09-10): the pre-existing failover machinery (retry.py's
classify_failure(), circuit_breaker.py) only ever reacts to EXCEPTIONS
raised by the provider call — a connection error, a 429, an explicit
provider-side content-policy exception. It has no opinion at all about a
response that comes back as an ordinary HTTP 200 success whose CONTENT is
useless: a boilerplate safety-shaped refusal to an entirely ordinary
request ("I'm sorry, but I can't help with that."), or an answer in the
wrong language (the user wrote in Portuguese, the model answered in
English). Both are real, observed failures the router previously accepted
as "success" — no exception was ever raised, so nothing downstream ever
looked twice at the text.

This module is a pure, deterministic, no-LLM-call evaluator (same
philosophy as complexity.py: routing/verification overhead must be
negligible next to the model call itself) that gives router.py a second,
CONTENT-based signal to react to, on top of (never instead of) the
existing exception-based one. router.py decides what to DO with the
result (continue the SAME bounded failover chain, exactly like any other
soft failure) — this module only judges the text.

Deliberately conservative about what counts as a "bad" refusal:
mission requirement — "não burlar políticas de segurança. Uma recusa
legítima deve continuar sendo respeitada." A refusal is treated as
LEGITIMATE (passed=True, no recovery triggered) whenever the user's own
request already matches a coarse, best-effort "this looks like a
sensitive/restricted topic" heuristic — self-harm, weapons/violence,
malware/hacking, illegal drugs, child exploitation, extremist content.
Only a refusal to a request that does NOT match any of those signals is
treated as a bogus/over-cautious refusal worth recovering from. This
cannot be perfect (no regex list is a substitute for real judgment), but
it structurally cannot make an unsafe request MORE likely to succeed: a
request that already looks sensitive is never touched, its refusal is
always respected as-is.

Language-mismatch detection is independent of the refusal check and
carries no such safety trade-off: recovering to a different candidate
because the reply is in the wrong language never changes whether the
underlying request was sensitive — it only asks for the same outcome
(refuse or answer) in the language the user actually used. It is,
however, only ever evaluated on a NON-refusal response (see evaluate()'s
own docstring) since a refusal already answers the "did this pass?"
question on its own terms.

Empty response is the third failure judged here, and the same class of
problem as the other two: an HTTP 200 the caller cannot use, with no
exception raised anywhere. A message whose text is None, "", or nothing
but whitespace carries no answer at all. The one carve-out is a TOOL-CALL
turn: a response that carries tool_calls legitimately has no prose to
show the user, so it passes unconditionally — re-running such a turn on
another candidate would throw away tool calls the caller already holds,
and router.py's existing invariant is that nothing here ever second-
guesses a tool call. This module never inspects a provider object itself;
the caller states whether tool calls are present via `has_tool_calls`.

Scope: English / Portuguese (pt-BR) / Spanish — the same three languages
capability/classifier.py and complexity.py already scope their own regex
rules to elsewhere in this codebase; not a general-purpose language
identifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Refusal boilerplate — deliberately coarse, mirrors the common shape of a
# templated safety refusal across providers. Matching one of these does NOT
# by itself mean the response is "bad" — see evaluate()'s sensitive-request
# carve-out below.
# ---------------------------------------------------------------------------

_REFUSAL_PATTERN = re.compile(
    r"("
    r"i'?m sorry,? but i (?:can'?t|cannot|won'?t) (?:help|assist|comply)|"
    r"i (?:can'?t|cannot) (?:help|assist) with (?:that|this)|"
    r"i'?m not able to (?:help|assist) with (?:that|this)|"
    r"as an ai(?:,| )? i (?:cannot|can'?t|am not able to)|"
    r"i (?:must|have to) decline (?:that|this) request|"
    r"desculpe,? (?:mas )?n[aã]o posso (?:ajudar|auxiliar|atender)|"
    r"n[aã]o posso ajudar (?:com isso|com essa solicita[cç][aã]o)|"
    r"n[aã]o posso atender a esse pedido|"
    r"como (?:uma )?ia,? n[aã]o posso|"
    r"lo siento,? pero no puedo ayudar(?:te)? con eso|"
    r"no puedo ayudar(?:te)? con (?:eso|esta solicitud)|"
    r"como (?:una )?ia,? no puedo"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# ---------------------------------------------------------------------------
# Sensitive-topic heuristic — best-effort, coarse, NOT a moderation system.
# Only used to decide whether an observed refusal should be RESPECTED
# (matches here) or treated as a candidate for recovery (no match). Never
# used to block, filter, or alter a request itself.
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERN = re.compile(
    r"\b("
    r"bomb|explosive|weapon|gun|firearm|kill (?:someone|a person)|murder|"
    r"suicide|self.harm|self.injur|"
    r"malware|ransomware|hack (?:into|a )|exploit (?:a |the )?(?:server|system|network)|ddos|"
    r"synthesiz(?:e|ing) (?:meth|drugs|cocaine|heroin)|"
    r"child (?:porn|exploitation|abuse)|csam|"
    r"bomba|explosivo|arma de fogo|matar (?:algu[eé]m|uma pessoa)|"
    r"suic[ií]dio|automutila[cç][aã]o|"
    r"malware|sequestro de dados|invadir (?:um )?(?:servidor|sistema)|"
    r"pornografia infantil|explora[cç][aã]o infantil|"
    r"bomba|explosivo|arma de fuego|matar a (?:alguien|una persona)|"
    r"suicidio|autolesi[oó]n|"
    r"pornograf[ií]a infantil|explotaci[oó]n infantil"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# ---------------------------------------------------------------------------
# Coarse EN/PT/ES language detection via stopword scoring. Not a general
# language identifier — only needs to be reliable enough to tell whether a
# response is OBVIOUSLY not in the language the user wrote in. Returns
# "unknown" whenever the signal is too weak to be confident (short text,
# no stopword hits, or a tie) — an "unknown" verdict on either side always
# suppresses the language-mismatch check (see evaluate()) rather than risk
# a false positive that would trigger an unnecessary recovery attempt.
# ---------------------------------------------------------------------------

_WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "is", "are", "was", "were", "what", "how", "why", "and", "you",
        "your", "please", "can", "could", "would", "with", "this", "that",
        "have", "has", "does", "do", "not", "help", "explain", "i'm", "im",
        "cannot", "sorry", "about", "for", "from", "will", "should",
    }),
    "pt": frozenset({
        "o", "a", "os", "as", "é", "são", "foi", "eram", "que", "como",
        "por", "favor", "você", "voce", "pode", "poderia", "com", "isso",
        "essa", "esse", "tem", "têm", "não", "para", "uma", "um", "de",
        "da", "do", "ajudar", "explique", "sobre", "desculpe", "vai", "deve",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "es", "son", "fue", "eran", "que", "cómo",
        "como", "por", "favor", "usted", "puede", "podría", "podria", "con",
        "eso", "esa", "ese", "tiene", "tienen", "no", "para", "una", "un",
        "de", "ayudar", "explica", "sobre", "siento", "va", "debe",
    }),
}

_MIN_WORDS_FOR_DETECTION = 3
_MIN_STOPWORD_HITS = 2


def _detect_language(text: str) -> str:
    """Return "en" | "pt" | "es" | "unknown". Deliberately conservative —
    see module docstring for why "unknown" is the safe default."""
    words = [w.lower() for w in _WORD_PATTERN.findall(text or "")]
    if len(words) < _MIN_WORDS_FOR_DETECTION:
        return "unknown"

    scores = {lang: sum(1 for w in words if w in stops) for lang, stops in _STOPWORDS.items()}
    best_lang = max(scores, key=scores.get)
    best_score = scores[best_lang]
    if best_score < _MIN_STOPWORD_HITS:
        return "unknown"
    # A tie between two languages is not a confident detection.
    if sum(1 for s in scores.values() if s == best_score) > 1:
        return "unknown"
    return best_lang


# ---------------------------------------------------------------------------
# Public result + evaluate()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityGateResult:
    """Outcome of evaluating one model response against its own request.

    `reasons` is empty iff `passed` is True. `passed=False` is the signal
    router.py uses to treat this response as a soft (non-exception) failure
    eligible for the SAME bounded failover chain a real provider error
    would use — never a new, separate retry budget."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    is_refusal: bool = False
    empty_response: bool = False
    has_tool_calls: bool = False
    sensitive_request_detected: bool = False
    language_mismatch: bool = False
    user_language: str = "unknown"
    response_language: str = "unknown"

    @property
    def primary_reason(self) -> str:
        return self.reasons[0] if self.reasons else ""


def evaluate(
    *, user_text: str, response_text: str | None, has_tool_calls: bool = False,
) -> QualityGateResult:
    """Judge one (request, response) pair. Pure, synchronous, no I/O.

    Three independent checks, in this order:
      0. Tool-call carve-out + empty-response check — a response carrying
         tool calls always passes untouched (see module docstring); with
         no tool calls, text that is None/""/whitespace-only is a
         recoverable failure, since there is no answer to return and the
         other two checks have nothing to judge.
      1. Refusal check — a boilerplate-shaped refusal is only a FAILURE
         when the user's own request does not match the sensitive-topic
         heuristic (a legitimate refusal is always respected — see module
         docstring). When it IS a refusal, the language-mismatch check
         below is skipped entirely: whether to accept or recover from a
         refusal is a safety decision, not a language one, and must not
         be second-guessed by language alone.
      2. Language-mismatch check — only reached for a non-refusal response.
         A response that answers in a different language than the user
         wrote in is always a recoverable failure (see module docstring
         for why this carries no safety trade-off). Skipped whenever
         either side's language couldn't be confidently detected.
    """
    response_text = response_text if isinstance(response_text, str) else ""
    user_text = user_text if isinstance(user_text, str) else ""

    # A tool-call turn is never judged — not for emptiness, not for
    # refusal, not for language. Recovering from it would discard the tool
    # calls the caller already has.
    if has_tool_calls:
        return QualityGateResult(passed=True, has_tool_calls=True)

    if not response_text.strip():
        return QualityGateResult(passed=False, reasons=["empty_response"], empty_response=True)

    is_refusal = bool(_REFUSAL_PATTERN.search(response_text))
    sensitive = bool(_SENSITIVE_PATTERN.search(user_text))

    if is_refusal:
        if sensitive:
            return QualityGateResult(
                passed=True, is_refusal=True, sensitive_request_detected=True,
            )
        return QualityGateResult(
            passed=False, reasons=["unexpected_refusal"],
            is_refusal=True, sensitive_request_detected=False,
        )

    user_lang = _detect_language(user_text)
    response_lang = _detect_language(response_text)
    language_mismatch = (
        user_lang != "unknown" and response_lang != "unknown" and user_lang != response_lang
    )
    if language_mismatch:
        return QualityGateResult(
            passed=False, reasons=["language_mismatch"],
            language_mismatch=True, user_language=user_lang, response_language=response_lang,
        )

    return QualityGateResult(
        passed=True, user_language=user_lang, response_language=response_lang,
    )


def _last_user_message_content(messages: list[dict]) -> str:
    """Same extraction rule as complexity.py's own private helper of the
    same name — duplicated rather than imported across module-privacy
    boundaries (5 lines; not worth a shared-utility module for this)."""
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def evaluate_from_messages(
    *, messages: list[dict], response_text: str | None, has_tool_calls: bool = False,
) -> QualityGateResult:
    """Convenience wrapper for callers (router.py) that have the full
    request `messages` list rather than an already-extracted user string —
    evaluates the response against the LATEST user turn."""
    return evaluate(
        user_text=_last_user_message_content(messages),
        response_text=response_text,
        has_tool_calls=has_tool_calls,
    )
