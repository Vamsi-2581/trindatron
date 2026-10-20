"""Opening a link is a browser task (capability/classifier.py WORK rule).

On a physical phone, a MAX account sent "Abra https://example.com e me diga qual é o título da página." It was
classified as chat, reached no tool, and the model answered that web access was unavailable.
"""
import pytest

from src.capability.classifier import Capability, classify
from src.capability.pipelines import TaskType, infer_execution_mode


@pytest.mark.parametrize("text", [
    "Abra https://example.com e me diga qual é o título da página.",
    "Abra https://example.com",
    "abra o site example.com",
    "Entre no site g1.globo.com",
    "Visite www.wikipedia.org",
    "Open https://example.com and tell me the title",
    "Abre https://example.com y dime el título",
])
def test_opening_a_link_goes_to_the_agent(text):
    capability = classify(text)
    assert capability is Capability.WORK
    mode = infer_execution_mode(capability=capability, messages=[{"role": "user", "content": text}], agent_available=True)
    assert mode is TaskType.AGENT


@pytest.mark.parametrize("text", [
    "abra o relatorio.pdf",
    "Abra sua mente para novas ideias",
    "Me explique o que é example.com",
    "Qual é o melhor .com para comprar?",
])
def test_words_that_only_look_like_it_stay_a_conversation(text):
    assert classify(text) is not Capability.WORK
