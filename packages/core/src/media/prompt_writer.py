"""What a person asks for, written as the prompt an image or video model can follow.

"crie um video de um barco no manso mt no po do sol" reached the video model as it was: Portuguese, a typo
and a local name the model cannot know. It made an orange object on a blue sea at noon (production,
2026-09-27). A person writes the way they speak; the model needs the scene. One short call to a text model
turns the first into the second — subject, place, light, composition, camera, style — keeping everything
asked, in English, the language these models follow best.

The rewrite never blocks a creation: any failure (timeout, no provider, an empty or overlong answer) leaves
the person's own words, which is what ran before this existed. Its cost is measured and added to the job's.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("trinity.media.prompt_writer")

TIMEOUT_S = 20.0
MAX_OUTPUT_TOKENS = 260
# A ceiling the router may reserve for one rewrite, not what it costs: a few hundred tokens on the cheapest
# capable model are measured at ~US$ 0.0001. The router's pre-call estimate is pessimistic; at 0.003 it
# discarded every model (tested against production keys, 2026-09-27), and every rewrite was skipped.
MAX_COST_USD = 0.02
MAX_PROMPT_CHARS = 1500

_SYSTEM = """You turn a person's request for {an} {kind} into ONE prompt for a text-to-{kind} model.

- Keep everything the person asked for: subject, place, mood, colors, text, number of people.
- Read typos and local names the way the person meant them ("po do sol" is "pôr do sol", sunset; "Manso MT" \
is Lago do Manso, a reservoir in Mato Grosso, Brazil, in the cerrado near Chapada dos Guimarães).
- Add concrete visual detail the request leaves open: the subject's look, the setting, time of day and \
light, composition{camera}, and a style (photorealistic unless another style is asked).
- {sound}
- Never add people, brands, logos or text the person did not ask for.

Always write the prompt in English, whatever language the request is in (only words someone says aloud stay as written). Answer with the prompt only: one paragraph, at most 90 words, no preface, no quotes."""

_CAMERA = " and framing, one clear camera movement"
_WITH_SOUND = ("Describe the sounds too, and keep any words someone says exactly as the person wrote them, "
               "in their original language.")
_SILENT = "Describe only what is seen: leave out sounds, music and spoken words."


@dataclass(frozen=True)
class Rewrite:
    prompt: str
    cost_usd: float
    model: str


def _clean(text: str | None) -> str:
    text = (text or "").strip().strip("\"'“”").strip()
    if text.lower().startswith("prompt:"):
        text = text[len("prompt:"):].strip()
    return " ".join(text.split())


async def rewrite(request: str, *, kind: str, plan: str, with_sound: bool = False, router=None) -> Rewrite | None:
    """The request as a model prompt, or None to use the person's own words."""
    from trinity_router import RoutingRequest, TaskType

    if router is None:
        from src.config import settings

        if settings.testing:
            return None  # tests inject a router when they exercise the rewrite
        router = _default_router()
    system = _SYSTEM.format(kind=kind, an="a" if kind == "video" else "an",
                            camera=_CAMERA if kind == "video" else "",
                            sound=_WITH_SOUND if with_sound else _SILENT)
    try:
        result = await asyncio.wait_for(router.complete(RoutingRequest(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": f"Request: {request}\n\nThe prompt, in English:"}],
            task_type=TaskType.CHAT, user_plan=plan, session_id="media-prompt-writer", capability="chat",
            max_output_tokens=MAX_OUTPUT_TOKENS, max_cost_usd_override=MAX_COST_USD,
        )), timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — the creation goes on with the person's words
        logger.warning("media prompt: rewrite skipped | reason=%s", type(exc).__name__)
        return None
    prompt = _clean(result.content)
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        logger.warning("media prompt: rewrite unusable | chars=%d", len(prompt))
        return None
    return Rewrite(prompt=prompt, cost_usd=max(0.0, float(result.cost_usd or 0.0)), model=result.model)


_router_instance = None


def _default_router():
    global _router_instance
    if _router_instance is None:
        from trinity_router import ModelRouter

        _router_instance = ModelRouter()
    return _router_instance
