"""CP38: HiggsfieldGenjutsuEngine (character_animate, media/higgsfield.py) in isolation — the
OperationEngine that CP35 added and CP37's real run first exercised for real. Reuses
test_higgsfield_media.py's FakeHiggsfield/factory (the same convention test_higgsfield_jobs.py
already cross-imports), so nothing here reinvents the mock Higgsfield API.

Every call goes through httpx.MockTransport: nothing leaves the machine, nothing is spent.
"""
from __future__ import annotations

import pytest

from src.media.engines import EngineFailed, FailureKind, ProviderLink
from src.media.higgsfield import HiggsfieldGenjutsuEngine
from src.media.operations import OperationInput, OperationRequest, ResolvedInput
from src.media.settings import media_settings
from tests.unit.test_higgsfield_media import CDN, MP4, FakeHiggsfield, _jpeg, factory
from tests.unit.test_media_runners import tiny_mp4

CHARACTER = ResolvedInput(data=_jpeg(), content_type="image/jpeg")
DRIVING = ResolvedInput(data=MP4, content_type="video/mp4")


def engine(server) -> HiggsfieldGenjutsuEngine:
    return HiggsfieldGenjutsuEngine(client_factory=factory(server))


def request(**parameters) -> OperationRequest:
    values = {"prompt": "test", "resolution": "480p", "duration_seconds": 1}
    values.update(parameters)
    return OperationRequest(
        operation="character_animate",
        inputs=(OperationInput(role="character", artifact_id="a1"),
                OperationInput(role="driving_video", artifact_id="a2")),
        parameters=values,
    )


def resolved() -> dict:
    return {"character": CHARACTER, "driving_video": DRIVING}


async def test_estimate_usd_matches_configured_per_second_pricing():
    eng = HiggsfieldGenjutsuEngine()
    assert eng.estimate_usd(request(resolution="480p", duration_seconds=3)) == \
        3 * media_settings().media_higgsfield_genjutsu_usd_per_second_480p
    assert eng.estimate_usd(request(resolution="720p", duration_seconds=2)) == \
        2 * media_settings().media_higgsfield_genjutsu_usd_per_second_720p


async def test_is_available_requires_credentials_and_a_configured_model(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "")
    media_settings.cache_clear()
    try:
        assert await HiggsfieldGenjutsuEngine().is_available() is False
    finally:
        media_settings.cache_clear()


async def test_success_uploads_both_inputs_submits_and_downloads_the_video(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    server = FakeHiggsfield(statuses=("queued", "completed"), kind="video",
                            completed_payload={"video": {"url": f"{CDN}/out/genjutsu.mp4"}})
    output = await engine(server).run(request(), resolved())
    assert output.content_type == "video/mp4"
    assert output.cost_usd == media_settings().media_higgsfield_genjutsu_usd_per_second_480p  # 1s @ 480p
    # Both inputs actually went to the provider — an image and a video, distinct uploads.
    assert server.count("POST", "https://api.higgsfield.ai/files/generate-upload-url") == 2
    body = server.bodies[-1]  # the submit body (last POST that isn't an upload-url request)
    assert "image_urls" in body and "video_url" in body and body["resolution"] == "480p"
    media_settings.cache_clear()


async def test_request_id_is_persisted_via_the_provider_link_before_polling(monkeypatch): # CP38-C
    """The exact gap CP37 hit: OperationRequest had no `link` field, so an OperationEngine had
    nothing to report a provider handle to. Now it does, and it's set BEFORE wait() polls."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    submitted_handles = []
    link = ProviderLink(on_submit=submitted_handles.append)
    server = FakeHiggsfield(statuses=("queued", "completed"), kind="video",
                            completed_payload={"video": {"url": f"{CDN}/out/genjutsu.mp4"}})
    req = OperationRequest(operation="character_animate",
                           inputs=(OperationInput(role="character", artifact_id="a1"),
                                   OperationInput(role="driving_video", artifact_id="a2")),
                           parameters={"prompt": "test", "resolution": "480p", "duration_seconds": 1},
                           link=link)
    await engine(server).run(req, resolved())
    assert link.handle is not None
    assert link.handle.request_id == "req-1"
    assert link.handle.provider == "higgsfield"
    assert len(submitted_handles) == 1 and submitted_handles[0].request_id == "req-1"
    media_settings.cache_clear()


async def test_a_provider_failure_still_raises_with_request_id_and_detail(monkeypatch): # CP38-A/C
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    link = ProviderLink()
    server = FakeHiggsfield(statuses=("queued", "failed"), kind="video",
                            failed_detail={"error": "preprocessing failed: no person detected"})
    req = OperationRequest(operation="character_animate",
                           inputs=(OperationInput(role="character", artifact_id="a1"),
                                   OperationInput(role="driving_video", artifact_id="a2")),
                           parameters={"prompt": "test", "resolution": "480p", "duration_seconds": 1},
                           link=link)
    try:
        raised = None
        try:
            await engine(server).run(req, resolved())
        except EngineFailed as exc:
            raised = exc
        assert raised is not None
        assert raised.kind == FailureKind.TEMPORARY
        assert "req-1" in str(raised)
        assert raised.provider_detail["error"] == "preprocessing failed: no person detected"
        # The link was still told about the handle before the failure — the request_id is not lost
        # even though the job itself failed.
        assert link.handle is not None and link.handle.request_id == "req-1"
    finally:
        media_settings.cache_clear()


# ── Billing duration correction (CP43: CP39/CP42 proved billing tracks the driving video's own ──
# real length, never TRINDATRON's requested duration_seconds — no `duration` field is ever sent) ─

def test_billable_duration_from_real_bytes_rounds_up_the_actual_video_length(): # CP43-A/B
    eng = HiggsfieldGenjutsuEngine()
    real_video = tiny_mp4(seconds=4.963)  # the CP40/CP42 case, exactly
    assert eng._billable_duration({}, real_video) == 5
    # An exact whole number is not pushed to the next one — math.ceil, not a blind +1.
    assert eng._billable_duration({}, tiny_mp4(seconds=4.0)) == 4
    # The smallest possible fraction over a whole second still rounds up — DOCUMENTED behavior.
    assert eng._billable_duration({}, tiny_mp4(seconds=4.001)) == 5


def test_billable_duration_prefers_real_bytes_over_the_stored_artifact_duration():
    eng = HiggsfieldGenjutsuEngine()
    parameters = {"_input_durations": {"driving_video": 2.0}}  # what job creation estimated
    real_video = tiny_mp4(seconds=4.963)  # what the actual upload really is
    assert eng._billable_duration(parameters, real_video) == 5  # real bytes win, not the stale 2.0


def test_billable_duration_falls_back_to_the_stored_artifact_duration_without_bytes(): # CP43-A
    eng = HiggsfieldGenjutsuEngine()
    assert eng._billable_duration({"_input_durations": {"driving_video": 4.963}}) == 5


def test_billable_duration_falls_back_to_the_caller_value_only_as_a_last_resort():
    eng = HiggsfieldGenjutsuEngine()
    # Neither real bytes nor a stored artifact duration known: the old, pre-CP43 fallback — kept
    # only as a floor, never presented as the true billing figure (see estimate_usd()'s docstring).
    assert eng._billable_duration({"duration_seconds": 3}) == 3
    assert eng._billable_duration({}) == 3  # the hardcoded default, unchanged


def test_480p_and_720p_pricing_use_the_billable_duration(): # CP43-C/D
    eng = HiggsfieldGenjutsuEngine()
    assert eng._cost_for("480p", 5) == pytest.approx(5 * media_settings().media_higgsfield_genjutsu_usd_per_second_480p)
    assert eng._cost_for("720p", 5) == pytest.approx(5 * media_settings().media_higgsfield_genjutsu_usd_per_second_720p)
    assert eng._cost_for("480p", 5) == pytest.approx(1.59)
    assert eng._cost_for("720p", 5) == pytest.approx(3.405)


async def test_a_real_duration_that_exceeds_the_reserved_budget_is_refused_before_any_network_call(monkeypatch):
    """CP43 item 6: never submit — or even upload — for more than what was actually reserved.
    Simulates job creation under-estimating (e.g. a legacy Artifact with no stored duration, so it
    fell back to a small duration_seconds) while the real driving video is materially longer."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    media_settings.cache_clear()
    server = FakeHiggsfield(statuses=("queued", "completed"), kind="video")
    real_video = tiny_mp4(seconds=10.0)  # bills for 10s @ 480p = $3.18
    req = OperationRequest(operation="character_animate",
                           inputs=(OperationInput(role="character", artifact_id="a1"),
                                   OperationInput(role="driving_video", artifact_id="a2")),
                           parameters={"prompt": "test", "resolution": "480p", "duration_seconds": 1},
                           budget_usd=0.318)  # only 1s worth was actually reserved
    resolved_inputs = {"character": CHARACTER, "driving_video": ResolvedInput(data=real_video, content_type="video/mp4")}
    try:
        with pytest.raises(EngineFailed) as failed:
            await engine(server).run(req, resolved_inputs)
        assert "exceeds" in str(failed.value)
        assert failed.value.resubmit_safe is False
        assert server.count("POST", "https://api.higgsfield.ai/files/generate-upload-url") == 0  # never uploaded
        assert server.count("POST", "https://api.higgsfield.ai/higgsfiled/genjutsu/motion-transfer/v1.0") == 0
    finally:
        media_settings.cache_clear()


async def test_a_real_duration_within_budget_still_succeeds(monkeypatch):
    """Regression guard alongside the refusal test above: a real duration that DOES fit the
    reservation must still go through normally."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    server = FakeHiggsfield(statuses=("queued", "completed"), kind="video",
                            completed_payload={"video": {"url": f"{CDN}/out/genjutsu.mp4"}})
    real_video = tiny_mp4(seconds=4.963)  # bills for 5s @ 480p = $1.59
    req = OperationRequest(operation="character_animate",
                           inputs=(OperationInput(role="character", artifact_id="a1"),
                                   OperationInput(role="driving_video", artifact_id="a2")),
                           parameters={"prompt": "test", "resolution": "480p", "duration_seconds": 1},
                           budget_usd=200.0)  # MAX's own request ceiling — plenty
    resolved_inputs = {"character": CHARACTER, "driving_video": ResolvedInput(data=real_video, content_type="video/mp4")}
    try:
        output = await engine(server).run(req, resolved_inputs)
        assert output.cost_usd == pytest.approx(1.59)
    finally:
        media_settings.cache_clear()
