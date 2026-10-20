"""Which of pt/en/es an answer is written in.

The order is always the same, and it is an order of evidence, not of guessing:

1. **What the person chose.** Settings offers Portuguese, English and Spanish; a chosen language is an instruction
   and nothing overrides it.
2. **What their device is set to.** "Automatic" means the phone's or browser's own language, sent by the client with
   the request. This is the case that used to be missing: a phone set to English asked a question with no accented
   word in it and was answered in Portuguese.
3. **What they wrote.** Marker-matching over the text, the last resort rather than the first — it reads a short
   message like "ok" or "status 500?" as Portuguese, and read one real Portuguese sentence as Spanish on a device
   under test.

Everything that answers without a model (the identity reply, the capability replies, the media reply) resolves the
language here, so one question gets one language whichever path answers it.
"""
from __future__ import annotations

from src.prompting.identity_guard import detect_language

#: The languages the product is translated into and answers in.
LANGUAGES: tuple[str, ...] = ("pt", "en", "es")
DEFAULT_LANGUAGE = "pt"


def normalise(tag: str | None) -> str:
    """"pt-BR", "PT_br", "pt" → "pt"; anything else → "" (including the "auto" preference)."""
    code = (tag or "").strip().replace("_", "-").split("-")[0].lower()
    return code if code in LANGUAGES else ""


def known_language(preference: str | None = None, client_locale: str | None = None) -> str:
    """The person's choice, else their device's language; "" when neither is known (an older client on "automatic").

    "" is not a language: each model-free reply then reads the text with its own hints, exactly as before, because
    those hints know their own vocabulary better than a general detector does ("Crea una imagen" has no general
    Spanish marker, but the media path's hints read it right).
    """
    return normalise(preference) or normalise(client_locale)


def resolve_answer_language(
    preference: str | None = None,
    client_locale: str | None = None,
    text: str = "",
) -> str:
    """The language to answer in: the person's choice, else their device, else the language they wrote in."""
    return known_language(preference, client_locale) or detect_language(text)
