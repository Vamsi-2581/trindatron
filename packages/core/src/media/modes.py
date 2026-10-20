"""Video modes: what a person chooses, priced by what the engine really costs.

    economy   Wan 2.2 TI2V-5B distilled, 480p          ≈ 1.24 TCU for 5 s
    quality   Wan 2.2 TI2V-5B distilled, 720p          ≈ 2.31 TCU for 5 s
    maximum   Wan 2.2 A14B + Lightning 4-step, 720p    ≈ 9.31 TCU for 5 s
    premium   hosted premium API (xAI), 720p           ≈ 35 TCU for 5 s — never automatic

Reference costs are the warm runs measured on Modal on 2026-09-25 (FastWan2.2
TI2V-5B, 3 DMD steps, H100: 8.7 s for 5 s of 832x480, 16.2 s for 5 s of
1280x704; Wan2.2-T2V-A14B + Lightning 250928, 4 steps, H200 without offload:
56.8 s for 81 frames of 1280x720), priced at Modal's per-second GPU rate plus
REFERENCE_MARKUP for cold starts, idle and retries. They size a request only
when its route cannot price itself; every job still settles at its real cost
(media_service._complete).

Each mode is served by one class of engine (engine_class): economy and quality
by the efficient self-hosted engines at two resolutions, maximum by the A14B
family, premium by hosted per-clip APIs. A request for one mode never runs on
another class's engine, so a premium API is never reached as a fallback.

The router (ladder, durations) only ever moves *down* from what was asked, in the
approved order: shorter clip, then lower resolution, then a cheaper engine. The
premium mode is entered only on an explicit, confirmed request.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Modal list prices, US$ per GPU second (2026-09).
GPU_USD_PER_SECOND = {"h100": 0.001097, "h200": 0.001261}
# What one measured warm run is multiplied by to cover cold starts, idle containers and retries.
REFERENCE_MARKUP = 1.30
# A clip is shortened to fit a budget, never below this (or below what was asked, if shorter).
MIN_ADJUSTED_SECONDS = 3

EFFICIENT, MAXIMUM_CLASS, PREMIUM_CLASS = "efficient", "maximum", "premium"
MAXIMUM_ENGINES = frozenset({"wan22_a14b", "wan22_i2v_a14b", "wan22_t2v_a14b", "wan22_i2v_a14b_lightning"})
PREMIUM_ENGINES = frozenset({"grok_imagine_video", "veo_3_1_lite"})


@dataclass(frozen=True)
class VideoMode:
    id: str
    rank: int                  # cost order; the router only moves to a lower rank
    engine_class: str
    short_side: int            # 480 = 480p
    quality_tier: str          # orchestrator.QUALITY_FLOOR key for the chain
    max_seconds: int
    reference_gpu: str | None = None
    reference_gpu_seconds_per_5s: float | None = None
    reference_usd_per_second: float | None = None  # hosted APIs price per clip second
    requires_confirmation: bool = False
    # The clip made when none is asked for, and the one a plan's daily count is sized on.
    default_seconds: int = 5

    def reference_usd(self, duration_seconds: int) -> float:
        """What `duration_seconds` of this mode costs on the measured reference, markup included."""
        seconds = max(1, int(duration_seconds))
        if self.reference_usd_per_second is not None:
            return self.reference_usd_per_second * seconds
        gpu_seconds = self.reference_gpu_seconds_per_5s * seconds / 5
        return gpu_seconds * GPU_USD_PER_SECOND[self.reference_gpu] * REFERENCE_MARKUP


# Up to 6 s on the 5B (measured 2026-09-26: no freeze at 6 s; the 8 s run froze), 5 s by default.
ECONOMY = VideoMode("economy", 0, EFFICIENT, 480, "draft", 6, "h100", 8.7)
QUALITY = VideoMode("quality", 1, EFFICIENT, 720, "standard", 6, "h100", 16.2)
MAXIMUM = VideoMode("maximum", 2, MAXIMUM_CLASS, 720, "premium", 5, "h200", 56.8)
# xAI grok-imagine-video at 720p: US$ 0.07 per second.
PREMIUM = VideoMode("premium", 3, PREMIUM_CLASS, 720, "premium", 5, reference_usd_per_second=0.07,
                    requires_confirmation=True)

MODES: dict[str, VideoMode] = {mode.id: mode for mode in (ECONOMY, QUALITY, MAXIMUM, PREMIUM)}


def get(mode_id: str | None) -> VideoMode | None:
    return MODES.get((mode_id or "").lower())


def engine_class(engine: object) -> str:
    """The class of a route by its engine id. Anything unnamed (a test double, a new route) is efficient."""
    engine_id = getattr(engine, "engine_id", None) or ""
    if engine_id in PREMIUM_ENGINES or engine_id.startswith("higgsfield_"):
        return PREMIUM_CLASS
    if engine_id in MAXIMUM_ENGINES:
        return MAXIMUM_CLASS
    return EFFICIENT


def restrict(engine, mode: VideoMode | None):
    """The part of a video engine (or chain) allowed to serve `mode`; None when nothing is.

    Without a mode (a job created before modes existed) premium routes are still excluded:
    a premium API is never reached unless it was asked for and confirmed.
    """
    if engine is None:
        return None

    def allowed(route) -> bool:
        cls = engine_class(route)
        return cls != PREMIUM_CLASS if mode is None else cls == mode.engine_class

    subset = getattr(engine, "subset", None)
    if subset is not None:
        return subset(allowed)
    return engine if allowed(engine) else None


def ladder(requested: VideoMode, offered: tuple[str, ...]) -> list[VideoMode]:
    """The modes to try for a request, best first: the one asked for, then every cheaper one the plan offers.

    Premium is never on the way down from another mode; it is only ever where a confirmed request starts.
    """
    below = [MODES[m] for m in offered if m in MODES and MODES[m].rank < requested.rank
             and not MODES[m].requires_confirmation]
    first = [requested] if requested.id in offered else []
    return first + sorted(below, key=lambda mode: mode.rank, reverse=True)


def durations(asked: int) -> list[int]:
    """Clip lengths to try, longest first: what was asked, then shorter down to MIN_ADJUSTED_SECONDS."""
    floor = min(asked, MIN_ADJUSTED_SECONDS)
    return list(range(asked, floor - 1, -1))


def adjustments(requested: VideoMode, chosen: VideoMode, asked_seconds: int, seconds: int,
                above_plan: bool) -> tuple[str, ...]:
    """What the router changed, in the order it changes things; empty when the request ran as asked.

    `requested` is the mode the router started from (the plan's best when the ask was above the
    plan, which `above_plan` records as "plan").
    """
    out = []
    if above_plan:
        out.append("plan")
    if seconds < asked_seconds:
        out.append("duration")
    if chosen.short_side < requested.short_side and chosen.engine_class == requested.engine_class:
        out.append("resolution")
    if chosen.engine_class != requested.engine_class:
        out.append("engine")
    return tuple(out)


# ── What a person asks for, in their words ───────────────────────────────────

_PREMIUM = re.compile(r"\b(premium|xai|grok)\b", re.IGNORECASE)
# Only explicit asks for a mode: words that describe the scene ("cinematic", "a fast car") never pick one.
_MAXIMUM = re.compile(
    r"m[áa]xima\s+(qualidade|calidad)|(qualidade|calidad)\s+m[áa]xima|max(imum)?\s+quality"
    r"|\bmodo\s+m[áa]ximo\b|\bmaximum\s+mode\b",
    re.IGNORECASE,
)
_ECONOMY = re.compile(
    r"econ[ôo]mic[oa]|\beconomy\b|\bmodo\s+r[áa]pido\b|\bvers[ãa]o\s+r[áa]pida\b|\bquick\s+version\b"
    r"|\brascunho\b|\bborrador\b|\bdraft\b|\b480p\b",
    re.IGNORECASE,
)
# Every plan's default is economy (media/plans.py), so these are the words that lift a video above 480p.
_QUALITY = re.compile(
    r"\b(?:full\s*)?hd\b|\b720p\b|\b1080p\b"
    r"|alta\s+(qualidade|calidad|resolu[çc][ãa]o|resoluci[óo]n|defini[çc][ãa]o|definici[óo]n)"
    r"|(qualidade|calidad|resolu[çc][ãa]o|resoluci[óo]n)\s+alta"
    r"|(melhor|[óo]tima|boa|mais)\s+qualidade|(mejor|buena|m[áa]s)\s+calidad"
    r"|(high|higher|better|best|good)\s+(quality|resolution|definition)"
    r"|\bmodo\s+(qualidade|calidad)\b|\bquality\s+mode\b",
    re.IGNORECASE,
)
_CONFIRM = re.compile(
    r"\b(confirmo|confirmado|confirmada|confirm(ed)?|autorizo|i\s+authori[sz]e|pode\s+cobrar|puedes\s+cobrar)\b",
    re.IGNORECASE,
)


# Words that say a video has to be heard; such a video is made where sound is recorded (premium).
_WANTS_AUDIO = re.compile(
    r"\bcom\s+(?:som|[áa]udio|m[úu]sica|narra[çc][ãa]o|trilha)\b|\bsonoro\b|\btrilha\s+sonora\b"
    r"|\b(?:falando|dizendo|gritando|cantando|narrando|conversando)\b|\bnarra[çc][ãa]o\b"
    r"|\bwith\s+(?:sound|audio|music|narration|voice(?:over)?)\b|\baudio\s*:|\bvoice[\s-]?over\b"
    r"|\b(?:speaking|saying|shouting|singing|narrating|talking)\b|\bsound\s+effects?\b"
    r"|\bcon\s+(?:sonido|audio|m[úu]sica|narraci[óo]n)\b|\b(?:hablando|diciendo|cantando|narrando)\b",
    re.IGNORECASE,
)
def wants_audio(content: str) -> bool:
    """The video has to be heard: speech, a shout, singing, narration, music or sound effects."""
    return bool(_WANTS_AUDIO.search(content or ""))


def mode_for_text(content: str) -> str | None:
    """The mode a request names ("vídeo em máxima qualidade", "modo rápido", "in HD", "premium"), or None."""
    text = content or ""
    for pattern, mode in ((_PREMIUM, "premium"), (_MAXIMUM, "maximum"), (_ECONOMY, "economy"), (_QUALITY, "quality")):
        if pattern.search(text):
            return mode
    return None


def confirms_premium(content: str) -> bool:
    """The same message names premium and explicitly confirms the cost ("premium, confirmo")."""
    return bool(_PREMIUM.search(content or "")) and bool(_CONFIRM.search(content or ""))
