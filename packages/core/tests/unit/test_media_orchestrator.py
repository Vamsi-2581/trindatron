"""An engine chain delivers from the first engine that can, within what was reserved, and records how."""
from __future__ import annotations

import pytest

from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    ImageEngine,
    ImageRequest,
    MediaOutput,
    VideoEngine,
    VideoRequest,
    VisionEngine,
    VisionResult,
)
from src.media.orchestrator import EngineChainFailed, ImageEngineChain, VideoEngineChain, VisionEngineChain


class FakeImage(ImageEngine):
    def __init__(self, name: str, *, available: bool = True, fail: Exception | None = None, cost: float = 0.01,
                 edits: bool = True) -> None:
        self.name, self.available, self.fail, self.cost, self.edits, self.calls = name, available, fail, cost, edits, 0

    async def is_available(self, *, editing: bool = False) -> bool:
        return self.available and (self.edits or not editing)

    def estimate_usd(self, request: ImageRequest) -> float:
        return self.cost

    async def generate(self, request: ImageRequest) -> MediaOutput:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return MediaOutput(data=self.name.encode(), content_type="image/jpeg", width=request.width,
                           height=request.height, duration_seconds=None, engine_seconds=0.1, cost_usd=self.cost)


class FakeVideo(VideoEngine):
    def __init__(self, *, available: bool = True, fail: Exception | None = None, cost_per_second: float = 0.05,
                 durations: tuple[int, ...] | None = None) -> None:
        self.available, self.fail, self.cost_per_second, self.durations, self.calls = available, fail, cost_per_second, durations, 0

    async def is_available(self, *, from_image: bool = False) -> bool:
        return self.available

    def normalize(self, request: VideoRequest) -> VideoRequest:
        from dataclasses import replace

        if not self.durations:
            return request
        return replace(request, duration_seconds=next((d for d in self.durations if d >= request.duration_seconds),
                                                      self.durations[-1]))

    def estimate_usd(self, request: VideoRequest) -> float:
        return self.cost_per_second * request.duration_seconds

    async def generate(self, request: VideoRequest) -> MediaOutput:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return MediaOutput(data=b"mp4", content_type="video/mp4", width=request.width, height=request.height,
                           duration_seconds=request.duration_seconds, engine_seconds=1.0,
                           cost_usd=self.estimate_usd(self.normalize(request)))


def image(**overrides) -> ImageRequest:
    values = dict(prompt="a lighthouse", width=1024, height=1024, seed=1, steps=8, budget_usd=0.05)
    values.update(overrides)
    return ImageRequest(**values)


async def test_the_primary_engine_serves_when_it_can():
    primary, backup = FakeImage("primary"), FakeImage("backup")
    output = await ImageEngineChain([primary, backup]).generate(image())
    assert output.data == b"primary" and backup.calls == 0
    assert [a["outcome"] for a in output.trace] == ["completed"]


async def test_an_unavailable_engine_hands_over_to_the_next():
    primary, backup = FakeImage("primary", available=False), FakeImage("backup")
    output = await ImageEngineChain([primary, backup]).generate(image())
    assert output.data == b"backup" and primary.calls == 0
    assert [a["outcome"] for a in output.trace] == ["unavailable", "completed"]


async def test_a_transient_failure_falls_back_once_per_engine():
    primary = FakeImage("primary", fail=EngineFailed("HTTP 503", retryable=True))
    backup = FakeImage("backup")
    output = await ImageEngineChain([primary, backup]).generate(image())
    assert output.data == b"backup" and primary.calls == 1 and backup.calls == 1


async def test_a_definitive_refusal_stops_the_chain():
    primary = FakeImage("primary", fail=EngineFailed("HTTP 400"))
    backup = FakeImage("backup")
    with pytest.raises(EngineChainFailed) as caught:
        await ImageEngineChain([primary, backup]).generate(image())
    assert backup.calls == 0 and caught.value.retryable is False and caught.value.unavailable is False


async def test_a_fallback_that_would_cost_more_than_was_reserved_is_never_tried():
    primary = FakeImage("primary", available=False, cost=0.004)
    expensive = FakeImage("expensive", cost=0.02)
    with pytest.raises(EngineChainFailed) as caught:
        await ImageEngineChain([primary, expensive]).generate(image(budget_usd=0.005))
    assert expensive.calls == 0
    assert caught.value.unavailable is True
    assert [a.outcome for a in caught.value.trace] == ["unavailable", "skipped_budget"]


async def test_when_every_engine_fails_transiently_the_failure_is_reported_once():
    engines = [FakeImage(str(i), fail=EngineUnavailable("down")) for i in range(3)]
    with pytest.raises(EngineChainFailed) as caught:
        await ImageEngineChain(engines).generate(image())
    assert all(engine.calls == 1 for engine in engines)
    assert caught.value.unavailable is True and len(caught.value.trace) == 3


async def test_editing_only_goes_to_engines_that_can_edit():
    cannot, can = FakeImage("cannot", edits=False), FakeImage("can")
    chain = ImageEngineChain([cannot, can])
    assert await chain.is_available(editing=True) is True
    output = await chain.generate(image(source=b"png", source_content_type="image/png"))
    assert output.data == b"can" and cannot.calls == 0


async def test_the_primary_sizes_the_reservation_and_the_request_shape():
    chain = VideoEngineChain([FakeVideo(durations=(4, 6, 8), cost_per_second=0.05), FakeVideo(cost_per_second=0.07)])
    request = chain.normalize(VideoRequest(prompt="waves", width=1280, height=720, duration_seconds=5, fps=24, seed=1))
    assert request.duration_seconds == 6
    assert chain.estimate_usd(request) == pytest.approx(0.30)


async def test_video_falls_back_when_the_primary_has_no_quota():
    blocked = FakeVideo(fail=EngineFailed("HTTP 429", retryable=True))
    backup = FakeVideo(cost_per_second=0.05)
    request = VideoRequest(prompt="waves", width=854, height=480, duration_seconds=4, fps=24, seed=1, budget_usd=0.25)
    output = await VideoEngineChain([blocked, backup]).generate(request)
    assert output.content_type == "video/mp4" and blocked.calls == 1 and backup.calls == 1
    assert output.trace[0]["outcome"] == "failed" and output.trace[1]["outcome"] == "completed"


class FakeVision(VisionEngine):
    def __init__(self, *, available: bool = True, cost: float = 0.001) -> None:
        self.available, self.cost, self.calls = available, cost, 0

    async def is_available(self) -> bool:
        return self.available

    def estimate_usd(self) -> float:
        return self.cost

    async def describe(self, data: bytes, content_type: str) -> VisionResult:
        self.calls += 1
        return VisionResult(text="a lighthouse", input_tokens=1, output_tokens=1, cost_usd=self.cost, engine_seconds=0.1)


async def test_vision_reads_with_the_next_engine_and_reserves_for_the_dearest():
    chain = VisionEngineChain([FakeVision(available=False, cost=0.0006), FakeVision(cost=0.0017)])
    assert chain.estimate_usd() == pytest.approx(0.0017)
    result = await chain.describe(b"img", "image/png")
    assert result.text == "a lighthouse"
    assert [a["outcome"] for a in chain.last_trace] == ["unavailable", "completed"]


def test_a_chain_needs_engines():
    with pytest.raises(ValueError):
        ImageEngineChain([])
