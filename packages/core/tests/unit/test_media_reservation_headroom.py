"""A creation reserves enough for an affordable fallback, and a daily offer is sized on what a request usually costs."""
from __future__ import annotations

import pytest

from src.media import plans
from src.media.engines import ImageEngine, ImageRequest, MediaOutput, VideoEngine, VideoRequest
from src.media.hosted import OpenAIImageEngine
from src.media.orchestrator import ImageEngineChain, VideoEngineChain
from src.media.settings import media_settings
from src.services import media_service


class PricedImage(ImageEngine):
    def __init__(self, worst: float, typical: float | None = None) -> None:
        self.worst, self.typical = worst, typical

    async def is_available(self, *, editing: bool = False) -> bool:
        return True

    def estimate_usd(self, request: ImageRequest) -> float:
        return self.worst

    def typical_usd(self, request: ImageRequest) -> float:
        return self.typical if self.typical is not None else self.worst

    async def generate(self, request: ImageRequest) -> MediaOutput:  # pragma: no cover - never called here
        raise AssertionError


class PricedVideo(VideoEngine):
    def __init__(self, per_second: float, durations: tuple[int, ...] | None = None) -> None:
        self.per_second, self.durations = per_second, durations

    async def is_available(self, *, from_image: bool = False) -> bool:
        return True

    def normalize(self, request: VideoRequest) -> VideoRequest:
        from dataclasses import replace

        if not self.durations:
            return request
        return replace(request, duration_seconds=next((d for d in self.durations if d >= request.duration_seconds),
                                                      self.durations[-1]))

    def estimate_usd(self, request: VideoRequest) -> float:
        return self.per_second * request.duration_seconds

    async def generate(self, request: VideoRequest) -> MediaOutput:  # pragma: no cover
        raise AssertionError


def _image() -> ImageRequest:
    return ImageRequest(prompt="x", width=1024, height=1024, seed=0, steps=8)


def test_a_single_engine_reserves_its_own_estimate():
    assert media_service._reservation_usd("pro", PricedImage(0.004), _image(), 0.004) == pytest.approx(0.004)


def test_a_chain_reserves_for_the_dearest_fallback_the_plan_ceiling_allows():
    chain = ImageEngineChain([PricedImage(0.004), PricedImage(0.02)])
    # PRO's per-request ceiling (11 TCU = $0.11) covers the $0.02 fallback.
    assert media_service._reservation_usd("pro", chain, _image(), 0.004) == pytest.approx(0.02)


def test_a_fallback_beyond_the_plan_ceiling_is_not_reserved_for():
    chain = ImageEngineChain([PricedImage(0.004), PricedImage(5.0)])
    assert plans.fits_request_ceiling("free", 5.0) is False
    assert media_service._reservation_usd("free", chain, _image(), 0.004) == pytest.approx(0.004)


def test_video_headroom_prices_each_engine_in_its_own_shape():
    chain = VideoEngineChain([PricedVideo(0.05), PricedVideo(0.05, durations=(4, 6, 8))])
    request = VideoRequest(prompt="x", width=854, height=480, duration_seconds=2, fps=24, seed=0)
    # The second engine's shortest clip is 4 s: $0.20, above the primary's $0.10.
    assert media_service._reservation_usd("max", chain, request, 0.10) == pytest.approx(0.20)


def test_a_daily_offer_uses_the_typical_cost_while_the_request_reserves_the_worst_case(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    media_settings.cache_clear()
    try:
        engine = OpenAIImageEngine()
        request = _image()
        assert engine.typical_usd(request) < engine.estimate_usd(request)
        base = plans.get_media_entitlement("start")
        assert media_service.image_unit_usd(base, engine) == pytest.approx(
            max(engine.typical_usd(ImageRequest(prompt="", width=w, height=h, seed=0, steps=8, quality_tier="standard"))
                for w, h in (media_service.image_dimensions_for(spec, base) for spec in media_service._PRICE_CHECK_FORMATS)))
    finally:
        media_settings.cache_clear()
