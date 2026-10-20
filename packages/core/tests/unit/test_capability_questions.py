"""Questions about what TRINDATRON can do are recognised, and answered from the capability matrix alone.

Deterministic: fixed strings and fixed matrices, no model.
"""
from __future__ import annotations

import pytest

from src.capability.classifier import Capability, classify
from src.capability.pipelines import TaskType, infer_execution_mode, needs_web_search
from src.capability.questions import asked_capabilities, capability_reply, search_unavailable_reply
from src.prompting import POLICY_V2, get_policy
from src.prompting.task_context import TaskContext
from src.prompting.tool_policy import ToolPolicy


@pytest.mark.parametrize("text,expected", [
    ("Você consegue criar imagens?", ("image_generation",)),
    ("consegue criar imagens?", ("image_generation",)),
    ("Você consegue criar vídeos para mim?", ("video_generation",)),
    ("você faz vídeos?", ("video_generation",)),
    ("e vídeos?", ("video_generation",)),
    # The Android test's follow-up, word for word.
    ("Nem. Vídeos?", ("video_generation",)),
    ("Você consegue criar imagens ou vídeos?", ("image_generation", "video_generation")),
    ("Você consegue pesquisar na Internet?", ("web_search",)),
    ("Você tem acesso à internet?", ("web_search",)),
    ("Você consegue editar fotos?", ("image_editing",)),
    ("Você consegue ler PDFs?", ("file_analysis",)),
    ("Você consegue usar o computador?", ("computer_control",)),
    ("Can you create images?", ("image_generation",)),
    ("Can you search the web?", ("web_search",)),
    ("Do you make videos?", ("video_generation",)),
    ("¿Puedes crear videos?", ("video_generation",)),
    ("y videos?", ("video_generation",)),
])
def test_a_question_about_a_capability_names_it(text, expected):
    assert asked_capabilities(text) == expected


@pytest.mark.parametrize("text", [
    # A request that names what to make is a creation, not a question about the capability.
    "Você consegue criar uma imagem de um gato astronauta?",
    "Crie a imagem dele",
    "gera o vídeo",
    "Pesquise as notícias de hoje.",
    "Quem é você?",
    "Qual é sua data de conhecimento?",
    "Explique vídeos de física",
    "O que é um vídeo?",
    "Você pode me ajudar com meu texto?",
    "Você pode ver meu código?",
    "Do you have a website?",
])
def test_anything_else_is_not_a_capability_question(text):
    assert asked_capabilities(text) == ()


@pytest.mark.parametrize("text,capability", [
    ("crie uma imagem de um farol", Capability.IMAGE),
    ("Crie a imagem dele", Capability.IMAGE),
    ("gera uma imagem de praia", Capability.IMAGE),
    ("gera a imagem dele", Capability.IMAGE),
    ("me dá uma imagem de um cachorro", Capability.IMAGE),
    ("Você consegue criar uma imagem de um gato?", Capability.IMAGE),
    ("create the image", Capability.IMAGE),
    ("crea la imagen", Capability.IMAGE),
    ("crie um vídeo do mar", Capability.VIDEO),
    ("gera o vídeo", Capability.VIDEO),
    ("gerar um vídeo curto", Capability.VIDEO),
    ("genera un video de un perro", Capability.VIDEO),
    # Questions about the capability never start a job.
    ("consegue criar imagens?", Capability.CHAT),
    ("você faz vídeos?", Capability.CHAT),
    ("e vídeos?", Capability.CHAT),
    # Words that only look like it.
    ("gera um resumo", Capability.CHAT),
    ("crie a tabela", Capability.CHAT),
    ("qual o formato de imagem ideal?", Capability.CHAT),
])
def test_the_classifier_accepts_articles_and_pronouns(text, capability):
    assert classify(text) is capability


def test_a_capability_question_stays_a_conversation_even_where_tools_exist():
    """"pesquisar na internet" reads as execution intent; asked as a question it must not become an agent run
    (and on a plan without tools, a 403)."""
    messages = [{"role": "user", "content": "Você consegue pesquisar na Internet?"}]
    assert infer_execution_mode(capability=classify(messages[0]["content"]), messages=messages) is TaskType.CHAT


MATRIX = {
    "image_generation": {"available": True, "status": "available"},
    "video_generation": {"available": False, "status": "plan"},
    "web_search": {"available": False, "status": "unavailable"},
    "avatar_generation": {"available": False, "status": "not_integrated"},
}


@pytest.mark.parametrize("name,status,phrase", [
    ("image_generation", "available", "está disponível para você agora"),
    ("image_generation", "plan", "não está incluído no seu plano atual"),
    ("image_generation", "unavailable", "temporariamente indisponível"),
    ("image_generation", "not_integrated", "ainda não está integrado"),
])
def test_the_answer_is_the_state_in_the_matrix(name, status, phrase):
    reply = capability_reply("Você consegue criar imagens?", {name: {"available": status == "available", "status": status}})
    assert phrase in reply
    lowered = reply.lower()
    for refusal in ("não consigo", "não posso", "não oferecemos", "youtube", "dall", "runway"):
        assert refusal not in lowered


def test_several_capabilities_are_answered_in_the_order_asked():
    reply = capability_reply("Você consegue criar imagens ou vídeos?", MATRIX)
    assert reply.index("Criar imagens está disponível") < reply.index("Criar vídeos não está incluído no seu plano")


@pytest.mark.parametrize("text,phrase", [
    ("Can you create videos?", "not included in your current plan"),
    ("¿Puedes crear videos?", "no está incluido en tu plan actual"),
])
def test_the_answer_follows_the_persons_language(text, phrase):
    assert phrase in capability_reply(text, MATRIX)


def test_a_capability_missing_from_the_matrix_is_never_promised():
    assert "ainda não está integrado" in capability_reply("Você consegue criar vídeos?", {})


@pytest.mark.parametrize("text,needs", [
    ("Pesquise as notícias de hoje.", True),
    ("Quais as últimas notícias?", True),
    ("pesquise na internet sobre baleias", True),
    ("Qual a cotação do dólar hoje?", True),
    ("What are today's headlines?", True),
    ("Planeje minha viagem de 2027", False),
    ("Quem é o presidente atual dos Estados Unidos?", False),
    ("Resuma estas notícias que colei", False),
    # An explicit instruction to look something up.
    ("Search for information about Python", True),
    ("Pesquise sobre baleias", True),
    ("Procure por informações sobre o SUS", True),
    # Searching inside the work is not a web search.
    ("procure o erro no arquivo main.py", False),
    ("Search the codebase for TODOs", False),
])
def test_a_request_that_only_the_web_can_answer_is_recognised(text, needs):
    assert needs_web_search(text) is needs


@pytest.mark.parametrize("text", ["Search for information about Python", "Pesquise sobre baleias"])
def test_an_explicit_search_goes_where_the_search_tool_is(text):
    """Left in the plain conversation, a model with no tool attached tried to call one and the provider rejected
    the turn (tool_use_failed). Where agent mode exists the tool loop searches; where it does not, the reason is
    answered from the matrix (services/product_answers.py)."""
    messages = [{"role": "user", "content": text}]
    assert infer_execution_mode(capability=classify(text), messages=messages, agent_available=True) is TaskType.AGENT
    assert infer_execution_mode(capability=classify(text), messages=messages, agent_available=False) is TaskType.CHAT


def test_without_web_search_the_reason_comes_from_the_matrix_and_with_it_the_search_runs():
    reply = search_unavailable_reply("Pesquise as notícias de hoje.", {"web_search": {"available": False, "status": "plan"}})
    assert reply.startswith("Pesquisar na web não está incluído no seu plano atual")
    assert "não consigo" not in reply
    assert search_unavailable_reply("Search the web for today's news", {"web_search": {"status": "unavailable"}}).startswith(
        "Searching the web is part of TRINDATRON, but it is temporarily unavailable"
    )
    assert search_unavailable_reply("Pesquise as notícias de hoje.", {"web_search": {"available": True, "status": "available"}}) is None
    # Answered in the language asked: on the real Core this English request first came back in Portuguese.
    assert search_unavailable_reply("Search for information about Python", {"web_search": {"status": "plan"}}).startswith(
        "Searching the web is not included in your current plan"
    )


# ── Identity and currency: what the model is told ─────────────────────────────


def test_the_current_policy_gives_the_identity_and_forbids_a_training_cutoff():
    policy = get_policy()
    assert policy.version == POLICY_V2
    assert "<identity>" in policy.text and "<currency>" in policy.text
    assert "You are TRINDATRON" in policy.text
    assert "Never state a training cutoff" in policy.text
    # A limit the capability block does not state may not be claimed.
    assert "I have no internet access" in policy.text and "never send the person to an outside service" in policy.text


def test_nothing_the_model_is_told_says_its_knowledge_ends():
    """The 2023 cutoff in the Android test was the model's own; nothing in the prompt may invite it."""
    rendered = TaskContext(today="2026-09-17").render() + ToolPolicy.none().render() + get_policy().text
    lowered = rendered.lower()
    assert "your own knowledge ends before" not in lowered and "answer directly from what you know" not in lowered
    assert "never quote a training cutoff" in lowered
    assert "not a limit of trindatron" in lowered
