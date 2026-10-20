"""CP19: OperationEngineChain reuses orchestrator.rank()/run_chain() unchanged — this pins that
the reuse actually works (ranking, fallback, health cooldown) for the new operation shape, the
same way test_media_orchestrator.py already pins it for ImageEngineChain/VideoEngineChain.

CP22 adds OperationAdapter.job()'s multi-input building (media/adapters.py) — the layer that
turns a resolved role -> (data, content_type) dict into a RunnerJob, still in this file since
it's the same "operation contract" surface, not a new adapter-specific mechanism."""
from __future__ import annotations

import pytest

from src.media.adapters import OperationAdapter
from src.media.engines import EngineFailed, EngineUnavailable, FailureKind, MediaOutput
from src.media.operations import OperationEngineChain, OperationInput, OperationRequest, ResolvedInput


class FakeOperationEngine:
    operation = "background_remove"

    def __init__(self, name: str, *, quality: int = 4, cost: float = 0.0, fail: Exception | None = None,
                engine_id: str | None = None, runner_name: str = "test") -> None:
        self.name, self.quality, self._cost, self.fail, self.calls = name, quality, cost, fail, 0
        self.engine_id, self.runner_name = engine_id, runner_name

    async def is_available(self) -> bool:
        return True

    def estimate_usd(self, request) -> float:
        return self._cost

    async def run(self, request, resolved):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return MediaOutput(data=b"out", content_type="image/png", width=1, height=1, duration_seconds=None,
                           engine_seconds=0.1, cost_usd=self._cost)


def _request() -> OperationRequest:
    return OperationRequest(operation="background_remove",
                            inputs=(OperationInput(role="source_image", artifact_id="a1"),))


async def test_the_cheapest_healthy_engine_runs_first():
    cheap, dear = FakeOperationEngine("cheap", cost=0.0), FakeOperationEngine("dear", cost=0.02)
    chain = OperationEngineChain([dear, cheap])  # declared expensive-first; ranking must still pick cheap
    output = await chain.run(_request(), {})
    assert cheap.calls == 1 and dear.calls == 0
    assert output.data == b"out"


async def test_a_retryable_failure_falls_over_to_the_next_engine_once():
    failing = FakeOperationEngine("failing", fail=EngineFailed("temp", kind=FailureKind.TEMPORARY, retryable=True))
    healthy = FakeOperationEngine("healthy", cost=0.01)
    chain = OperationEngineChain([failing, healthy])
    output = await chain.run(_request(), {})
    assert failing.calls == 1 and healthy.calls == 1
    assert output.data == b"out"


async def test_a_non_retryable_failure_stops_the_chain_without_trying_the_next_engine():
    refused = FakeOperationEngine("refused", fail=EngineFailed("bad input", kind=FailureKind.REJECTED, retryable=False))
    never = FakeOperationEngine("never")
    chain = OperationEngineChain([refused, never])
    with pytest.raises(EngineFailed):
        await chain.run(_request(), {})
    assert refused.calls == 1 and never.calls == 0


async def test_when_every_engine_is_unavailable_the_chain_raises_unavailable():
    class Unreachable(FakeOperationEngine):
        async def is_available(self) -> bool:
            return False

    chain = OperationEngineChain([Unreachable("a"), Unreachable("b")])
    with pytest.raises(EngineFailed) as caught:
        await chain.run(_request(), {})
    assert caught.value.reason.endswith("no engine delivered")


def test_a_quality_floor_excludes_an_engine_below_it():
    weak = FakeOperationEngine("weak", quality=2)
    chain = OperationEngineChain([weak])
    request = OperationRequest(operation="background_remove",
                               inputs=(OperationInput(role="source_image", artifact_id="a1"),),
                               quality_tier="premium")  # floor 4, weak is grade 2
    assert chain.estimate_usd(request) is None


def test_headroom_usd_is_the_most_an_affordable_fallback_would_cost():
    cheap, dear = FakeOperationEngine("cheap", cost=0.0), FakeOperationEngine("dear", cost=0.05)
    chain = OperationEngineChain([cheap, dear])
    assert chain.headroom_usd(_request(), lambda usd: usd <= 0.10) == 0.05
    assert chain.headroom_usd(_request(), lambda usd: usd <= 0.01) == 0.0  # dear excluded, only cheap affordable


# ── OperationAdapter.job(): single-input compatibility and multi-input building (CP22) ────────

_SINGLE = OperationAdapter(id="single_role", catalog_name="Single", operation="background_remove", quality=4)
_MULTI = OperationAdapter(id="wan22_animate", catalog_name="Wan2.2-Animate-14B", operation="character_animate",
                          quality=4, primary_role="character", extra_roles=("driving_video",))


def test_a_single_role_adapter_builds_exactly_the_same_job_as_before_extra_roles_existed():
    resolved = {"source_image": ResolvedInput(b"img-bytes", "image/png")}
    job = _SINGLE.job(None, resolved)
    assert job.source == b"img-bytes" and job.source_content_type == "image/png"
    assert job.extra_files == {}  # untouched: this adapter declares no extra_roles


def test_a_two_input_adapter_puts_the_primary_in_source_and_the_rest_in_extra_files_by_role():
    resolved = {"character": ResolvedInput(b"char-bytes", "image/png"),
               "driving_video": ResolvedInput(b"video-bytes", "video/mp4")}
    job = _MULTI.job(None, resolved)
    assert job.source == b"char-bytes" and job.source_content_type == "image/png"
    assert job.extra_files == {"driving_video": (b"video-bytes", "video/mp4")}


def test_extra_files_preserves_role_names_exactly_not_positional_order():
    """Two extra roles, resolved out of declaration order in the dict — job() must still label
    each by its own role, never assume dict insertion order matches extra_roles order."""
    triple = OperationAdapter(id="x", catalog_name="X", operation="op", quality=4, primary_role="a",
                              extra_roles=("b", "c"))
    resolved = {"c": ResolvedInput(b"c-bytes", "video/mp4"), "a": ResolvedInput(b"a-bytes", "image/png"),
               "b": ResolvedInput(b"b-bytes", "audio/wav")}
    job = triple.job(None, resolved)
    assert job.extra_files == {"b": (b"b-bytes", "audio/wav"), "c": (b"c-bytes", "video/mp4")}


def test_a_missing_extra_role_is_simply_absent_not_a_crash():
    """job() itself never validates required-ness — that happens earlier, at creation time
    (media_service.create_operation_job, OPERATION_INPUT_KINDS). A partially-resolved dict here
    just omits what wasn't found, so job() stays a pure, non-raising translation."""
    job = _MULTI.job(None, {"character": ResolvedInput(b"char-bytes", "image/png")})
    assert job.extra_files == {}
