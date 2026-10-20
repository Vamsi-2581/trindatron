"""Video modes and the router's rules (media/modes.py), without a database or a network."""
from __future__ import annotations

import pytest

from src.media import modes
from src.media.adapters import WAN22_TI2V_5B
from src.media.engines import VideoRequest
from src.media.orchestrator import VideoEngineChain


class Route:
    """Just enough of a route for class filtering."""

    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id


def test_each_engine_belongs_to_one_class():
    assert modes.engine_class(Route("wan22_ti2v_5b")) == modes.EFFICIENT
    assert modes.engine_class(Route("ltx_video_13b_distilled")) == modes.EFFICIENT
    assert modes.engine_class(Route("wan22_a14b")) == modes.MAXIMUM_CLASS
    assert modes.engine_class(Route("wan22_i2v_a14b")) == modes.MAXIMUM_CLASS
    assert modes.engine_class(Route("grok_imagine_video")) == modes.PREMIUM_CLASS
    assert modes.engine_class(Route("veo_3_1_lite")) == modes.PREMIUM_CLASS
    assert modes.engine_class(Route("higgsfield_kling_3_std")) == modes.PREMIUM_CLASS
    assert modes.engine_class(object()) == modes.EFFICIENT  # unnamed engines never count as premium


def test_a_mode_only_ever_runs_on_its_own_class():
    chain = VideoEngineChain([Route("wan22_ti2v_5b"), Route("wan22_a14b"), Route("grok_imagine_video")])
    ids = lambda engine: [route.engine_id for route in engine.engines]  # noqa: E731
    assert ids(modes.restrict(chain, modes.ECONOMY)) == ["wan22_ti2v_5b"]
    assert ids(modes.restrict(chain, modes.QUALITY)) == ["wan22_ti2v_5b"]
    assert ids(modes.restrict(chain, modes.MAXIMUM)) == ["wan22_a14b"]
    assert ids(modes.restrict(chain, modes.PREMIUM)) == ["grok_imagine_video"]
    # A job without a mode (created before modes existed) can never reach a premium API.
    assert ids(modes.restrict(chain, None)) == ["wan22_ti2v_5b", "wan22_a14b"]


def test_premium_alone_serves_nothing_else():
    only_xai = VideoEngineChain([Route("grok_imagine_video")])
    for mode in (modes.ECONOMY, modes.QUALITY, modes.MAXIMUM):
        assert modes.restrict(only_xai, mode) is None
    assert modes.restrict(only_xai, None) is None
    assert modes.restrict(Route("grok_imagine_video"), modes.QUALITY) is None
    assert modes.restrict(None, modes.QUALITY) is None


def test_the_ladder_only_goes_down_and_never_into_premium():
    everything = ("economy", "quality", "maximum", "premium")
    assert [m.id for m in modes.ladder(modes.PREMIUM, everything)] == ["premium", "maximum", "quality", "economy"]
    assert [m.id for m in modes.ladder(modes.MAXIMUM, everything)] == ["maximum", "quality", "economy"]
    assert [m.id for m in modes.ladder(modes.QUALITY, everything)] == ["quality", "economy"]
    assert [m.id for m in modes.ladder(modes.ECONOMY, everything)] == ["economy"]
    # Only what the plan offers.
    assert [m.id for m in modes.ladder(modes.QUALITY, ("economy", "quality"))] == ["quality", "economy"]
    assert [m.id for m in modes.ladder(modes.MAXIMUM, ("economy",))] == ["economy"]


def test_durations_shorten_before_anything_else_changes():
    assert modes.durations(5) == [5, 4, 3]
    assert modes.durations(3) == [3]
    assert modes.durations(2) == [2]  # a short ask is never lengthened


@pytest.mark.parametrize("requested, chosen, asked, seconds, above, expected", [
    (modes.QUALITY, modes.QUALITY, 5, 5, False, ()),
    (modes.QUALITY, modes.QUALITY, 5, 3, False, ("duration",)),
    (modes.QUALITY, modes.ECONOMY, 5, 5, False, ("resolution",)),
    (modes.MAXIMUM, modes.QUALITY, 5, 5, False, ("engine",)),
    (modes.MAXIMUM, modes.ECONOMY, 5, 4, False, ("duration", "engine")),
    (modes.QUALITY, modes.QUALITY, 5, 5, True, ("plan",)),
    (modes.QUALITY, modes.ECONOMY, 5, 5, True, ("plan", "resolution")),
])
def test_what_the_router_changed_is_named(requested, chosen, asked, seconds, above, expected):
    assert modes.adjustments(requested, chosen, asked, seconds, above) == expected


@pytest.mark.parametrize("text, mode", [
    ("Crie um vídeo em máxima qualidade de um farol", "maximum"),
    ("vídeo com qualidade máxima", "maximum"),
    ("a video in maximum quality", "maximum"),
    ("un video en calidad máxima", "maximum"),
    ("vídeo HD de um gato", "quality"),
    ("video in 720p", "quality"),
    ("vídeo em alta qualidade", "quality"),
    # Every plan defaults to 480p, so each everyday way of asking for more must lift it.
    ("um vídeo com a melhor qualidade possível", "quality"),
    ("vídeo em Full HD de uma praia", "quality"),
    ("quero em alta definição", "quality"),
    ("vídeo em 1080p", "quality"),
    ("a video in high resolution", "quality"),
    ("best quality please", "quality"),
    ("un video en alta calidad", "quality"),
    ("uma versão rápida do vídeo", "economy"),
    ("vídeo econômico", "economy"),
    ("a quick version, 480p", "economy"),
    ("vídeo premium de um carro", "premium"),
    ("faça no grok", "premium"),
    # Words that describe the scene never pick a mode.
    ("vídeo de um carro rápido", None),
    ("a cinematic video of waves", None),
    ("um vídeo fotorrealista de ondas", None),
])
def test_a_mode_is_read_only_from_an_explicit_ask(text, mode):
    assert modes.mode_for_text(text) == mode


def test_premium_needs_both_the_name_and_a_confirmation_in_the_same_message():
    assert modes.confirms_premium("vídeo premium, confirmo") is True
    assert modes.confirms_premium("premium video, I confirm") is True
    assert modes.confirms_premium("video premium, confirmo") is True
    assert modes.confirms_premium("vídeo premium de um carro") is False
    assert modes.confirms_premium("confirmo, vídeo em alta qualidade") is False


def test_the_5b_makes_480p_for_economy_and_720p_for_quality():
    economy = WAN22_TI2V_5B.normalize(VideoRequest(prompt="", width=832, height=480, duration_seconds=5, fps=16, seed=0))
    quality = WAN22_TI2V_5B.normalize(VideoRequest(prompt="", width=1280, height=720, duration_seconds=5, fps=16, seed=0))
    assert (economy.width, economy.height) == (832, 480)
    assert (quality.width, quality.height) == (1248, 704)
    assert economy.fps == quality.fps == 24 and economy.duration_seconds == quality.duration_seconds == 5


# ── A video that has to be heard (owner's rule, 2026-09-27) ──────────────────

@pytest.mark.parametrize("text, heard", [
    ("Crie um vídeo de um homem falando bom dia", True),
    ("vídeo com som de ondas quebrando", True),
    ("com trilha sonora épica", True),
    ("a crowd cheering, with audio: a man shouting 'VIVA A VIDA!'", True),
    ("a woman singing on a stage", True),
    ("un video con música de fondo", True),
    ("Crie um vídeo de um barco no pôr do sol", False),
    ("a cinematic shot of waves on rocks", False),
    ("vídeo de um cachorro correndo", False),
])
def test_a_video_that_has_to_be_heard_is_recognised(text, heard):
    assert modes.wants_audio(text) is heard
