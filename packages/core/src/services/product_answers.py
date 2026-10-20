"""Answers TRINDATRON gives about itself, before any model runs.

Two kinds of question are about the product rather than about the person's work, and the product — not a model's
beliefs about language models — holds the answer:

* what it runs on ("qual modelo você usa?") — never disclosed (prompting/identity_guard.py);
* what it can do right now ("você consegue criar vídeos?") — the capability matrix for this person
  (capability/questions.py).

Both cost nothing and reach no model. Anything else returns None and goes on to the model, which is told who it is
and what it can do by the system policy and the request's capability block.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session as DBSession

from src.capability.pipelines import needs_web_search
from src.capability.questions import asked_capabilities, capability_reply, search_unavailable_reply
from src.prompting.identity_guard import is_architecture_probe, neutral_identity_reply

logger = logging.getLogger(__name__)


async def current_matrix(db: DBSession, user_id: str) -> dict[str, dict] | None:
    """This person's capability matrix: the one their client last received when recent, otherwise freshly built."""
    from src.models.user import User
    from src.services import capability_service

    cached = capability_service.cached_matrix(user_id)
    if cached is not None:
        return cached
    user = await asyncio.to_thread(lambda: db.query(User).filter(User.id == user_id).one_or_none())
    if user is None:
        return None
    return (await capability_service.user_capabilities(db, user))["matrix"]


async def product_answer(db: DBSession, user_id: str, text: str, language: str | None = None) -> str | None:
    """TRINDATRON's own reply to a question about itself, or None when the question is for the model.

    `language` is the resolved answer language (src/language.py): the person's choice, then their device. These
    replies never reach a model, so without it they would be the one place that still guesses from the text.
    """
    if is_architecture_probe(text):
        return neutral_identity_reply(text, language)
    names = asked_capabilities(text)
    searching = not names and needs_web_search(text)
    if not names and not searching:
        return None
    try:
        matrix = await current_matrix(db, user_id)
    except Exception:
        # Without the matrix there is nothing true to say about the capability; the model, told the same
        # rules, answers instead of the person getting an error.
        logger.warning("product_answers: capability matrix unavailable", exc_info=True)
        return None
    if matrix is None:
        return None
    if searching:
        # "Pesquise as notícias de hoje" where web search is not available: the only true answer is why. Measured
        # on the real Core, a model told the same thing still wrote "não consigo", switched language, or named the
        # internal capability. Where web search is available this returns None and the tool loop searches.
        return search_unavailable_reply(text, matrix, language)
    return capability_reply(text, matrix, names, language)
