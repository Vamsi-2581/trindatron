"""
Capability -> execution mode (TaskType) mapping.

WHAT vs HOW (Execution Mode / Quality Independence etapa, 2026-09-07):
`Capability` describes WHAT a request is about (chat/code/files/research/
work/automation/agent/...). `TaskType` (trinity_router.selector) describes
HOW it must be executed — TaskType.CHAT ("conversational exchange without
tool use") or TaskType.AGENT ("ReAct loop with tool calls"), per that
enum's own inline comments. These are two different questions, and this
module's OLD behavior — a single static dict, CAPABILITY_TASK_TYPE, used
as the sole authority — collapsed them into one: it assumed every CODE/
RESEARCH-classified request always needs the full tool loop, and every
CHAT-classified request never does. That is false in both directions:
"write a function to add two numbers" (topic: code) needs zero tools;
"open the repository and inspect the files" (topic: falls through to the
CHAT default today, since no current classifier rule recognizes
"repository") genuinely needs real file access.

infer_execution_mode() (below) is now the authority for /message — it
determines whether THIS SPECIFIC request expresses real execution intent,
independent of which topic capability it was classified as. FILES/WORK/
AUTOMATION/AGENT keep their unconditional AGENT mapping (their own
classifier trigger phrases already only fire on text that inherently
implies real action — "open the file", "browse to", "automate this
workflow", "act as an agent" — there is no meaningful "topic without
intent" case for these four). Every other capability (CHAT, CODE,
RESEARCH, and the unimplemented multimodal ones) is checked against
_EXECUTION_INTENT_PATTERN instead of being hard-mapped.

CAPABILITY_TASK_TYPE is kept (not deleted) — see its own docstring below
for where it is still legitimately used (LoadClassifier's `forced_task_type`
derivation, and /chat's and /agent's docstrings referencing it for
observability) — but it is no longer /message's authority, and its
CODE/RESEARCH entries no longer describe real routing behavior. Do not
add new call sites that treat it as decisive for CODE/RESEARCH; use
infer_execution_mode() for any new "should this go through Agent" decision.
"""
from __future__ import annotations

import re

from trinity_router import TaskType

from src.capability.classifier import Capability

CAPABILITY_TASK_TYPE: dict[Capability, TaskType] = {
    Capability.CHAT:       TaskType.CHAT,
    Capability.RESEARCH:   TaskType.AGENT,
    Capability.CODE:       TaskType.AGENT,
    Capability.WORK:       TaskType.AGENT,
    # Multimodal capabilities will route to dedicated pipelines in a future phase.
    # For now they fall back to CHAT so the request is not silently dropped.
    Capability.IMAGE:      TaskType.CHAT,
    Capability.VIDEO:      TaskType.CHAT,
    Capability.AUDIO:      TaskType.CHAT,
    Capability.FILES:      TaskType.AGENT,
    Capability.AUTOMATION: TaskType.AGENT,
    Capability.AGENT:      TaskType.AGENT,
    # A deck is delegated execution, not a conversation: it runs for minutes,
    # produces a file, and the user is not waiting on a reply. AGENT is also
    # what makes the load classifier read it as HEAVY, which is what the
    # capacity gate needs to know before admitting one.
    Capability.PRESENTATION: TaskType.AGENT,
}

# Capabilities with no backend implementation yet.
# Services should surface a friendly "coming soon" message for these.
UNIMPLEMENTED_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.IMAGE,
    Capability.VIDEO,
    Capability.AUDIO,
})

# PRESENTATION is deliberately NOT in the set above: a real provider exists.
# Whether it can run in THIS deployment is a separate, runtime question —
# presentation_service.availability() — because it depends on a service being
# configured and reachable, not on whether the code was written.

# Capabilities whose OWN classifier trigger phrases already only fire on
# text that inherently implies real tool/action use — no separate
# execution-intent check needed, unconditional AGENT is correct as-is.
_UNCONDITIONAL_AGENT_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.FILES,
    Capability.WORK,
    Capability.AUTOMATION,
    Capability.AGENT,
    # "Create a presentation about X" already states the execution intent in
    # full; there is no phrasing of it that means "just talk about slides".
    Capability.PRESENTATION,
})

# Execution-intent signals (EN/PT/ES, same style/precedent as
# classifier.py's own rules) — detects that THIS request wants real
# tool/file/execution action, independent of topic capability. Deliberately
# phrase-based (not single keywords, per the "não dependa de uma única
# palavra isolada" requirement): every alternative pairs an action verb
# with its object (file/repository/disk/test/command/web), the same
# precision level classifier.py's own FILES/WORK/AUTOMATION rules use.
_EXECUTION_INTENT_PATTERN = re.compile(
    r"\b("
    # EN
    r"open\s+the\s+(?:file|files|repository|repo)|"
    r"inspect\s+the\s+(?:file|files|repository|repo)|"
    r"modify\s+the\s+(?:file|files|repository|repo|code)|"
    r"(?:save|write)\s+(?:this|it|the\s+\w+)?\s*to\s+disk|"
    r"run\s+the\s+(?:test|tests|script)|"
    r"execute\s+(?:this|the)?\s*(?:command|script)|"
    r"apply\s+the\s+changes|"
    r"(?:operate|work)\s+on\s+the\s+repository|"
    r"search\s+the\s+web|on\s+the\s+web|"
    r"external\s+(?:source|sources|information)|"
    # PT
    r"abra\s+(?:o|os|a|as)\s+(?:arquivos?|reposit[oó]rios?)|"
    r"inspecione\s+(?:o|os|a|as)\s+(?:arquivos?|reposit[oó]rios?)|"
    r"altere\s+(?:o|os|a|as)\s+(?:c[oó]digo|arquivos?|reposit[oó]rios?)|"
    r"(?:salve|grave)\s+(?:isso\s+)?(?:em|no)\s+disco|"
    r"rode\s+os?\s+testes?|"
    r"execute\s+(?:este|o)?\s*(?:comando|script)|"
    r"aplique\s+as\s+mudan[cç]as|"
    r"opere\s+no\s+reposit[oó]rio|"
    r"pesquise\s+na\s+web|na\s+web|"
    r"informa[cç][oõ]es\s+externas|fontes\s+externas|"
    # ES
    r"abre\s+(?:el|los|la|las)\s+(?:archivos?|repositorios?)|"
    r"inspecciona\s+(?:el|los|la|las)\s+(?:archivos?|repositorios?)|"
    r"modifica\s+(?:el|los|la|las)\s+(?:c[oó]digo|archivos?|repositorios?)|"
    r"(?:guarda|escribe)\s+(?:esto\s+)?en\s+disco|"
    r"ejecuta\s+(?:las?)?\s*pruebas?|"
    r"ejecuta\s+(?:este|el)?\s*(?:comando|script)|"
    r"aplica\s+los\s+cambios|"
    r"busca\s+en\s+la\s+web|informaci[oó]n\s+externa"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


# Freshness signals (EN/PT/ES): the request is about how things are NOW, so answering it from
# training data alone is answering it wrong. A model cannot know what changed after its data ends,
# and "latest" asked in 2026 answered from 2024 reads as confident invention. These escalate to the
# tool loop — where web_search exists — but only when this account actually has tools; on a plan
# without them the turn stays a conversation rather than becoming a refusal.
# A bare temporal word is not a freshness question: "how are you today?" is a greeting and "the
# current function signature" is about the code on screen. Escalation therefore needs either a
# phrase that is inherently about the present, or a temporal word paired with something being asked
# about — the same phrase-not-keyword precision the execution-intent pattern above already uses.
_FRESHNESS_INTENT_PATTERN = re.compile(
    r"\b("
    # Inherently about the present, in any of the three languages.
    r"latest|newest|most\s+recent|recently|up\s+to\s+date|state\s+of\s+the\s+art|"
    r"breaking\s+news|headlines|trending|"
    r"[uú]ltim[oa]s?\s+\w+|mais\s+recentes?|recentemente|novidades?|"
    r"lan[cç]amentos?\s+recentes?|tend[eê]ncias?\s+atuais|o\s+que\s+h[aá]\s+de\s+novo|"
    r"m[aá]s\s+recientes?|recientemente|novedades?|tendencias\s+actuales|qu[eé]\s+hay\s+de\s+nuevo"
    r")\b"
    # A temporal word plus the thing being asked about.
    r"|\b(?:what|which|who|how\s+much)\b[^.?!]{0,60}\b(?:today|right\s+now|this\s+(?:week|month|year))\b"
    r"|\b(?:o\s+que|quais|qual|quanto)\b[^.?!]{0,60}\b(?:hoje|agora|nest[ae]\s+(?:semana|m[eê]s|ano))\b"
    r"|\b(?:qu[eé]|cu[aá]l(?:es)?|cu[aá]nto)\b[^.?!]{0,60}\b(?:hoy|ahora|est[ae]\s+(?:semana|mes|a[nñ]o))\b"
    r"|\b(?:current|atual(?:mente)?|actual(?:mente)?)\b[^.?!]{0,30}"
    r"\b(?:version|vers[ãa]o|versi[óo]n|price|pre[çc]o|precio|status|estado|release|news|not[íi]cias?)\b"
    # News and prices of a day: "pesquise as notícias de hoje", "today's news", "cotação do dólar hoje".
    r"|\b(?:not[íi]cias?|news|manchetes|cota[çc][ãa]o|pre[çc]os?|prices?|precios?|clima|weather|tiempo)\b"
    r"[^.?!]{0,30}\b(?:hoje|today|hoy|agora|now|ahora|atuais|actuales)\b"
    r"|\btoday'?s\s+(?:news|headlines|price|prices|weather)\b"
    # An explicit ask to look something up on the web.
    r"|\b(?:pesquis(?:e|a|ar)|busque|procure|search|look\s+up|google|busca(?:r)?)\b[^.?!]{0,50}"
    r"\b(?:na\s+(?:internet|web)|online|on\s+the\s+(?:internet|web)|en\s+(?:internet|la\s+web))\b"
    # A year at or after the one this code was written in: asking about 2026 is asking about now.
    r"|\b20(?:2[6-9]|[3-9]\d)\b",
    re.IGNORECASE | re.UNICODE,
)


def wants_current_information(text: str) -> bool:
    """Whether this request is about the present rather than about knowledge in general."""
    return bool(_FRESHNESS_INTENT_PATTERN.search(text or ""))


# Narrower than freshness: requests that can only be done by looking at the web now — today's news or prices, or
# an explicit "search the internet". "Plan my 2027 trip" is about the present in the freshness sense, but a model can
# still help with it; "pesquise as notícias de hoje" without web search has nothing true to give but the reason.
_SEARCH_NOW_PATTERN = re.compile(
    r"\b(?:not[íi]cias?|news|manchetes|headlines|cota[çc][ãa]o|pre[çc]os?|prices?|precios?)\b"
    r"[^.?!]{0,30}\b(?:de\s+)?(?:hoje|today|hoy|agora|now|ahora)\b"
    r"|\btoday'?s\s+(?:news|headlines|prices?)\b|\bbreaking\s+news\b|[uú]ltimas\s+not[íi]cias|latest\s+news"
    r"|\b(?:pesquis(?:e|a|ar)|busque|procure|search|look\s+up|google|busca(?:r)?)\b[^.?!]{0,50}"
    r"\b(?:na\s+(?:internet|web)|online|on\s+the\s+(?:internet|web)|en\s+(?:internet|la\s+web))\b"
    # An instruction to go and look something up: "search for information about X", "pesquise sobre X". Left in the
    # plain conversation, a model with no tool attached tried to call a search tool anyway and the provider rejected
    # the turn (tool_use_failed). Searching in a file or the code ("procure o erro no arquivo") is not this.
    r"|^\s*(?:pesquis(?:e|a|ar)|busque|buscar?|procure|search(?:\s+for)?|look\s+up|google|investigue)\s+"
    r"(?:(?:por|for|pelas?|pelos?|as|os|the|las|los)\s+)?"
    r"(?:informa[çc](?:[õo]es|[ãa]o)|information|info|sobre|about|acerca\s+de|not[íi]cias|news|dados\s+sobre|data\s+on)\b",
    re.IGNORECASE | re.UNICODE,
)


def needs_web_search(text: str) -> bool:
    """Whether the request cannot be answered truthfully without searching the web right now."""
    return bool(_SEARCH_NOW_PATTERN.search(text or ""))


def _last_user_message_content(messages: list[dict]) -> str:
    """Same extraction convention as trinity_router.complexity's own
    helper — the LAST message with role="user", empty string if none/not
    a plain string. Not imported from there to avoid a new cross-package
    coupling for a two-line helper; kept in lockstep deliberately."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def get_task_type(capability: Capability) -> TaskType:
    """Legacy, capability-only mapping. Kept for callers that only have a
    Capability and no message text (e.g. LoadClassifier's `forced_task_type`
    derivation, which is observability-only — see load_classification.py).
    NOT used by /message anymore for CODE/RESEARCH — see infer_execution_mode()."""
    return CAPABILITY_TASK_TYPE.get(capability, TaskType.CHAT)


def infer_execution_mode(*, capability: Capability, messages: list[dict], agent_available: bool = True) -> TaskType:
    """Determine the TaskType (execution mode) this SPECIFIC request needs,
    independent of topic capability alone.

    FILES/WORK/AUTOMATION/AGENT -> always AGENT (their own classifier
    trigger phrases already imply real action; see module docstring).
    Every other capability (CHAT/CODE/RESEARCH/the unimplemented
    multimodal ones) -> AGENT only if the user's latest message also
    expresses real execution intent (_EXECUTION_INTENT_PATTERN); CHAT
    otherwise. Checked for CHAT too (not just CODE/RESEARCH) because the
    capability classifier itself has gaps — e.g. "open the repository and
    inspect the files" does not match any current FILES/WORK trigger
    phrase and falls through to capability=CHAT, yet unambiguously needs
    tools; skipping the check for capability=CHAT would silently strand
    that request without a toolset.
    """
    if capability in _UNCONDITIONAL_AGENT_CAPABILITIES:
        return TaskType.AGENT
    text = _last_user_message_content(messages)
    # "Você consegue pesquisar na internet?" asks about a capability; it is answered from the matrix in the
    # conversation (capability/questions.py), never escalated — on a plan without tools that would be a 403.
    from src.capability.questions import is_capability_question

    if is_capability_question(text):
        return TaskType.CHAT
    if _EXECUTION_INTENT_PATTERN.search(text):
        return TaskType.AGENT
    # A question about the present needs a look at the present. Only RESEARCH and CHAT escalate this
    # way: a code or media request that happens to say "latest" is not asking for the news.
    # An explicit request to search is the same: it belongs where the search tool is.
    if (agent_available and capability in (Capability.RESEARCH, Capability.CHAT)
            and (_FRESHNESS_INTENT_PATTERN.search(text) or needs_web_search(text))):
        return TaskType.AGENT
    return TaskType.CHAT
