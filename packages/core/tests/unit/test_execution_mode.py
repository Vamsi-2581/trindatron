"""infer_execution_mode() — Execution Mode / Quality Independence etapa
(2026-09-07).

Proves capability (WHAT the task is about) and task_type/execution mode
(HOW it must be executed — CHAT without tools vs AGENT with the ReAct
tool loop) are independent dimensions, replacing the old
CAPABILITY_TASK_TYPE-as-absolute-authority behavior for CODE/RESEARCH
(see pipelines.py's module docstring for the full rationale and root
cause this etapa fixes).
"""
from __future__ import annotations

import pytest

from src.capability.classifier import Capability
from src.capability.pipelines import infer_execution_mode
from trinity_router import TaskType


def _msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def _mode(capability: Capability, text: str) -> TaskType:
    return infer_execution_mode(capability=capability, messages=_msgs(text))


# ── 1/4. CODE text-only -> CHAT ─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "write a Python function to add two numbers",
    "escreva uma função em Python",
    "fix this code",  # pasted code, no file/execution reference
    "corrija este código que colei",
])
def test_code_text_only_maps_to_chat(text):
    assert _mode(Capability.CODE, text) == TaskType.CHAT


# ── 2/3. CODE + real execution intent -> AGENT ──────────────────────────────

@pytest.mark.parametrize("text", [
    "open the repository, modify the files and run the tests",
    "abra os arquivos e altere o código",
    "run the tests and fix the failures",
    "rode os testes e corrija os erros",
    "save this to disk",
])
def test_code_with_execution_intent_maps_to_agent(text):
    assert _mode(Capability.CODE, text) == TaskType.AGENT


# ── 5. RESEARCH explanation (no tool use implied) -> CHAT ───────────────────

@pytest.mark.parametrize("text", [
    "explique este assunto",
    "explain this topic in simple terms",
])
def test_research_explanation_maps_to_chat(text):
    assert _mode(Capability.RESEARCH, text) == TaskType.CHAT


# ── 6. RESEARCH + real web search intent -> AGENT ───────────────────────────

@pytest.mark.parametrize("text", [
    "pesquise na web e traga fontes",
    "research this topic on the web and compare five sources",
    "procure informações externas e compare",
])
def test_research_with_web_intent_maps_to_agent(text):
    assert _mode(Capability.RESEARCH, text) == TaskType.AGENT


# ── A question about the present needs a look at the present ────────────────

@pytest.mark.parametrize("text", [
    "Pesquise os últimos avanços em inteligência artificial generativa",
    "Quais são as novidades de IA desta semana?",
    "O que aconteceu hoje no mercado de tecnologia?",
    "latest developments in generative AI",
    "what is the current version of that library",
    "¿cuáles son las novedades de esta semana?",
    "o que mudou em 2026",
])
def test_a_question_about_now_escalates_to_the_tool_loop(text):
    """Answered from training data alone, these are answered wrong: the model cannot know what
    changed after its data ends. This is the bug where "últimos avanços" came back with 2023 news."""
    assert _mode(Capability.RESEARCH, text) == TaskType.AGENT
    assert _mode(Capability.CHAT, text) == TaskType.AGENT


@pytest.mark.parametrize("text", [
    "Pesquise os últimos avanços em inteligência artificial generativa",
    "latest developments in generative AI",
])
def test_without_tools_a_freshness_question_stays_a_conversation(text):
    """A plan with no agent mode has no web_search to reach. Escalating anyway would turn "what is
    new?" into a 403; the turn stays a conversation, and the prompt's own date block is what keeps
    the answer honest about its age."""
    assert infer_execution_mode(capability=Capability.RESEARCH, messages=_msgs(text),
                                agent_available=False) == TaskType.CHAT


@pytest.mark.parametrize("capability", [Capability.CODE, Capability.IMAGE, Capability.VIDEO])
def test_freshness_words_do_not_escalate_unrelated_work(capability):
    """"Use the latest syntax" is not a request for the news, and a media request never needs the
    tool loop — only RESEARCH and CHAT escalate on freshness."""
    assert _mode(capability, "use the latest syntax") == TaskType.CHAT


# ── 7. Explicit Agent capability -> always AGENT ────────────────────────────

def test_explicit_agent_capability_always_maps_to_agent():
    assert _mode(Capability.AGENT, "do whatever seems reasonable") == TaskType.AGENT


# ── 8. CHAT capability -> CHAT (the common/default case) ────────────────────

@pytest.mark.parametrize("text", [
    "hi",
    "how are you today?",
    "analise profundamente este problema",
    "compare estas duas implementações",
])
def test_chat_capability_default_case_maps_to_chat(text):
    assert _mode(Capability.CHAT, text) == TaskType.CHAT


def test_chat_capability_with_execution_intent_still_escalates_to_agent():
    """The one deliberate exception to 'CHAT capability -> always CHAT':
    the classifier itself has gaps (e.g. no current rule recognizes
    "repository"), so text that unambiguously needs tools can still land
    on capability=CHAT by default. Skipping the intent check there would
    silently strand a genuinely tool-needing request without a toolset —
    see pipelines.py's infer_execution_mode() docstring."""
    assert _mode(Capability.CHAT, "open the repository and inspect the files") == TaskType.AGENT
    assert _mode(Capability.CHAT, "inspect the repository and modify the files") == TaskType.AGENT
    assert _mode(Capability.CHAT, "execute this command and tell me what happened") == TaskType.AGENT


# ── 9/10/11. FILES/WORK/AUTOMATION -> always AGENT (unconditional) ─────────

@pytest.mark.parametrize("capability", [Capability.FILES, Capability.WORK, Capability.AUTOMATION])
def test_unconditional_agent_capabilities_ignore_text_content(capability):
    """These three never need the execution-intent check — their own
    classifier trigger phrases already only fire on text implying real
    action, so even a deliberately bland text still maps to AGENT."""
    assert _mode(capability, "hello") == TaskType.AGENT


# ── 12/13/14. Deep analysis, text-only -> CHAT, reachable at any quality tier ──

def test_deep_analysis_text_only_maps_to_chat():
    assert _mode(Capability.CHAT, "analyze this problem deeply and explain the best solution") == TaskType.CHAT


def test_deep_analysis_can_reach_standard_quality():
    from trinity_router import compute_complexity

    # reasoning(15) + code_signal(15, "function") + length(20, ~3000 chars) = 50 -> standard
    text = (
        "Why does this design choice matter, and how would you compare it to "
        "an alternative approach for this function, considering exception handling? "
        + ("Think through each affected caller carefully. " * 65)
    )
    result = compute_complexity(capability="chat", messages=_msgs(text), tools=None)
    assert result.required_quality_class in ("standard", "premium")
    assert result.signals["tool_signal"] == 0


def test_deep_analysis_can_reach_premium_quality_text_only():
    from trinity_router import compute_complexity

    long_text = (
        "Explain why this design choice matters, and compare it step by step to "
        "alternative approaches, including how the current function signature "
        "affects long-term maintainability. "
        + ("Consider this scenario carefully and weigh the tradeoffs of each "
           "alternative in depth. " * 60)
    )
    result = compute_complexity(capability="chat", messages=_msgs(long_text), tools=None)
    assert result.required_quality_class == "premium"
    assert result.signals["tool_signal"] == 0


# ── 15/16. Premium text-only stays CHAT; premium + tools goes AGENT ─────────

def test_premium_text_only_stays_chat_task_type():
    """execution_mode is decided from TEXT INTENT alone — a request that
    happens to score 'premium' in complexity, but expresses no execution
    intent, must still be CHAT. Quality and execution mode are genuinely
    independent axes."""
    long_text = (
        "Explain why this design choice matters, and compare it step by step to "
        "alternative approaches, including how the current function signature "
        "affects long-term maintainability. "
        + ("Consider this scenario carefully and weigh the tradeoffs of each "
           "alternative in depth. " * 60)
    )
    assert _mode(Capability.CHAT, long_text) == TaskType.CHAT


def test_premium_with_execution_intent_maps_to_agent():
    long_text = (
        "Open the repository, modify the architecture, run the tests and fix the "
        "failures. " + ("Consider every affected module carefully and explain each "
                          "change in depth. " * 60)
    )
    assert _mode(Capability.CODE, long_text) == TaskType.AGENT
