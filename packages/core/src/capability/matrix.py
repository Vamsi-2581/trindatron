"""The capability matrix: what TRINDATRON can do for one person, right now, in one place.

Every client (Mobile, Web, Desktop) and the conversation itself read the same
answer, so a button, a sentence from the model and the Core's own refusal can
never disagree. A capability is described by abstract facts only:

  available   every gate passed: plan, operator switch, engine, storage, health
  status      "available" | "plan" (the plan does not include it) |
              "unavailable" (included, but not reachable here right now) |
              "not_integrated" (the product does not do this yet)
  inputs /    what goes in and what comes out ("text", "image", "audio", ...)
  outputs
  health      "ok" | "down" | "unknown"
  fallback    whether more than one engine stands behind it
  scope       for execution capabilities: where the work happens
              ("sandbox" = a separate virtual computer, never the person's device)

Never in here: engine, vendor, model, price, URL. Costs and engine choice stay
in media/, voice/ and the router, where they are decided.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.entitlements import get_plan_entitlement
from src.media.orchestrator import ImageEngineChain, VideoEngineChain, VisionEngineChain
from src.media.registry import image_engine, operation_engine, video_engine, vision_engine
from src.media.video_edit import ffmpeg_path
from src.models.user import User
from src.services.runtime_settings import is_capability_disabled

ORDER = (
    "text", "vision", "image_generation", "image_editing", "video_generation", "video_editing",
    "audio_input", "audio_output", "voice_conversation", "transcription", "image_to_video",
    "video_to_video", "avatar_generation", "agent", "web_search", "browser_control", "computer_control",
    "file_analysis", "document_generation", "code_execution",
)

_IO = {
    "text": (("text",), ("text",)),
    "vision": (("image",), ("text",)),
    "image_generation": (("text",), ("image",)),
    "image_editing": (("image", "text"), ("image",)),
    "video_generation": (("text",), ("video",)),
    "video_editing": (("video", "text"), ("video",)),
    "audio_input": (("audio",), ("text",)),
    "audio_output": (("text",), ("audio",)),
    "voice_conversation": (("audio",), ("audio", "text")),
    "transcription": (("audio",), ("text",)),
    "image_to_video": (("image", "text"), ("video",)),
    "video_to_video": (("video", "text"), ("video",)),
    "avatar_generation": (("image", "text", "audio"), ("video",)),
    "agent": (("text",), ("text", "file")),
    "web_search": (("text",), ("text",)),
    "browser_control": (("text",), ("text", "image")),
    "computer_control": (("text",), ("text", "image")),
    "file_analysis": (("file",), ("text",)),
    "document_generation": (("text",), ("file",)),
    "code_execution": (("text",), ("text", "file")),
}

# What a person can ask for, in their words, when a capability is available.
HOW_TO_ASK = {
    "text": "conversation, writing and analysis in the chat",
    "vision": "attach an image and ask about it",
    "image_generation": "ask to create an image, in any format or size",
    "image_editing": "attach an image and ask for a change",
    "video_generation": "ask to create a video, with a length and format",
    "image_to_video": "attach an image and ask to animate it",
    "audio_input": "send a recording and have it understood",
    "transcription": "speak with the microphone",
    "audio_output": "ask to hear the answer",
    "voice_conversation": "talk by voice",
    "agent": "use agent mode for multi-step work",
    # A question about the present is escalated to the tool loop, which searches (capability/pipelines.py).
    "web_search": "ask about anything current — news, prices, releases — and the web is searched",
    "browser_control": "agent mode opens web pages, reads and fills them",
    "computer_control": "agent mode operates a separate virtual computer",
    "file_analysis": "attach a document and ask about it",
    "document_generation": "ask for a presentation",
    "code_execution": "agent mode runs code in an isolated sandbox",
}


@dataclass(frozen=True)
class CapabilityState:
    name: str
    status: str
    health: str = "unknown"
    fallback: bool = False
    scope: str | None = None
    limits: dict = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status == "available"

    def view(self) -> dict:
        inputs, outputs = _IO[self.name]
        body: dict = {"available": self.available, "status": self.status, "inputs": list(inputs),
                      "outputs": list(outputs), "health": self.health, "fallback": self.fallback}
        if self.scope:
            body["scope"] = self.scope
        if self.available and self.limits:
            body["limits"] = self.limits
        return body


def _state(name: str, *, included: bool, reachable: bool, health: str | None = None, fallback: bool = False,
           scope: str | None = None, limits: dict | None = None) -> CapabilityState:
    if not included:
        status = "plan"
    elif not reachable:
        status = "unavailable"
    else:
        status = "available"
    return CapabilityState(name, status, health or ("ok" if reachable else "down"), fallback, scope, limits or {})


def _not_integrated(name: str, scope: str | None = None) -> CapabilityState:
    return CapabilityState(name, "not_integrated", "unknown", False, scope)


def _chain_size(engine) -> int:
    if isinstance(engine, (ImageEngineChain, VideoEngineChain, VisionEngineChain)):
        return len(engine.engines)
    return 1 if engine is not None else 0


def stt_engines() -> int:
    return sum(1 for key in (settings.elevenlabs_api_key, settings.groq_api_key) if key)


def _tts_engines() -> int:
    return sum(1 for key in (settings.gemini_api_key, settings.elevenlabs_api_key) if key)


def _browser_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def build(db: DBSession, user: User, legacy: dict, *, desktop_ready: bool, sandbox_ready: bool,
          presentations: bool) -> dict[str, dict]:
    """The full matrix, from the per-feature states capability_service already computed (`legacy`)."""
    entitlement = get_plan_entitlement(user.plan)

    def legacy_available(key: str) -> bool:
        return bool((legacy.get(key) or {}).get("available"))

    def limits(key: str) -> dict:
        state = legacy.get(key) or {}
        out = {}
        if state.get("daily_quota"):
            out["daily_quota"] = state["daily_quota"]
        if state.get("limits"):
            out.update(state["limits"])
        return out

    image, video, vision = image_engine(), video_engine(), vision_engine()
    stt, tts = stt_engines(), _tts_engines()
    agent_included = bool(entitlement.supports_agent_mode)
    work_included = bool(getattr(entitlement, "supports_trinity_work", False))
    # "plan" answers only "does this plan include it"; a missing or unreachable engine is "unavailable".
    from src.media.plans import get_media_entitlement

    media_table = get_media_entitlement(user.plan)
    image_included = media_table.image_daily_limit > 0
    video_included = media_table.video_daily_limit > 0
    voice_off = is_capability_disabled(db, "voice")

    states = [
        _state("text", included=True, reachable=True, health="ok", fallback=True),
        _state("vision", included=True, reachable=legacy_available("image_understanding"),
               fallback=_chain_size(vision) > 1, limits=limits("image_understanding")),
        _state("image_generation", included=image_included, reachable=legacy_available("image_generation"),
               fallback=_chain_size(image) > 1, limits=limits("image_generation")),
        _state("image_editing", included=image_included, reachable=legacy_available("image_editing"),
               fallback=_chain_size(image) > 1, limits=limits("image_editing")),
        _state("video_generation", included=video_included, reachable=legacy_available("video_generation"),
               fallback=_chain_size(video) > 1, limits=limits("video_generation")),
        # Integrated once MEDIA_OPERATION_ROUTES lists video_edit@local; reachable while ffmpeg is installed.
        (_state("video_editing", included=video_included, reachable=ffmpeg_path() is not None)
         if operation_engine("video_edit") is not None else _not_integrated("video_editing")),
        _state("audio_input", included=True, reachable=stt > 0 and not voice_off, fallback=stt > 1),
        _state("audio_output", included=True, reachable=tts > 0 and not voice_off, fallback=tts > 1),
        # Turn-based today: speak, the answer is written and can be heard. Continuous duplex voice is not integrated.
        _state("voice_conversation", included=True, reachable=stt > 0 and tts > 0 and not voice_off,
               fallback=stt > 1 or tts > 1, limits={"mode": "turn_based"}),
        _state("transcription", included=True, reachable=stt > 0 and not voice_off, fallback=stt > 1),
        _state("image_to_video", included=media_table.image_to_video, reachable=legacy_available("image_to_video"),
               fallback=_chain_size(video) > 1, limits=limits("image_to_video")),
        _not_integrated("video_to_video"),
        _not_integrated("avatar_generation"),
        _state("agent", included=agent_included, reachable=agent_included and not is_capability_disabled(db, "agent")),
        # web_search is a tool of the agent loop (integrations/catalog.py): it exists wherever agent mode does.
        _state("web_search", included=agent_included,
               reachable=agent_included and not is_capability_disabled(db, "agent")),
        _state("browser_control", included=agent_included,
               reachable=agent_included and _browser_installed() and not is_capability_disabled(db, "agent"),
               scope="sandbox"),
        _state("computer_control", included=work_included, reachable=work_included and desktop_ready, scope="sandbox"),
        _state("file_analysis", included=True, reachable=legacy_available("chat_attachments")),
        _state("document_generation", included=_presentations_included(user.plan),
               reachable=presentations, limits={"formats": ["presentation"]}),
        _state("code_execution", included=agent_included, reachable=agent_included and sandbox_ready, scope="sandbox"),
    ]
    return {state.name: state.view() for state in states}


def _presentations_included(plan: str) -> bool:
    from src.services import presentation_service

    return bool(presentation_service.plan_allows_presentations(plan))


def snapshot(db: DBSession, user: User) -> dict[str, dict]:
    """The matrix without network probes, for the conversation prompt.

    Uses the last matrix this person's client received when it is recent;
    otherwise configuration and plan alone. Execution capabilities that need a
    live probe are reported unavailable rather than promised.
    """
    from src.services import capability_service, media_service, vision_service

    cached = capability_service.cached_matrix(user.id)
    if cached is not None:
        return cached
    entitlement = media_service.entitlement_for(db, user)
    image, video, vision = image_engine(), video_engine(), vision_engine()
    legacy = {
        "image_generation": {"available": image is not None and entitlement.image_daily_limit > 0
                             and not is_capability_disabled(db, "image")},
        "image_editing": {"available": image is not None and entitlement.image_editing
                          and not is_capability_disabled(db, "image")},
        "video_generation": {"available": video is not None and entitlement.video_daily_limit > 0
                             and not is_capability_disabled(db, "video")},
        "image_to_video": {"available": video is not None and entitlement.image_to_video
                           and not is_capability_disabled(db, "video")},
        "image_understanding": {"available": vision is not None and vision_service.daily_limit(user) > 0
                                and not is_capability_disabled(db, "vision")},
        "chat_attachments": {"available": True},
    }
    return build(db, user, legacy, desktop_ready=False, sandbox_ready=False,
                 presentations=_presentations_included(user.plan) and not is_capability_disabled(db, "presentation"))


def prompt_lines(matrix: dict[str, dict]) -> tuple[tuple[str, bool, str], ...]:
    """(name, available, how to ask) for the conversation's capability block, in a stable order."""
    lines = []
    for name in ORDER:
        state = matrix.get(name)
        if not state:
            continue
        available = bool(state.get("available"))
        hint = HOW_TO_ASK.get(name, "") if available else state.get("status", "")
        if name in ("browser_control", "computer_control", "code_execution") and available:
            hint = hint + " (a separate environment, never the person's own device)"
        lines.append((name, available, hint))
    return tuple(lines)
