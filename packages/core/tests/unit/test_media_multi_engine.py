"""Many engines on many runners: the orchestrator picks the best eligible combination and survives failures.

Real adapters (media/adapters.py) and real routes (media/routes.py) run on
scripted runners, so what is pinned is the selection and fallback policy —
which combination runs first, what hands over to what, what cools down, what
stops the chain — with no network and no spend.
"""
from __future__ import annotations

import pytest

from src.config import settings as core_settings
from src.media import engine_catalog
from src.media.adapters import LTX_VIDEO_13B_DISTILLED, WAN22_I2V_A14B, WAN22_TI2V_5B, Z_IMAGE_TURBO
from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    FailureKind,
    MediaOutput,
    VideoEngine,
    VideoRequest,
    ImageRequest,
)
from src.media.health import HealthRegistry, health, runner_scope
from src.media.orchestrator import EngineChainFailed, VideoEngineChain
from src.media.routes import HostedVideoRoute, VideoRoute
from src.media.runners.base import TEXT_TO_VIDEO, Runner, RunnerResult
from src.media.settings import media_settings
from src.services import media_service

MP4 = b"\x00\x00\x00\x18ftypmp42fake"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("MEDIA_ACCEPTED_LICENSE_CONDITIONS", engine_catalog.LTX_CONDITION)
    media_settings.cache_clear()
    health.reset()
    yield
    health.reset()
    media_settings.cache_clear()


class ScriptedRunner(Runner):
    def __init__(self, name: str, *, prices: dict[str, float], fail: list[Exception] | None = None,
                 tasks: set[str] | None = None, available: bool = True, commercial: bool = True,
                 experimental: bool = False) -> None:
        self.name, self.prices, self.fail, self.tasks = name, prices, list(fail or []), tasks
        self.available, self.commercial, self.experimental = available, commercial, experimental
        self.calls: list[tuple] = []

    def supports(self, engine: str, task: str) -> bool:
        return engine in self.prices and (self.tasks is None or task in self.tasks)

    async def is_available(self) -> bool:
        return self.available

    def estimate_usd(self, engine: str, gpu_seconds: float | None) -> float | None:
        return self.prices[engine] * (gpu_seconds or 10.0)

    async def run(self, job):
        self.calls.append((job.engine, job.task, job.width, job.height, job.duration_seconds))
        if self.fail:
            raise self.fail.pop(0)
        return RunnerResult(data=MP4, content_type="video/mp4", total_seconds=12.0, queue_seconds=2.0,
                            gpu_seconds=10.0, cost_usd=self.prices[job.engine] * 10.0)


class PricedHostedVideo(VideoEngine):
    def __init__(self, cost: float) -> None:
        self.cost, self.calls = cost, 0

    async def is_available(self, *, from_image: bool = False) -> bool:
        return True

    def estimate_usd(self, request: VideoRequest) -> float:
        return self.cost

    async def generate(self, request: VideoRequest) -> MediaOutput:
        self.calls += 1
        return MediaOutput(data=MP4, content_type="video/mp4", width=request.width, height=request.height,
                           duration_seconds=float(request.duration_seconds), engine_seconds=3.0, cost_usd=self.cost)


def video(**overrides) -> VideoRequest:
    values = dict(prompt="ondas no mar", width=1280, height=720, duration_seconds=4, fps=16, seed=1,
                  budget_usd=1.0, quality_tier="draft")
    values.update(overrides)
    return VideoRequest(**values)


def outcomes(output_or_error) -> list[tuple[str, str, str]]:
    trace = output_or_error.trace
    rows = [attempt if isinstance(attempt, dict) else attempt.as_dict() for attempt in trace]
    return [(row["engine"], row["outcome"], row["kind"]) for row in rows]


# ── Selection ───────────────────────────────────────────────────────────────

async def test_the_cheapest_route_that_can_do_the_task_runs_first():
    cloud_a = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.002, "wan22_i2v_a14b": 0.0001})
    cloud_b = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.001})
    chain = VideoEngineChain([VideoRoute(WAN22_I2V_A14B, cloud_a), VideoRoute(WAN22_TI2V_5B, cloud_a),
                              VideoRoute(WAN22_TI2V_5B, cloud_b)])

    output = await chain.generate(video())

    # Image-to-video only, so it is never used for text, however cheap; then the cheaper runner of the same engine.
    assert output.metrics["route"] == "wan22_ti2v_5b@cloud_b"
    assert outcomes(output) == [("wan22_ti2v_5b@cloud_b", "completed", "")]
    assert cloud_a.calls == []


async def test_image_to_video_goes_only_to_engines_and_runners_that_animate_images():
    text_only = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.0001}, tasks={TEXT_TO_VIDEO})
    animates = ScriptedRunner("cloud_b", prices={"wan22_i2v_a14b": 0.003})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, text_only), VideoRoute(WAN22_I2V_A14B, animates)])

    output = await chain.generate(video(source=b"png", source_content_type="image/png"))

    assert output.metrics["route"] == "wan22_i2v_a14b@cloud_b"
    assert animates.calls[0][1] == "image_to_video" and text_only.calls == []


async def test_the_quality_floor_filters_and_the_cheapest_route_above_it_still_wins():
    gpu = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001})
    best = PricedHostedVideo(cost=0.50)
    routes = [HostedVideoRoute(best, engine_id="grok_imagine_video", runner_name="xai", quality=4),
              VideoRoute(WAN22_TI2V_5B, gpu)]

    standard = await VideoEngineChain(routes).generate(video(quality_tier="standard"))
    premium = await VideoEngineChain(routes).generate(video(quality_tier="premium"))

    # A plan with a higher floor does not buy the dearest engine: grade 3 at US$0.01 beats grade 4 at US$0.50.
    assert standard.metrics["route"] == "wan22_ti2v_5b@cloud_a"
    assert premium.metrics["route"] == "grok_imagine_video@xai"
    assert ("wan22_ti2v_5b@cloud_a", "skipped_quality", "") in outcomes(premium)


async def test_a_route_that_would_cost_more_than_was_reserved_is_never_tried():
    cheap = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001},
                           fail=[EngineFailed("HTTP 503", kind=FailureKind.TEMPORARY)])
    dear = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.01})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, cheap), VideoRoute(WAN22_TI2V_5B, dear)])

    with pytest.raises(EngineChainFailed) as caught:
        await chain.generate(video(budget_usd=0.015))

    assert dear.calls == []
    assert outcomes(caught.value)[-1] == ("wan22_ti2v_5b@cloud_b", "skipped_budget", "")


# ── Fallback across runners and engines ─────────────────────────────────────

async def test_engine_a_runner_a_then_engine_a_runner_b_then_engine_b_runner_a():
    cloud_a = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001, "ltx_video_13b_distilled": 0.003},
                             fail=[EngineFailed("no gpu", kind=FailureKind.GPU_UNAVAILABLE)])
    cloud_b = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002},
                             fail=[EngineFailed("timed out", kind=FailureKind.TIMEOUT)])
    chain = VideoEngineChain([VideoRoute(LTX_VIDEO_13B_DISTILLED, cloud_a), VideoRoute(WAN22_TI2V_5B, cloud_b),
                              VideoRoute(WAN22_TI2V_5B, cloud_a)])

    output = await chain.generate(video())

    assert outcomes(output) == [("wan22_ti2v_5b@cloud_a", "failed", "gpu_unavailable"),
                                ("wan22_ti2v_5b@cloud_b", "failed", "timeout"),
                                ("ltx_video_13b_distilled@cloud_a", "completed", "")]
    assert output.metrics["route"] == "ltx_video_13b_distilled@cloud_a"
    assert output.metrics["gpu_seconds"] == 10.0 and output.cost_usd == pytest.approx(0.03)


async def test_no_quota_cools_the_whole_runner_and_the_next_request_goes_straight_to_another_cloud():
    cloud_a = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001, "ltx_video_13b_distilled": 0.001},
                             fail=[EngineFailed("HTTP 429 quota", kind=FailureKind.QUOTA, retry_after=1200)])
    cloud_b = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, cloud_a), VideoRoute(LTX_VIDEO_13B_DISTILLED, cloud_a),
                              VideoRoute(WAN22_TI2V_5B, cloud_b)])

    first = await chain.generate(video())
    assert outcomes(first) == [("wan22_ti2v_5b@cloud_a", "failed", "quota"),
                               ("ltx_video_13b_distilled@cloud_a", "skipped_cooldown", ""),
                               ("wan22_ti2v_5b@cloud_b", "completed", "")]
    assert health.cooling_seconds(runner_scope("cloud_a")) >= 1199

    # Cooling routes rank last, so the next request goes straight to the healthy cloud and never touches cloud_a.
    second = await chain.generate(video())
    assert outcomes(second) == [("wan22_ti2v_5b@cloud_b", "completed", "")]
    assert len(cloud_a.calls) == 1


async def test_when_only_cooling_routes_remain_they_are_skipped_not_retried():
    cloud_a = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001},
                             fail=[EngineFailed("quota", kind=FailureKind.QUOTA)])
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, cloud_a)])
    with pytest.raises(EngineChainFailed):
        await chain.generate(video())
    with pytest.raises(EngineChainFailed) as caught:
        await chain.generate(video())
    assert outcomes(caught.value) == [("wan22_ti2v_5b@cloud_a", "skipped_cooldown", "")]
    assert caught.value.unavailable is True and len(cloud_a.calls) == 1
    assert await chain.is_available() is False


async def test_a_provider_that_rejects_the_key_hands_over_instead_of_stopping_the_chain():
    import httpx

    from src.media.hosted import _raise_for

    for status in (401, 403, 404, 402):
        with pytest.raises(EngineFailed) as caught:
            _raise_for(httpx.Response(status, text="nope"), "provider")
        assert caught.value.retryable is True, status
    with pytest.raises(EngineFailed) as refused:
        _raise_for(httpx.Response(422, text="invalid prompt"), "provider")
    assert refused.value.retryable is False


@pytest.mark.parametrize("kind", [FailureKind.RATE_LIMITED, FailureKind.UNAVAILABLE, FailureKind.GPU_UNAVAILABLE,
                                  FailureKind.TIMEOUT, FailureKind.COLD_START_TIMEOUT, FailureKind.OOM,
                                  FailureKind.UNSUPPORTED_FORMAT, FailureKind.UNSUPPORTED_DURATION,
                                  FailureKind.TEMPORARY])
async def test_every_recoverable_failure_hands_over_to_the_next_combination(kind):
    failing = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001}, fail=[EngineFailed("x", kind=kind)])
    backup = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002})
    output = await VideoEngineChain([VideoRoute(WAN22_TI2V_5B, failing), VideoRoute(WAN22_TI2V_5B, backup)]).generate(video())
    assert output.metrics["route"] == "wan22_ti2v_5b@cloud_b"


@pytest.mark.parametrize("kind", [FailureKind.OOM, FailureKind.UNSUPPORTED_FORMAT, FailureKind.UNSUPPORTED_DURATION])
async def test_request_specific_failures_never_cool_a_route(kind):
    failing = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001}, fail=[EngineFailed("x", kind=kind)] * 3)
    backup = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, failing), VideoRoute(WAN22_TI2V_5B, backup)])
    for _ in range(3):
        await chain.generate(video())
    assert health.state("wan22_ti2v_5b@cloud_a") != "cooling"
    assert len(failing.calls) == 3


@pytest.mark.parametrize("kind", [FailureKind.REJECTED, FailureKind.FILTERED])
async def test_a_refused_input_or_filtered_output_stops_the_chain(kind):
    failing = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001}, fail=[EngineFailed("x", kind=kind)])
    backup = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002})
    with pytest.raises(EngineChainFailed) as caught:
        await VideoEngineChain([VideoRoute(WAN22_TI2V_5B, failing), VideoRoute(WAN22_TI2V_5B, backup)]).generate(video())
    assert backup.calls == [] and caught.value.retryable is False


async def test_when_no_combination_can_run_the_failure_says_unavailable():
    down = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001}, available=False)
    gone = ScriptedRunner("cloud_b", prices={"wan22_ti2v_5b": 0.002},
                          fail=[EngineUnavailable("space sleeping", kind=FailureKind.UNAVAILABLE)])
    with pytest.raises(EngineChainFailed) as caught:
        await VideoEngineChain([VideoRoute(WAN22_TI2V_5B, down), VideoRoute(WAN22_TI2V_5B, gone)]).generate(video())
    assert caught.value.unavailable is True
    assert [row[1] for row in outcomes(caught.value)] == ["unavailable", "unavailable"]


# ── Health ──────────────────────────────────────────────────────────────────

def test_cooldowns_follow_the_failure_kind_and_end_on_time():
    now = [100.0]
    registry = HealthRegistry(clock=lambda: now[0])

    assert registry.record_failure("wan@a", FailureKind.GPU_UNAVAILABLE) is True
    assert registry.state("wan@a") == "cooling"
    now[0] += 91
    assert registry.state("wan@a") == "healthy"

    assert registry.record_failure("wan@b", FailureKind.TIMEOUT) is False
    assert registry.state("wan@b") == "degraded"
    assert registry.record_failure("wan@b", FailureKind.TIMEOUT) is True
    registry.record_success("wan@b")
    assert registry.state("wan@b") == "healthy"

    registry.record_failure("runner:a", FailureKind.QUOTA, retry_after=5000)
    assert registry.cooling_seconds("runner:a") == pytest.approx(3600.0)
    assert registry.record_failure("wan@c", FailureKind.OOM) is False


# ── Licence, policy, adapters, registry, economics ──────────────────────────

async def test_an_engine_whose_licence_condition_is_not_confirmed_is_never_run(monkeypatch):
    monkeypatch.setenv("MEDIA_ACCEPTED_LICENSE_CONDITIONS", "")
    media_settings.cache_clear()
    runner = ScriptedRunner("cloud_a", prices={"ltx_video_13b_distilled": 0.001})
    route = VideoRoute(LTX_VIDEO_13B_DISTILLED, runner)
    assert route.eligible() is False and await route.is_available() is False
    with pytest.raises(EngineUnavailable):
        await route.generate(video())
    assert runner.calls == []
    assert engine_catalog.license_permits("wan22_ti2v_5b") is True
    assert engine_catalog.license_permits("not_in_the_catalog") is False
    assert engine_catalog.record_for("veo_3_1_lite").license_decision == engine_catalog.ALLOWED


async def test_experimental_infrastructure_is_refused_in_production(monkeypatch):
    shared = ScriptedRunner("zerogpu", prices={"wan22_ti2v_5b": 0.0}, commercial=False, experimental=True)
    paid = ScriptedRunner("runpod", prices={"wan22_ti2v_5b": 0.001})
    monkeypatch.setattr(core_settings, "environment", "production")
    assert VideoRoute(WAN22_TI2V_5B, shared).eligible() is False
    assert VideoRoute(WAN22_TI2V_5B, paid).eligible() is True
    output = await VideoEngineChain([VideoRoute(WAN22_TI2V_5B, shared), VideoRoute(WAN22_TI2V_5B, paid)]).generate(video())
    assert output.metrics["route"] == "wan22_ti2v_5b@runpod" and shared.calls == []


def test_adapters_turn_any_format_into_what_each_model_makes():
    vertical = WAN22_TI2V_5B.normalize(video(width=1080, height=1920, duration_seconds=9))
    # Up to 720p on the 5B (the quality mode); the economy mode asks it for 480p (tests/unit/test_media_modes.py).
    assert (vertical.width, vertical.height, vertical.duration_seconds, vertical.fps) == (704, 1248, 6, 24)  # up to 6 s
    wide = LTX_VIDEO_13B_DISTILLED.normalize(video(width=1920, height=1080, duration_seconds=6))
    assert wide.width % 32 == 0 and wide.height == 512 and wide.duration_seconds == 6 and wide.fps == 30
    job = WAN22_TI2V_5B.job(vertical)
    assert (job.task, job.frames, job.steps) == ("text_to_video", 6 * 24 + 1, 4)
    story = Z_IMAGE_TURBO.normalize(ImageRequest(prompt="x", width=1080, height=1920, seed=1, steps=8))
    assert (story.width, story.height) == (720, 1280)


def test_routes_come_from_configuration_and_unknown_ones_are_ignored(monkeypatch):
    from src.media import registry

    monkeypatch.setenv("MEDIA_VIDEO_ROUTES", "wan22_ti2v_5b@modal, grok_imagine_video@xai, hunyuan@nowhere")
    monkeypatch.setenv("MEDIA_IMAGE_ROUTES", "z_image_turbo@zerogpu,gpt_image_1_mini@openai")
    media_settings.cache_clear()
    assert [route.label for route in registry.video_engine().engines] == ["wan22_ti2v_5b@modal", "grok_imagine_video@xai"]
    assert [route.label for route in registry.image_engine().engines] == ["z_image_turbo@zerogpu", "gpt_image_1_mini@openai"]

    monkeypatch.setenv("MEDIA_VIDEO_ROUTES", "")
    monkeypatch.setenv("MEDIA_VIDEO_ENGINE", "xai")
    media_settings.cache_clear()
    assert registry.video_engine().label == "grok_imagine_video@xai"


def test_a_plans_promise_rests_only_on_commercial_infrastructure():
    shared = ScriptedRunner("zerogpu", prices={"wan22_ti2v_5b": 0.0}, commercial=False, experimental=True)
    paid = ScriptedRunner("runpod", prices={"wan22_ti2v_5b": 0.001})
    mixed = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, shared), VideoRoute(WAN22_TI2V_5B, paid)])
    assert [route.label for route in media_service.guaranteed_engine(mixed).engines] == ["wan22_ti2v_5b@runpod"]
    assert media_service.guaranteed_engine(VideoEngineChain([VideoRoute(WAN22_TI2V_5B, shared)])) is None


async def test_the_catalog_reports_each_layer_separately():
    runner = ScriptedRunner("cloud_a", prices={"wan22_ti2v_5b": 0.001})
    health.record_failure("wan22_ti2v_5b@cloud_a", FailureKind.GPU_UNAVAILABLE)
    rows = {row["engine_id"] or row["engine"]: row
            for row in await engine_catalog.status_report([VideoRoute(WAN22_TI2V_5B, runner)])}
    wan = rows["wan22_ti2v_5b"]
    assert wan["researched"] and wan["approved"] and wan["integrated"]
    assert wan["available"] == ["wan22_ti2v_5b@cloud_a"] and wan["healthy"] == []
    assert rows["HunyuanVideo-1.5"]["rejected"] is True and rows["HunyuanVideo-1.5"]["approved"] is False
    assert rows["wan22_i2v_a14b"]["disabled"] is True
