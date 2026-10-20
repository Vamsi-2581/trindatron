"""Conversation history as model context.

The messages table is the record of a conversation: what the chat shows, kept as it was said. Model context is a
different thing: what the model builds this turn on. The two differ in one respect only.

An earlier assistant reply that states whether TRINDATRON can do something ("Não consigo criar vídeos.") was written
under the capability state of its own time. Replayed verbatim it outweighs the current <capabilities> block, whatever
that block says and wherever it sits. Measured on groq/openai/gpt-oss-20b against a real conversation carrying four
such replies (5 samples each):

    history replayed as it is stored                                  5/5 repeated the permanent refusal
    same, with the current capability state restated before the turn  5/5 repeated it
    same, with the history rendered as a transcript instead of turns  5/5 repeated it
    same, with those question/claim exchanges left out of the context 0/5 — every answer named the current state

So what TRINDATRON can do comes from one place, the current <capabilities> list, and never from what an earlier reply
said about it. Those exchanges stay in the database and in the chat; they are only not handed to the model as
something to build on. Every other turn is passed through unchanged, in order.

Recognising such a reply is deterministic, like the capability classifier: its FIRST sentence has to be about
something a capability covers and say that it cannot be done or is not available (EN / PT-BR / ES) — worded as a
refusal or as the product's state, since both go stale when the state changes. An answer that mentions a limitation
later on is left alone, and user turns are never touched.
"""
from __future__ import annotations

import logging
import re
from typing import Protocol, Sequence

logger = logging.getLogger(__name__)


class _Turn(Protocol):
    role: str
    content: str


# What a capability is about, in the words a reply uses for it. Nouns, not verbs: a claim names the thing it cannot do.
_CAPABILITY_SUBJECT = re.compile(
    r"\b("
    # image generation / editing / vision
    r"im[aá]g(?:em|ens|e|es|en|enes)|fotos?|photos?|pictures?|ilustra[çc](?:[ãa]o|[õo]es)|illustrations?|"
    r"ilustraci[óo]n(?:es)?|desenhos?|drawings?|dibujos?|logos?|"
    # video
    r"v[ií]deos?|anima[çc](?:[ãa]o|[õo]es)|animations?|animaci[óo]n(?:es)?|clipes?|clips?|"
    # voice and audio
    r"[áa]udios?|voz|voice|fala|speech|"
    # documents
    r"apresenta[çc](?:[ãa]o|[õo]es)|presentations?|presentaci[óo]n(?:es)?|slides?|diapositivas?|"
    # agent, browser, computer, code
    r"agentes?|agents?|navegador|browser|navegar|browse|internet|computador|computer|ordenador|c[óo]digo|code"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# A reply that opens by saying it cannot. Optional apology and "but", then the inability itself.
_OPENS_WITH_INABILITY = re.compile(
    r"^\W*"
    r"(?:(?:desculpe|desculpa|sinto\s+muito|infelizmente|sorry|unfortunately|lo\s+siento|lamentablemente)\W+)?"
    r"(?:(?:mas|but|pero)\s+)?"
    r"(?:(?:eu|i|yo)\s+)?"
    r"(?:(?:ainda|still|todav[ií]a|a[uú]n)\s+)?"
    r"(?:"
    r"n[ãa]o\s+(?:consigo|posso|sou\s+capaz|tenho\s+(?:como|capacidade|acesso))|"
    r"can(?:not|['’]t)|am\s+(?:not\s+able|unable)|(?:do\s+not|don['’]t)\s+have\s+(?:the\s+)?(?:ability|capability|access)|"
    r"no\s+(?:puedo|soy\s+capaz|tengo\s+(?:la\s+)?(?:capacidad|acceso))"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# The same statement worded as the product's state ("Criar vídeos não está disponível neste plano."). Correct when it
# was said, and stale the moment the state changes — after an upgrade it contaminates exactly like a refusal does.
_UNAVAILABLE = re.compile(
    r"\b("
    r"n[ãa]o\s+(?:est[áa]\s+(?:dispon[íi]vel|inclu[íi]d[oa]|habilitad[oa]|liberad[oa])|[ée]\s+poss[íi]vel|faz\s+parte)|"
    r"indispon[íi]vel|"
    r"(?:is\s+not|isn['’]t|are\s+not|aren['’]t)\s+(?:currently\s+)?(?:available|included|enabled|possible)|"
    r"not\s+(?:currently\s+)?available|unavailable|"
    r"no\s+(?:est[áa]\s+(?:disponible|incluid[oa]|habilitad[oa])|es\s+posible)"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

_FIRST_SENTENCE = re.compile(r"^[^.!?\n]*[.!?\n]?")


def states_capability_availability(reply: str) -> bool:
    """True when an assistant reply opens by stating that something a capability covers cannot be done.

    Either wording counts — "não consigo criar vídeos" and "criar vídeos não está disponível" — because either one,
    replayed, answers today's question with the state of the day it was said.
    """
    first = _FIRST_SENTENCE.match(reply.strip())
    sentence = first.group(0) if first else ""
    if not _CAPABILITY_SUBJECT.search(sentence):
        return False
    return bool(_OPENS_WITH_INABILITY.search(sentence) or _UNAVAILABLE.search(sentence))


def conversation_context(history: Sequence[_Turn]) -> list[dict]:
    """The stored conversation as the model should receive it, oldest first.

    `history` ends with the turn being answered; that turn is always kept. An earlier assistant reply that states
    capability availability is left out together with the user turn it answered, so the exchange disappears as a
    whole and the remaining turns still alternate the way they were said.
    """
    turns = list(history)
    left_out: set[int] = set()
    for index, turn in enumerate(turns[:-1]):
        if turn.role != "assistant" or not states_capability_availability(turn.content or ""):
            continue
        left_out.add(index)
        if index > 0 and turns[index - 1].role == "user" and (index - 1) not in left_out:
            left_out.add(index - 1)

    if left_out:
        logger.info("prompting: earlier capability-availability replies left out of model context | exchanges=%d",
                    sum(1 for i in left_out if turns[i].role == "assistant"))
    return [{"role": turn.role, "content": turn.content} for index, turn in enumerate(turns) if index not in left_out]
