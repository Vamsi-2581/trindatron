"""The answer's language comes from the person's settings and device, not from guessing at their words (src/language.py).

The five phrases are the ones checked by hand on a physical phone; each goes down a different path (the model, a
capability answer, the search answer, an image, a video), and each has to come back in the configured language.
"""
from __future__ import annotations

import pytest

from src.capability.pipelines import needs_web_search
from src.capability.questions import capability_reply, search_unavailable_reply
from src.language import normalise, resolve_answer_language
from src.prompting.identity_guard import detect_language, neutral_identity_reply
from src.prompting.task_context import TaskContext
from src.services.chat_media import language_for

# Web search off, image and video on: the Free matrix as the device saw it.
MATRIX = {
    "web_search": {"status": "plan", "available": False},
    "image": {"status": "available", "available": True},
    "video": {"status": "available", "available": True},
}


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("pt-BR", "pt"), ("PT_br", "pt"), ("en-US", "en"), ("es-419", "es"), ("es", "es"),
     ("auto", ""), ("fr-FR", ""), ("", ""), (None, "")],
)
def test_normalise_reads_a_device_tag_and_nothing_else(tag, expected):
    assert normalise(tag) == expected


def test_a_chosen_language_beats_the_device_and_the_device_beats_the_text():
    portuguese = "Você faz busca na internet?"
    assert resolve_answer_language("es", "en-US", portuguese) == "es"
    assert resolve_answer_language("auto", "en-US", portuguese) == "en"
    assert resolve_answer_language(None, "en", portuguese) == "en"
    # Only an older client that sends no locale falls back to reading the text.
    assert resolve_answer_language("auto", None, portuguese) == "pt"


def test_a_single_word_no_longer_decides_the_language():
    # "buscar" is Spanish and Portuguese; this sentence used to be answered in Spanish.
    assert resolve_answer_language("auto", "pt-BR", "Pode buscar isso?") == "pt"
    assert resolve_answer_language("auto", "en-US", "buscar") == "en"


@pytest.mark.parametrize("language", ["pt", "en", "es"])
def test_the_model_is_told_the_configured_language_whatever_the_message_is_in(language):
    names = {"pt": "Portuguese (Brazil)", "en": "English", "es": "Spanish"}
    rendered = TaskContext(language=names[language]).render()
    assert f"Respond in {names[language]}" in rendered
    assert "even when their message is in another language" in rendered
    # The old rule — follow whatever the text is in — is gone.
    assert "follow theirs instead" not in rendered


def test_without_a_known_language_the_model_still_mirrors_the_person():
    assert "language the person is writing in" in TaskContext().render()


@pytest.mark.parametrize("language", ["pt", "en", "es"])
def test_the_five_device_phrases_follow_the_configured_language(language):
    # 1. "Bom dia, quantos anos você tem?" goes to the model, told the language above; the identity reply —
    #    the one model-free answer about TRINDATRON itself — follows it too.
    assert neutral_identity_reply("Bom dia, quantos anos você tem?", language) == neutral_identity_reply("", language)
    assert neutral_identity_reply("", language) != neutral_identity_reply("", {"pt": "en", "en": "es", "es": "pt"}[language])

    # 2. "Você faz busca na internet?" — needs the web (services/product_answers.py), so with search off on this
    #    plan the product answers why, from the matrix.
    question = "Você faz busca na internet?"
    assert needs_web_search(question)
    reply = search_unavailable_reply(question, MATRIX, language)
    assert reply is not None
    assert detect_language(reply) == language, reply

    # 3. "Procure isso na internet." — search is off on this plan, so the answer is why, in the configured language.
    search = search_unavailable_reply("Procure isso na internet.", MATRIX, language)
    assert search is not None
    assert detect_language(search) == language, search

    # 4 and 5. "Crie uma imagem." / "Faça um vídeo." — the media reply's language.
    assert language_for("Crie uma imagem.", language) == language
    assert language_for("Faça um vídeo.", language) == language


def test_replies_without_a_language_keep_reading_the_text():
    # An older client sends no locale: behaviour is exactly what it was.
    assert language_for("Crie uma imagem.") == "pt"
    assert language_for("Create an image.") == "en"
    assert capability_reply("Can you search the web?", MATRIX, ("web_search",)) == capability_reply(
        "Can you search the web?", MATRIX, ("web_search",), "en"
    )
