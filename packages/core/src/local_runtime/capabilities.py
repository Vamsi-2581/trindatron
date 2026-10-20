"""Status of the experimental local capabilities, in one place. Internal (operators, health), never user-facing.

    llm.local        src/local_runtime/provider.py            LOCAL_LLM_ENABLED
    stt.local        src/voice/adapters/whisper_local.py      STT_LOCAL_ENABLED
    web_research     src/capability/web_research/capability.py WEB_RESEARCH_ENABLED
    groq.oss_120b    src/local_runtime/groq_candidate.py      GROQ_OSS_120B_ENABLED

All four are off by default and removable: each is imported only here, in main.py's
optional stt.local registration, and in tests. The status carries switches and health
only — no engine, model, path or address. groq.oss_120b's "configured" is whether
GROQ_API_KEY is set (the same credential the ACTIVE groq-llama4-scout catalog entry
already uses) — never whether the free-tier quota happens to have room; call
GroqCandidateProvider.quota() for that.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.capability.web_research.capability import WebResearchCapability
from src.capability.web_research.service import web_research_settings
from src.local_runtime.groq_candidate import GroqCandidateProvider
from src.local_runtime.llm import LocalRuntimeUnavailable, local_runtime_settings, loopback_base_url
from src.local_runtime.provider import LocalLLMProvider


@dataclass(frozen=True)
class LocalCapabilityStatus:
    name: str
    enabled: bool       # the operator switched it on
    configured: bool    # it has what it needs to run
    healthy: bool | None  # answered a health probe; None when not probed (off)


def _loopback(url: str) -> bool:
    try:
        loopback_base_url(url)
        return True
    except LocalRuntimeUnavailable:
        return False


async def local_capabilities(*, probe: bool = False) -> list[LocalCapabilityStatus]:
    runtime = local_runtime_settings()
    llm = LocalLLMProvider()
    research = WebResearchCapability()
    groq_120b = GroqCandidateProvider("openai/gpt-oss-120b")
    rows = [
        LocalCapabilityStatus("llm.local", runtime.local_llm_enabled, llm.enabled(),
                              (await llm.health()) if probe and llm.enabled() else None),
        LocalCapabilityStatus("stt.local", runtime.stt_local_enabled,
                              runtime.stt_local_enabled and _loopback(runtime.whisper_local_base_url), None),
        LocalCapabilityStatus("web_research", web_research_settings().web_research_enabled,
                              research.extractor.available(), None),
        LocalCapabilityStatus("groq.oss_120b", runtime.groq_oss_120b_enabled, groq_120b.enabled(), None),
    ]
    return rows
