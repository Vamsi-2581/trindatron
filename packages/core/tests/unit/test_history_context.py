"""Model context built from conversation history (src/prompting/history.py).

The old-conversation case is the one the Web validation of 2026-09-16 reproduced: a conversation whose earlier
replies said "Não consigo criar…" kept answering "Não consigo criar vídeos." although the <capabilities> block said
video generation was simply not in the plan. The replies below are the ones stored in that conversation.
"""
from dataclasses import dataclass

import pytest

from src.prompting.history import conversation_context, states_capability_availability


@dataclass
class Turn:
    role: str
    content: str


PHOTOSYNTHESIS_Q = "Explique como funciona a fotossíntese em 6 parágrafos."
PHOTOSYNTHESIS_A = "# Fotossíntese\n\nA fotossíntese é o processo pelo qual as plantas convertem luz em energia."
CURRENT = "Você consegue criar vídeos para mim?"


def old_conversation() -> list[Turn]:
    return [
        Turn("user", "Crie uma imagem pra mim de uma leão bem realista"),
        Turn("assistant", "Você já usou as criações de hoje do seu plano. Elas renovam amanhã."),
        Turn("user", "Onde vc consegue"),
        Turn("assistant", "Desculpe, mas não consigo gerar vídeos aqui."),
        Turn("user", "Então gere um vídeo pra mim, quantos segundos vc consegue criar"),
        Turn("assistant", "Não consigo criar imagens ou vídeos. Você pode usar ferramentas de geração de imagens como "
                          "DALL‑E, Midjourney, Stable Diffusion ou bancos de imagens (Unsplash, Shutterstock)."),
        Turn("user", PHOTOSYNTHESIS_Q),
        Turn("assistant", PHOTOSYNTHESIS_A),
        Turn("user", "Você consegue criar imagens ou vídeos?"),
        Turn("assistant", "Não consigo criar imagens nem vídeos."),
        Turn("user", "Você cria vídeos?"),
        Turn("assistant", "Não consigo criar vídeos."),
        Turn("user", CURRENT),
    ]


def test_old_conversation_no_longer_hands_the_model_its_earlier_refusals():
    context = conversation_context(old_conversation())

    assert not any("consigo" in m["content"].lower() and m["role"] == "assistant" for m in context)
    # What remains is everything else, in the order it was said, still alternating and ending on the current turn.
    assert context == [
        {"role": "user", "content": "Crie uma imagem pra mim de uma leão bem realista"},
        {"role": "assistant", "content": "Você já usou as criações de hoje do seu plano. Elas renovam amanhã."},
        {"role": "user", "content": PHOTOSYNTHESIS_Q},
        {"role": "assistant", "content": PHOTOSYNTHESIS_A},
        {"role": "user", "content": CURRENT},
    ]


def test_a_correct_answer_about_availability_is_not_replayed_either():
    # Right on the day it was said; after the plan is upgraded it would answer "not in this plan" all over again.
    history = [
        Turn("user", "Você cria vídeos?"),
        Turn("assistant", "Criar vídeos não está disponível neste plano."),
        Turn("user", CURRENT),
    ]
    assert conversation_context(history) == [{"role": "user", "content": CURRENT}]


def test_new_conversation_is_passed_through_untouched():
    assert conversation_context([Turn("user", CURRENT)]) == [{"role": "user", "content": CURRENT}]


def test_ordinary_history_is_passed_through_untouched():
    history = [
        Turn("user", PHOTOSYNTHESIS_Q),
        Turn("assistant", PHOTOSYNTHESIS_A),
        Turn("user", "Resuma em uma frase"),
        Turn("assistant", "Plantas transformam luz, água e CO₂ em glicose e oxigênio."),
        Turn("user", "Obrigado"),
    ]
    assert conversation_context(history) == [{"role": t.role, "content": t.content} for t in history]


def test_the_turn_being_answered_is_always_kept():
    history = [Turn("user", "Oi"), Turn("assistant", "Não consigo criar vídeos.")]
    # The last turn is the one being answered; it is never left out, whatever it says.
    assert conversation_context(history)[-1] == {"role": "assistant", "content": "Não consigo criar vídeos."}


def test_user_turns_are_never_judged():
    history = [Turn("user", "Não consigo criar imagens no Photoshop, me ajuda?"), Turn("assistant", "Claro!"),
               Turn("user", CURRENT)]
    assert len(conversation_context(history)) == 3


def test_a_claim_without_its_question_leaves_only_itself():
    history = [Turn("assistant", "Olá! Como posso ajudar?"), Turn("assistant", "Não consigo criar vídeos."),
               Turn("user", CURRENT)]
    assert conversation_context(history) == [
        {"role": "assistant", "content": "Olá! Como posso ajudar?"},
        {"role": "user", "content": CURRENT},
    ]


@pytest.mark.parametrize("reply", [
    "Não consigo criar vídeos.",
    "Desculpe, mas não consigo gerar vídeos aqui.",
    "Infelizmente não posso criar imagens.",
    "**Não consigo** gerar apresentações por aqui.",
    "I can't create images.",
    "Sorry, but I can’t generate videos.",
    "I am unable to browse the internet.",
    "Lo siento, no puedo crear imágenes.",
    "No tengo la capacidad de generar videos.",
    # The same statement worded as the product's state goes stale just the same.
    "Criar vídeos não está disponível no seu plano atual.",
    "Não é possível criar imagens ou vídeos.",
    "A geração de imagens está indisponível agora.",
    "Video generation is not available right now.",
    "La creación de videos no está disponible.",
])
def test_recognises_replies_that_open_by_stating_a_capability_is_out_of_reach(reply):
    assert states_capability_availability(reply)


@pytest.mark.parametrize("reply", [
    PHOTOSYNTHESIS_A,
    "Esse artigo não está disponível no site, mas encontrei um resumo.",
    "Aqui está a imagem que você pediu.",
    "Não consigo acessar esse link, mas posso resumir o texto se você colar aqui.",
    "Posso ajudar com isso. Não consigo criar vídeos, mas posso escrever o roteiro.",
    "Você já usou as criações de hoje do seu plano. Elas renovam amanhã.",
    "",
])
def test_leaves_every_other_reply_alone(reply):
    assert not states_capability_availability(reply)
