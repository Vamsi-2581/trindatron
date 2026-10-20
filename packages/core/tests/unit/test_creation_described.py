"""A creation described instead of ordered is still a creation; a question about one is not; a "sim" answers an offer."""
from __future__ import annotations

import pytest

from src.capability.classifier import Capability, classify
from src.services.chat_media import _YES


@pytest.mark.parametrize("text, capability", [
    # The scene that was answered "video creation is not available" (production, 2026-09-27).
    ("Wide cinematic shot at golden hour on Lago do Manso, a small wooden boat glides across calm water, gentle "
     "camera dolly following the boat. Photorealistic, natural colors, with audio: a man shouting", Capability.VIDEO),
    ("Vídeo de um barco no pôr do sol no Manso", Capability.VIDEO),
    ("um vídeo de ondas quebrando nas pedras", Capability.VIDEO),
    ("aerial drone shot over a rainforest river at dawn", Capability.VIDEO),
    ("plano aéreo de uma cachoeira na Chapada", Capability.VIDEO),
    ("Uma imagem de um farol à noite", Capability.IMAGE),
    ("foto de um gato astronauta", Capability.IMAGE),
])
def test_a_creation_described_is_a_creation(text, capability):
    assert classify(text) is capability


@pytest.mark.parametrize("text", [
    "me explique o que é time-lapse",
    "o vídeo que você fez ficou ruim, por quê?",
    "qual o melhor ângulo de câmera para vlog?",
    "Vídeo de um barco no pôr do sol?",
    "como fazer um plano aéreo com drone",
    "what is a dolly shot",
])
def test_a_question_about_a_technique_or_a_video_is_conversation(text):
    assert classify(text) is Capability.CHAT


@pytest.mark.parametrize("text", ["pesquise vídeos de drone sobre o Manso", "search the web for cinematic shots"])
def test_explicit_rules_still_win(text):
    assert classify(text) is Capability.RESEARCH


@pytest.mark.parametrize("text, is_yes", [
    ("sim", True), ("Sim, pode", True), ("pode sim!", True), ("confirmo", True), ("yes please", True),
    ("sí, dale", True), ("ok", True),
    ("sim mas quero em 720p", False), ("não", False), ("sim, e depois faça outro de um carro", False),
])
def test_only_a_short_yes_accepts_an_offer(text, is_yes):
    assert bool(_YES.match(text)) is is_yes
