"""Runners speak each infrastructure's contract, classify its failures and measure what a run took.

Every call goes through httpx.MockTransport: nothing leaves the machine and
nothing is spent. What is pinned is what the orchestrator depends on — the
request each platform receives, how its answers become RunnerResults, which
failure kind each error becomes, and how GPU seconds become cost.
"""
from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest

from src.media.engines import EngineFailed, EngineUnavailable, FailureKind
from src.media.hosted import _raise_for
from src.media.runners import gpu_worker, zerogpu
from src.media.runners.base import (
    BACKGROUND_REMOVE,
    CHARACTER_ANIMATE,
    IMAGE_TO_VIDEO,
    TEXT_TO_VIDEO,
    RunnerJob,
    assert_public_https_url,
    kind_from_message,
    kind_from_status,
    video_frame_count,
    video_metadata,
)
from src.media.runners.gpu_worker import ModalRunner, RunPodRunner
from src.media.runners.zerogpu import ZeroGPURunner, space_host
from src.media.settings import media_settings


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def tiny_mp4(width: int = 480, height: int = 832, seconds: float = 3.5) -> bytes:
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\x00" * 80)
    tkhd = _box(b"tkhd", b"\x00" * 76 + struct.pack(">II", width << 16, height << 16))
    return _box(b"ftyp", b"mp42\x00\x00\x00\x00") + _box(b"moov", mvhd + _box(b"trak", tkhd))


MP4 = tiny_mp4()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf-test")
    monkeypatch.setenv("MEDIA_MODAL_URL", "https://worker.example")
    monkeypatch.setenv("MEDIA_MODAL_TOKEN", "modal-test")
    monkeypatch.setenv("MEDIA_MODAL_ENGINES", "wan22_ti2v_5b,ltx_video_13b_distilled")
    monkeypatch.setenv("MEDIA_MODAL_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("MEDIA_RUNPOD_ENDPOINT_ID", "ep1")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-test")
    monkeypatch.setenv("MEDIA_RUNPOD_ENGINES", "wan22_ti2v_5b")
    monkeypatch.setenv("MEDIA_RUNPOD_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    zerogpu._space_state.clear()
    zerogpu._space_config.clear()
    gpu_worker._health_cache.clear()
    yield
    media_settings.cache_clear()
    zerogpu._space_state.clear()
    zerogpu._space_config.clear()
    gpu_worker._health_cache.clear()


def factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def no_sleep(_seconds):
    return None


def ticking(*values):
    ticks = iter(values)
    last = [0.0]

    def clock():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    return clock


def job(**overrides) -> RunnerJob:
    values = dict(engine="wan22_ti2v_5b", task=TEXT_TO_VIDEO, prompt="ondas no mar", width=480, height=832, seed=7,
                  duration_seconds=3, fps=24, frames=73, steps=4, guidance=0.0)
    values.update(overrides)
    return RunnerJob(**values)


# ── Shared contract ─────────────────────────────────────────────────────────

def test_the_worker_job_carries_the_source_as_base64_and_no_empty_fields():
    payload = job(task=IMAGE_TO_VIDEO, source=b"png-bytes", source_content_type="image/png").payload()
    assert payload["source"] == {"content_type": "image/png", "data_base64": base64.b64encode(b"png-bytes").decode()}
    assert "negative_prompt" not in payload and payload["frames"] == 73


@pytest.mark.parametrize("status,text,kind", [
    (429, "Too Many Requests", FailureKind.RATE_LIMITED),
    (429, "RESOURCE_EXHAUSTED: check your plan and billing details", FailureKind.QUOTA),
    (402, "", FailureKind.QUOTA),
    (503, "", FailureKind.UNAVAILABLE),
    (500, "CUDA error: out of memory", FailureKind.OOM),
    (400, "unsupported resolution 4096x4096", FailureKind.UNSUPPORTED_FORMAT),
    (400, "prompt refused", FailureKind.REJECTED),
])
def test_http_failures_become_the_kind_the_orchestrator_acts_on(status, text, kind):
    assert kind_from_status(status, text) == kind


def test_messages_are_classified():
    assert kind_from_message("No GPU was available after 60s") == FailureKind.GPU_UNAVAILABLE
    assert kind_from_message("You have exceeded your GPU quota (120s requested vs. 30s left)") == FailureKind.QUOTA
    assert kind_from_message("worker failed to start in time") == FailureKind.COLD_START_TIMEOUT
    assert kind_from_message("duration 12s not supported") == FailureKind.UNSUPPORTED_DURATION


def test_a_hosted_api_out_of_quota_is_a_quota_failure_not_a_refusal():
    with pytest.raises(EngineFailed) as caught:
        _raise_for(httpx.Response(429, text='{"error": {"status": "RESOURCE_EXHAUSTED"}}'), "veo submit")
    assert caught.value.kind == FailureKind.QUOTA and caught.value.retryable is True
    with pytest.raises(EngineFailed) as refused:
        _raise_for(httpx.Response(400, text="bad"), "veo submit")
    assert refused.value.retryable is False


@pytest.mark.parametrize("url", [
    "https://cdn.fal.media/files/output.jpg",
    "https://storage.googleapis.com/bucket/output.mp4",
    "https://higgsfield-outputs.s3.amazonaws.com/x.png",
])
def test_public_cdn_urls_are_left_alone(url):
    assert assert_public_https_url(url, "test") == url


@pytest.mark.parametrize("url,why", [
    ("http://cdn.fal.media/files/output.jpg", "not https"),
    ("https://127.0.0.1/output.jpg", "loopback"),
    ("https://169.254.169.254/latest/meta-data/", "link-local: cloud metadata endpoint"),
    ("https://10.0.5.9/internal", "private 10.0.0.0/8"),
    ("https://172.16.0.1/internal", "private 172.16.0.0/12"),
    ("https://192.168.1.1/internal", "private 192.168.0.0/16"),
    ("https://[::1]/internal", "IPv6 loopback"),
    ("", "empty"),
])
def test_unsafe_output_urls_are_refused_before_any_fetch(url, why):
    with pytest.raises(EngineFailed) as caught:
        assert_public_https_url(url, "test")
    assert caught.value.kind == FailureKind.TEMPORARY
    assert caught.value.resubmit_safe is False


def test_video_metadata_is_read_from_the_file_itself():
    assert video_metadata(tiny_mp4(560, 832, 3.56)) == (560, 832, pytest.approx(3.56))
    assert video_metadata(b"not a video") == (None, None, None)


def tiny_mp4_with_frame_table(sample_counts: list[int], *, width: int = 480, height: int = 832,
                              seconds: float = 4.963) -> bytes:
    """Like tiny_mp4(), but the track also carries a real stts (time-to-sample) box — the exact
    structure video_frame_count() (CP46) reads. `sample_counts` is a list of (count) entries, each
    implicitly sharing one nominal delta; the sum is the total frame count callers should get back.
    """
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\x00" * 80)
    tkhd = _box(b"tkhd", b"\x00" * 76 + struct.pack(">II", width << 16, height << 16))
    entries = b"".join(struct.pack(">II", count, 1) for count in sample_counts)
    stts = _box(b"stts", b"\x00\x00\x00\x00" + struct.pack(">I", len(sample_counts)) + entries)
    stbl = _box(b"stbl", stts)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", tkhd + mdia)
    return _box(b"ftyp", b"mp42\x00\x00\x00\x00") + _box(b"moov", mvhd + trak)


def test_video_frame_count_reads_the_real_track_sample_table(): # CP46
    # CP40's real driving video, reproduced deterministically: 119 real frames (confirmed against
    # both this box parser and ffprobe independently in CP46's own report), not the 120
    # duration*assumed-24fps would suggest.
    assert video_frame_count(tiny_mp4_with_frame_table([119])) == 119
    # Multiple stts entries (a real encoder often emits more than one run) sum correctly.
    assert video_frame_count(tiny_mp4_with_frame_table([40, 40, 39])) == 119
    # No stts box at all (tiny_mp4()'s own plain shape): a clean None, never a guess.
    assert video_frame_count(tiny_mp4()) is None
    assert video_frame_count(b"not a video") is None


# ── ZeroGPU ─────────────────────────────────────────────────────────────────

def test_space_hosts_follow_hugging_face_naming():
    assert space_host("Tongyi-MAI/Z-Image-Turbo") == "https://tongyi-mai-z-image-turbo.hf.space"
    assert space_host("zerogpu-aoti/wan2-2-fp8da-aoti-faster") == "https://zerogpu-aoti-wan2-2-fp8da-aoti-faster.hf.space"


def _completed(output: dict, success: bool = True) -> str:
    return "data: " + json.dumps({"msg": "process_completed", "event_id": "ev1", "success": success, "output": output}) + "\n\n"


def zerogpu_handler(seen: dict, *, stage="RUNNING", events: str | None = None):
    host = "https://kingnish-wan2-2-fast.hf.space"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://huggingface.co/api/spaces/"):
            return httpx.Response(200, json={"runtime": {"stage": stage}})
        if url == f"{host}/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "protocol": "sse_v3", "dependencies": [
                {"id": 0, "api_name": "handle_image_upload_for_dims_wan"}, {"id": 2, "api_name": "generate_video"}]})
        if url == f"{host}/gradio_api/upload":
            seen["upload"] = True
            return httpx.Response(200, json=["/tmp/gradio/source.png"])
        if url == f"{host}/gradio_api/queue/join":
            body = json.loads(request.content)
            seen["data"], seen["fn_index"] = body["data"], body["fn_index"]
            seen["session_hash"] = body["session_hash"]
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"event_id": "ev1"})
        if url.startswith(f"{host}/gradio_api/queue/data"):
            assert request.url.params["session_hash"] == seen["session_hash"]
            body = events or (
                'data: {"msg": "estimation", "event_id": "ev1", "rank": 0}\n\n'
                'data: {"msg": "heartbeat"}\n\n'
                'data: {"msg": "process_starts", "event_id": "ev1"}\n\n'
                + _completed({"data": [{"video": {"path": "/tmp/out.mp4", "url": f"{host}/gradio_api/file=/tmp/out.mp4"},
                                        "subtitles": None}, 7]})
                + 'data: {"msg": "close_stream"}\n\n'
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if "gradio_api/file=" in url:
            seen["download_auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    return handler


async def test_zerogpu_runs_a_space_with_the_account_token_and_reads_the_real_video():
    seen: dict = {}
    result = await ZeroGPURunner(factory(zerogpu_handler(seen))).run(job())
    assert seen["fn_index"] == 2
    assert seen["data"][:4] == [None, "ondas no mar", 832, 480]
    assert seen["data"][5:] == [3.0, 0.0, 4, 7, False]
    assert seen["auth"] == "Bearer hf-test" and seen["download_auth"] == "Bearer hf-test"
    assert (result.width, result.height) == (480, 832) and result.duration_seconds == pytest.approx(3.5)
    assert result.cost_usd == 0.0 and result.content_type == "video/mp4"


async def test_zerogpu_uploads_the_first_frame_for_image_to_video():
    seen: dict = {}
    await ZeroGPURunner(factory(zerogpu_handler(seen))).run(
        job(task=IMAGE_TO_VIDEO, source=b"png", source_content_type="image/png"))
    assert seen["upload"] is True and seen["data"][0]["path"] == "/tmp/gradio/source.png"


@pytest.mark.parametrize("message,kind,retry_after", [
    ("No GPU was available after 60s. Create a free account to get a higher priority", FailureKind.GPU_UNAVAILABLE, None),
    ("You have exceeded your ZeroGPU quota (120s requested vs. 40s left). Try again in 18:38:07. "
     "Authenticate with a Hugging Face token for more quota", FailureKind.QUOTA, 18 * 3600 + 38 * 60 + 7),
    ("CUDA out of memory", FailureKind.OOM, None),
])
async def test_zerogpu_reads_the_spaces_own_error_and_classifies_it(message, kind, retry_after):
    events = _completed({"error": message}, success=False)
    with pytest.raises(EngineFailed) as caught:
        await ZeroGPURunner(factory(zerogpu_handler({}, events=events))).run(job())
    assert caught.value.kind == kind and caught.value.retryable is True
    assert caught.value.retry_after == retry_after


async def test_zerogpu_unknown_endpoint_is_unavailable(monkeypatch):
    monkeypatch.setattr(zerogpu, "PROFILES", {**zerogpu.PROFILES, "wan22_ti2v_5b": zerogpu.PROFILES["wan22_ti2v_5b"].__class__(
        "wan22_ti2v_5b", zerogpu.PROFILES["wan22_ti2v_5b"].tasks, "media_zerogpu_wan22_ti2v_space",
        lambda job: "renamed_endpoint", zerogpu.PROFILES["wan22_ti2v_5b"].build, "video")})
    with pytest.raises(EngineUnavailable):
        await ZeroGPURunner(factory(zerogpu_handler({}))).run(job())


async def test_a_space_that_is_not_running_is_unavailable_without_submitting():
    seen: dict = {}
    with pytest.raises(EngineUnavailable):
        await ZeroGPURunner(factory(zerogpu_handler(seen, stage="SLEEPING"))).run(job())
    assert "data" not in seen


def test_zerogpu_is_experimental_and_free_inside_its_quota():
    runner = ZeroGPURunner()
    assert runner.experimental is True and runner.commercial is False
    assert runner.estimate_usd("wan22_ti2v_5b", 40.0) == 0.0
    assert runner.supports("wan22_i2v_a14b", IMAGE_TO_VIDEO) and not runner.supports("wan22_i2v_a14b", TEXT_TO_VIDEO)


# ── BiRefNet (CP20: schema verified against the real ZhengPeng7/BiRefNet_demo) ──

BIREFNET_HOST = "https://zhengpeng7-birefnet-demo.hf.space"


def _birefnet_job(**overrides) -> RunnerJob:
    values = dict(engine="birefnet", task=BACKGROUND_REMOVE, prompt="", width=0, height=0, seed=0,
                  source=b"source-bytes", source_content_type="image/png")
    values.update(overrides)
    return RunnerJob(**values)


def birefnet_handler(seen: dict, *, output=None, stage="RUNNING"):
    """A minimal fake of the real Space: api_name="image", 3 positional args, ImageSlider output —
    all read from ZhengPeng7/BiRefNet_demo's own /gradio_api/info and app.py, not invented."""
    host = BIREFNET_HOST
    default_output = {"data": [[{"path": "/tmp/original.png", "url": f"{host}/gradio_api/file=/tmp/original.png"},
                                {"path": "/tmp/masked.png", "url": f"{host}/gradio_api/file=/tmp/masked.png"}]]}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://huggingface.co/api/spaces/"):
            return httpx.Response(200, json={"runtime": {"stage": stage}})
        if url == f"{host}/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "protocol": "sse_v3",
                                             "dependencies": [{"id": 2, "api_name": "image"}]})
        if url == f"{host}/gradio_api/upload":
            return httpx.Response(200, json=["/tmp/gradio/source.png"])
        if url == f"{host}/gradio_api/queue/join":
            body = json.loads(request.content)
            seen["data"], seen["fn_index"] = body["data"], body["fn_index"]
            seen["session_hash"] = body["session_hash"]
            return httpx.Response(200, json={"event_id": "ev1"})
        if url.startswith(f"{host}/gradio_api/queue/data"):
            return httpx.Response(200, text=_completed(output if output is not None else default_output),
                                  headers={"content-type": "text/event-stream"})
        if "gradio_api/file=" in url:
            seen.setdefault("downloaded", []).append(url)
            return httpx.Response(200, content=PNG_1PX, headers={"content-type": "image/png"})
        return httpx.Response(404)

    return handler


PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
          b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture
def birefnet_space(monkeypatch):
    """These tests exercise the Space directly, so they configure it locally — CP20 does not
    change the real default (still ""), pinned separately by test_birefnet_space_is_unset_by_default."""
    monkeypatch.setenv("MEDIA_ZEROGPU_BIREFNET_SPACE", "ZhengPeng7/BiRefNet_demo")
    media_settings.cache_clear()
    yield
    media_settings.cache_clear()


async def test_birefnet_uses_the_real_verified_api_name_and_three_args(birefnet_space):
    seen: dict = {}
    await ZeroGPURunner(factory(birefnet_handler(seen))).run(
        _birefnet_job(extra={"resolution": "1024x1024", "weights": "Portrait"}))
    assert seen["fn_index"] == 2  # the "image" dependency's id in the real /config
    assert len(seen["data"]) == 3
    assert seen["data"][1:] == ["1024x1024", "Portrait"]  # [image, resolution, weights]


async def test_birefnet_defaults_weights_to_general_and_resolution_to_empty_when_not_supplied(birefnet_space):
    seen: dict = {}
    await ZeroGPURunner(factory(birefnet_handler(seen))).run(_birefnet_job())
    assert seen["data"][1:] == ["", "General"]


async def test_birefnet_extracts_the_second_imageslider_image_not_the_first(birefnet_space):
    """The pair is (original, masked) per the Space's own `return (image_ori, image_masked)` —
    index 0 is the ORIGINAL unprocessed input; using it would be silently wrong."""
    seen: dict = {}
    result = await ZeroGPURunner(factory(birefnet_handler(seen))).run(_birefnet_job())
    assert seen["downloaded"] == [f"{BIREFNET_HOST}/gradio_api/file=/tmp/masked.png"]
    assert result.data == PNG_1PX and result.content_type == "image/png"


@pytest.mark.parametrize("malformed_output", [
    {"data": [None]},
    {"data": [[]]},
    {"data": [[{"path": "/tmp/only-one.png"}]]},  # a 1-item, not 2-item, pair
    {"data": [[None, None]]},                     # a valid-shaped pair with no real file in either slot
    {"data": []},
])
async def test_birefnet_malformed_output_is_a_controlled_failure_not_a_crash(birefnet_space, malformed_output):
    with pytest.raises(EngineFailed) as caught:
        await ZeroGPURunner(factory(birefnet_handler({}, output=malformed_output))).run(_birefnet_job())
    assert caught.value.kind == FailureKind.TEMPORARY


async def test_birefnet_result_never_names_the_space_or_provider(birefnet_space):
    result = await ZeroGPURunner(factory(birefnet_handler({}))).run(_birefnet_job())
    dump = str(result.__dict__).lower()
    assert "birefnet" not in dump and "zhengpeng7" not in dump and "gradio" not in dump


def test_birefnet_space_is_unset_by_default():
    from src.media.settings import media_settings as _settings

    assert _settings().media_zerogpu_birefnet_space == ""


# ── Multi-file upload (CP22: RunnerJob.extra_files, permanent — no real Space wired to it yet,
# see media/engine_catalog.py's Wan2.2-Animate-14B entry for why. Exercised here through a
# test-only SpaceProfile, temporarily registered in zerogpu.PROFILES, not a real engine). ──────

MULTI_HOST = "https://owner-test-multi.hf.space"  # space_host("owner/test-multi")


def _multi_build(job, uploaded):
    return [uploaded.get("source"), uploaded.get("driving_video")]


@pytest.fixture
def multi_input_profile(monkeypatch):
    """A synthetic 2-input profile — proves the generic multi-file mechanism works without
    depending on any real (and, for Animate, not yet trustworthy — see engine_catalog.py)
    Space schema. Reuses `media_zerogpu_birefnet_space` (a real settings field, normally unset)
    as this test profile's own space-setting name — simplest way to point a synthetic profile at
    a real, settable field without adding a settings field that only a test would ever use."""
    from src.media.runners.zerogpu import SpaceProfile

    monkeypatch.setattr(zerogpu, "PROFILES", {
        **zerogpu.PROFILES,
        "test_multi": SpaceProfile("test_multi", frozenset({CHARACTER_ANIMATE}), "media_zerogpu_birefnet_space",
                                   lambda job: "predict", _multi_build, "image"),
    })
    monkeypatch.setenv("MEDIA_ZEROGPU_BIREFNET_SPACE", "owner/test-multi")
    media_settings.cache_clear()
    yield
    media_settings.cache_clear()


def _multi_job(**overrides) -> RunnerJob:
    values = dict(engine="test_multi", task=CHARACTER_ANIMATE, prompt="", width=0, height=0, seed=0,
                  source=b"character-bytes", source_content_type="image/png",
                  extra_files={"driving_video": (b"video-bytes", "video/mp4")})
    values.update(overrides)
    return RunnerJob(**values)


def multi_handler(seen: dict, *, fail_role: str | None = None, fail_status: int = 500):
    """Tracks every /upload call (order, filename, content-type) and the final queue/join data."""
    host = MULTI_HOST
    upload_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://huggingface.co/api/spaces/"):
            return httpx.Response(200, json={"runtime": {"stage": "RUNNING"}})
        if url == f"{host}/config":
            return httpx.Response(200, json={"api_prefix": "/gradio_api", "protocol": "sse_v3",
                                             "dependencies": [{"id": 1, "api_name": "predict"}]})
        if url == f"{host}/gradio_api/upload":
            body = request.content
            # multipart body carries the filename/content-type; cheap sniff without a full parser.
            text = body.decode("latin-1")
            filename = text.split('filename="', 1)[1].split('"', 1)[0]
            role = filename.rsplit(".", 1)[0]
            seen.setdefault("uploads", []).append(role)
            if fail_role and role == fail_role:
                return httpx.Response(fail_status, text="upload failed")
            upload_count["n"] += 1
            return httpx.Response(200, json=[f"/tmp/gradio/{role}-{upload_count['n']}"])
        if url == f"{host}/gradio_api/queue/join":
            seen["data"] = json.loads(request.content)["data"]
            return httpx.Response(200, json={"event_id": "ev1"})
        if url.startswith(f"{host}/gradio_api/queue/data"):
            output = {"data": [{"path": "/tmp/result.png", "url": f"{host}/gradio_api/file=/tmp/result.png"}]}
            return httpx.Response(200, text=_completed(output), headers={"content-type": "text/event-stream"})
        if "gradio_api/file=" in url:
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0", headers={"content-type": "image/png"})
        return httpx.Response(404)

    return handler


async def test_zerogpu_uploads_every_file_and_preserves_role_association(multi_input_profile):
    seen: dict = {}
    await ZeroGPURunner(factory(multi_handler(seen))).run(_multi_job())
    assert seen["uploads"] == ["source", "driving_video"]  # A: source first, B: role preserved
    assert len(seen["data"]) == 2
    assert seen["data"][0]["path"] == "/tmp/gradio/source-1"
    assert seen["data"][1]["path"] == "/tmp/gradio/driving_video-2"


async def test_zerogpu_single_input_job_is_unaffected_by_the_multi_file_machinery(multi_input_profile):
    """A: single-input still works — a job with no extra_files uploads exactly one file, exactly
    like every engine before CP22."""
    seen: dict = {}
    await ZeroGPURunner(factory(multi_handler(seen))).run(_multi_job(extra_files={}))
    assert seen["uploads"] == ["source"]  # only the one file this job actually carries
    assert seen["data"][0] == {"path": "/tmp/gradio/source-1", "orig_name": "source-1", "mime_type": "image/png",
                               "meta": {"_type": "gradio.FileData"}}
    assert seen["data"][1] is None  # this build() always asks for driving_video; none was supplied


async def test_zerogpu_uses_the_right_extension_and_content_type_per_file(multi_input_profile):
    """F: content-type correct — the video file must not default to image/png the way an
    unrecognized type would."""
    seen: dict = {}
    await ZeroGPURunner(factory(multi_handler(seen))).run(_multi_job())
    assert seen["data"][0]["mime_type"] == "image/png"
    assert seen["data"][1]["mime_type"] == "video/mp4"


async def test_zerogpu_does_not_reupload_identical_bytes_across_roles(multi_input_profile):
    """No literal duplicate-input test case exists in this job, so this proves the dedup rule
    directly: the same bytes under two different roles is uploaded exactly once."""
    seen: dict = {}
    job = _multi_job(extra_files={"driving_video": (b"character-bytes", "image/png")})  # same bytes as source
    await ZeroGPURunner(factory(multi_handler(seen))).run(job)
    assert seen["uploads"] == ["source"]  # driving_video's identical bytes reused source's upload
    assert seen["data"][0] == seen["data"][1]  # both roles point at the same uploaded file


async def test_zerogpu_reports_which_role_failed_to_upload_without_losing_the_first_ones_success(multi_input_profile):
    """O: failure de upload — individual, not an opaque whole-batch failure; and proves uploads
    really are sequential (the first file's upload genuinely completed before the second failed)."""
    seen: dict = {}
    with pytest.raises(EngineFailed) as caught:
        await ZeroGPURunner(factory(multi_handler(seen, fail_role="driving_video"))).run(_multi_job())
    assert "driving_video" in caught.value.reason
    assert seen["uploads"] == ["source", "driving_video"]  # source's own upload call did happen


async def test_zerogpu_extra_files_upload_order_matches_declared_order_deterministically(multi_input_profile):
    """Q: deterministic ordering — three files, uploaded and joined in the exact declared order."""
    seen: dict = {}
    job = _multi_job(extra_files={"driving_video": (b"video-bytes", "video/mp4"),
                                  "style_reference": (b"style-bytes", "image/webp")})
    await ZeroGPURunner(factory(multi_handler(seen))).run(job)
    assert seen["uploads"] == ["source", "driving_video", "style_reference"]


# ── Modal ───────────────────────────────────────────────────────────────────

async def test_modal_runs_the_worker_job_and_prices_its_gpu_seconds():
    seen: dict = {}
    states = iter([{"status": "queued"}, {"status": "running"},
                   {"status": "completed", "output": {"content_type": "video/mp4",
                                                     "data_base64": base64.b64encode(MP4).decode(),
                                                     "metrics": {"gpu_seconds": 40.0, "cold_start_seconds": 12.0,
                                                                 "inference_seconds": 28.0}}}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json={"id": "job1"})
        return httpx.Response(200, json=next(states))

    result = await ModalRunner(factory(handler), sleep=no_sleep).run(job())
    assert seen["body"]["job"]["engine"] == "wan22_ti2v_5b" and seen["auth"] == "Bearer modal-test"
    assert result.gpu_seconds == 40.0 and result.cold_start_seconds == 12.0
    assert result.cost_usd == pytest.approx(40.0 * media_settings().media_modal_gpu_usd_per_second)
    assert result.metrics()["output_bytes"] == len(MP4)


async def test_modal_worker_failure_kind_is_kept():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job1"})
        return httpx.Response(200, json={"status": "failed", "error": {"kind": "oom", "message": "720p needs 80GB"}})

    with pytest.raises(EngineFailed) as caught:
        await ModalRunner(factory(handler), sleep=no_sleep).run(job())
    assert caught.value.kind == FailureKind.OOM


async def test_modal_job_that_never_leaves_the_queue_is_a_cold_start_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job1"} if request.method == "POST" else {"status": "queued"})

    runner = ModalRunner(factory(handler), clock=ticking(0.0, 700.0), sleep=no_sleep)
    with pytest.raises(EngineFailed) as caught:
        await runner.run(job())
    assert caught.value.kind == FailureKind.COLD_START_TIMEOUT


async def test_modal_refuses_an_engine_it_has_not_deployed():
    with pytest.raises(EngineUnavailable):
        await ModalRunner(factory(lambda request: httpx.Response(500)), sleep=no_sleep).run(job(engine="wan22_i2v_a14b"))


async def test_modal_passes_the_reservation_to_the_worker_as_gpu_seconds():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "job1"})
        return httpx.Response(200, json={"status": "completed", "output": {
            "content_type": "video/mp4", "data_base64": base64.b64encode(MP4).decode(), "metrics": {"gpu_seconds": 9.0}}})

    await ModalRunner(factory(handler), sleep=no_sleep).run(job(budget_usd=0.0231))
    await ModalRunner(factory(handler), sleep=no_sleep).run(job())
    price = media_settings().media_modal_gpu_usd_per_second
    assert seen[0]["limits"] == {"max_gpu_seconds": round(0.0231 / price, 2)}
    assert "limits" not in seen[1] and "budget_usd" not in seen[0]["job"]


@pytest.mark.parametrize("ticks,kind", [((0.0, 700.0), FailureKind.COLD_START_TIMEOUT),
                                        ((0.0, 5.0, 4000.0), FailureKind.TIMEOUT)])
async def test_modal_cancels_a_job_it_stops_waiting_for(ticks, kind):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job1"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(200, json={"status": "queued" if len(calls) < 3 and kind == FailureKind.COLD_START_TIMEOUT
                                         else "running"})

    with pytest.raises(EngineFailed) as caught:
        await ModalRunner(factory(handler), clock=ticking(*ticks), sleep=no_sleep).run(job())
    assert caught.value.kind == kind
    assert ("DELETE", "/v1/jobs/job1") in calls


async def test_modal_reports_a_cancelled_job_as_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job1"} if request.method == "POST" else {"status": "cancelled"})

    with pytest.raises(EngineFailed):
        await ModalRunner(factory(handler), sleep=no_sleep).run(job())


# A cold start in production: ~100 s "queued", then ~17 s "running" (measured 2026-09-27). Status checks are
# polled every 3 s; the first video in production failed when one check failed and the whole job was dropped.

class FakeTime:
    """A clock that only moves when the runner sleeps between status checks."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # This module sets the poll interval to 0; time still passes between checks as in production (3 s).
        self.now += max(seconds, 3.0)


def _completed_clip() -> dict:
    return {"status": "completed", "output": {"content_type": "video/mp4", "data_base64": base64.b64encode(MP4).decode(),
                                              "metrics": {"gpu_seconds": 17.0, "cold_start_seconds": 72.0}}}


def cold_start_worker(time: FakeTime, calls: list, *, broken_checks: dict | None = None, queued_for: float = 100.0,
                      running_for: float = 17.0):
    """Submit, then queued/running/completed by the clock; `broken_checks` maps a check number to what goes wrong."""
    broken_checks = broken_checks or {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job1"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        check = sum(1 for method, _ in calls if method == "GET")
        broken = broken_checks.get(check)
        if broken == "timeout":
            raise httpx.ReadTimeout("status took too long", request=request)
        if broken == "dropped":
            raise httpx.ConnectError("connection reset", request=request)
        if isinstance(broken, int):
            return httpx.Response(broken, json={"detail": "worker says no"})
        if time.now < queued_for:
            return httpx.Response(200, json={"status": "queued"})
        if time.now < queued_for + running_for:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(200, json=_completed_clip())

    return handler


@pytest.mark.parametrize("broken_checks", [
    {},
    {10: "timeout"},                                  # one slow check in the middle of the cold start
    {20: "dropped", 21: 502, 22: 503, 23: 429},       # a burst of failures shorter than the grace
    {34: "timeout", 35: "timeout"},                   # failures while the clip is already running
])
async def test_modal_waits_through_a_cold_start_even_when_some_status_checks_fail(broken_checks):
    time, calls = FakeTime(), []
    runner = ModalRunner(factory(cold_start_worker(time, calls, broken_checks=broken_checks)),
                         clock=time.clock, sleep=time.sleep)

    result = await runner.run(job())

    assert result.gpu_seconds == 17.0 and result.cold_start_seconds == 72.0
    assert time.now >= 117.0  # it really waited through queue + inference
    assert ("DELETE", "/v1/jobs/job1") not in calls  # a delivered clip is never cancelled


async def test_modal_gives_up_after_status_checks_keep_failing_and_cancels_the_clip():
    time, calls = FakeTime(), []
    broken = {check: "timeout" for check in range(5, 200)}
    runner = ModalRunner(factory(cold_start_worker(time, calls, broken_checks=broken)), clock=time.clock,
                         sleep=time.sleep)

    with pytest.raises(EngineFailed) as caught:
        await runner.run(job())

    assert caught.value.kind == FailureKind.TIMEOUT
    grace = media_settings().media_modal_poll_failure_grace_s
    assert grace < time.now < grace + 30  # held on for the grace, not a moment on the first failure
    assert calls[-1] == ("DELETE", "/v1/jobs/job1")


@pytest.mark.parametrize("status", [404, 401])
async def test_a_status_the_worker_answered_for_sure_ends_the_wait_at_once(status):
    time, calls = FakeTime(), []
    runner = ModalRunner(factory(cold_start_worker(time, calls, broken_checks={3: status})), clock=time.clock,
                         sleep=time.sleep)

    with pytest.raises(EngineFailed):
        await runner.run(job())

    assert sum(1 for method, _ in calls if method == "GET") == 3
    assert ("DELETE", "/v1/jobs/job1") in calls


async def test_a_failure_the_worker_reports_is_final_and_needs_no_cancel():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job1"})
        return httpx.Response(200, json={"status": "failed", "error": {"kind": "oom", "message": "out of memory"}})

    with pytest.raises(EngineFailed) as caught:
        await ModalRunner(factory(handler), sleep=no_sleep).run(job())

    assert caught.value.kind == FailureKind.OOM
    assert "DELETE" not in calls


async def test_when_the_job_itself_is_cancelled_the_clip_on_modal_is_cancelled_too():
    import asyncio

    time, calls = FakeTime(), []
    runner = ModalRunner(factory(cold_start_worker(time, calls)), clock=time.clock, sleep=time.sleep)
    polled = asyncio.Event()
    real_sleep = runner._sleep

    async def sleep(seconds):
        polled.set()
        await real_sleep(seconds)
        await asyncio.sleep(0)

    runner._sleep = sleep
    task = asyncio.create_task(runner.run(job()))
    await polled.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls[-1] == ("DELETE", "/v1/jobs/job1")


def test_modal_serves_only_the_tasks_the_worker_declares(monkeypatch):
    runner = ModalRunner()
    assert runner.supports("wan22_ti2v_5b", IMAGE_TO_VIDEO)  # no declaration: every task of its engines
    monkeypatch.setenv("MEDIA_MODAL_TASKS", "text_to_video")
    media_settings.cache_clear()
    assert runner.supports("wan22_ti2v_5b", TEXT_TO_VIDEO)
    assert not runner.supports("wan22_ti2v_5b", IMAGE_TO_VIDEO)


# ── RunPod ──────────────────────────────────────────────────────────────────

async def test_runpod_runs_the_serverless_job_and_prices_the_platform_execution_time():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/ep1/run"):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json={"id": "rp1", "status": "IN_QUEUE"})
        if url.endswith("/ep1/status/rp1"):
            return httpx.Response(200, json={"status": "COMPLETED", "delayTime": 9000, "executionTime": 42000,
                                             "output": {"url": "https://files.example/out.mp4",
                                                        "content_type": "video/mp4"}})
        if url == "https://files.example/out.mp4":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    result = await RunPodRunner(factory(handler), sleep=no_sleep).run(job())
    assert seen["body"]["input"]["job"]["prompt"] == "ondas no mar" and seen["auth"] == "Bearer rp-test"
    assert seen["body"]["policy"]["executionTimeout"] > 0
    assert result.gpu_seconds == 42.0 and result.queue_seconds == 9.0
    assert result.cost_usd == pytest.approx(42.0 * media_settings().media_runpod_gpu_usd_per_second)


@pytest.mark.parametrize("state,kind", [
    ({"status": "FAILED", "error": "torch.OutOfMemoryError: CUDA out of memory"}, FailureKind.OOM),
    ({"status": "TIMED_OUT"}, FailureKind.TIMEOUT),
])
async def test_runpod_failures_are_classified(state, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "rp1"} if str(request.url).endswith("/run") else state)

    with pytest.raises(EngineFailed) as caught:
        await RunPodRunner(factory(handler), sleep=no_sleep).run(job())
    assert caught.value.kind == kind


async def test_runpod_rate_limit_keeps_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, text="Too Many Requests")

    with pytest.raises(EngineFailed) as caught:
        await RunPodRunner(factory(handler), sleep=no_sleep).run(job())
    assert caught.value.kind == FailureKind.RATE_LIMITED and caught.value.retry_after == 3.0


async def test_runpod_cancels_a_job_stuck_in_the_queue():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/run"):
            return httpx.Response(200, json={"id": "rp1"})
        if "/cancel/" in url:
            seen["cancelled"] = True
            return httpx.Response(200, json={"status": "CANCELLED"})
        return httpx.Response(200, json={"status": "IN_QUEUE"})

    with pytest.raises(EngineFailed) as caught:
        await RunPodRunner(factory(handler), clock=ticking(0.0, 700.0), sleep=no_sleep).run(job())
    assert caught.value.kind == FailureKind.COLD_START_TIMEOUT and seen["cancelled"] is True


def test_gpu_runners_are_commercial_and_estimate_with_a_cold_start_allowance():
    runner = RunPodRunner()
    assert runner.commercial is True and runner.experimental is False
    settings = media_settings()
    expected = (60.0 + settings.media_gpu_cold_start_allowance_s) * settings.media_runpod_gpu_usd_per_second
    assert runner.estimate_usd("wan22_ti2v_5b", 60.0) == pytest.approx(expected)
    assert runner.estimate_usd("wan22_ti2v_5b", None) is None
    assert runner.supports("wan22_ti2v_5b", TEXT_TO_VIDEO) and not runner.supports("ltx_video_13b_distilled", TEXT_TO_VIDEO)


# ── One worker, two GPU classes: each engine is priced at its own GPU ───────

async def test_modal_prices_the_a14b_at_the_h200_rate_and_the_5b_at_the_h100_rate(monkeypatch):
    monkeypatch.setenv("MEDIA_MODAL_ENGINES", "wan22_ti2v_5b,wan22_t2v_a14b")
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "job1"})
        return httpx.Response(200, json={"status": "completed", "output": {
            "content_type": "video/mp4", "data_base64": base64.b64encode(MP4).decode(), "metrics": {"gpu_seconds": 57.0}}})

    maximum = await ModalRunner(factory(handler), sleep=no_sleep).run(
        job(engine="wan22_t2v_a14b", budget_usd=0.0931))
    economy = await ModalRunner(factory(handler), sleep=no_sleep).run(job(budget_usd=0.0931))

    assert maximum.cost_usd == pytest.approx(57.0 * 0.001261)
    assert economy.cost_usd == pytest.approx(57.0 * media_settings().media_modal_gpu_usd_per_second)
    assert seen[0]["limits"] == {"max_gpu_seconds": round(0.0931 / 0.001261, 2)}  # the H200 seconds it can pay for
    assert seen[1]["limits"] == {"max_gpu_seconds": round(0.0931 / 0.001097, 2)}


def test_a_malformed_per_engine_price_falls_back_to_the_endpoint_price(monkeypatch):
    monkeypatch.setenv("MEDIA_MODAL_GPU_USD_PER_SECOND_BY_ENGINE", "wan22_t2v_a14b=cheap, =0.5, other=-1")
    runner = ModalRunner()
    assert runner._price("wan22_t2v_a14b") == media_settings().media_modal_gpu_usd_per_second
    assert runner._price("other") == media_settings().media_modal_gpu_usd_per_second


async def test_maximum_mode_reaches_only_the_a14b_route_and_economy_only_the_5b(monkeypatch):
    """The same Modal endpoint serves both; the mode's engine class decides which route a request may use,
    and the A14B route sends the measured Maximum shape (1280x720, 5 s, 16 fps, 4 steps)."""
    from src.media import modes
    from src.media.adapters import WAN22_T2V_A14B, WAN22_TI2V_5B
    from src.media.engines import VideoRequest
    from src.media.orchestrator import VideoEngineChain
    from src.media.routes import VideoRoute

    monkeypatch.setenv("MEDIA_MODAL_ENGINES", "wan22_ti2v_5b,wan22_t2v_a14b")
    monkeypatch.setenv("MEDIA_MODAL_TASKS", "text_to_video")
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(json.loads(request.content)["job"])
            return httpx.Response(200, json={"id": "job1"})
        if request.url.path.endswith("/v1/health"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "completed", "output": {
            "content_type": "video/mp4", "data_base64": base64.b64encode(MP4).decode(), "metrics": {"gpu_seconds": 57.0}}})

    runner = ModalRunner(factory(handler), sleep=no_sleep)
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, runner), VideoRoute(WAN22_T2V_A14B, runner)])
    maximum = modes.restrict(chain, modes.MAXIMUM)
    economy = modes.restrict(chain, modes.ECONOMY)
    assert [route.engine_id for route in maximum.engines] == ["wan22_t2v_a14b"]
    assert [route.engine_id for route in economy.engines] == ["wan22_ti2v_5b"]

    request = VideoRequest(prompt="um bonde subindo a ladeira", width=1280, height=720, duration_seconds=5, fps=24,
                           seed=1, quality_tier=modes.MAXIMUM.quality_tier)
    output = await maximum.generate(request)
    assert {k: seen[0][k] for k in ("engine", "width", "height", "duration_seconds", "fps", "steps")} == \
        {"engine": "wan22_t2v_a14b", "width": 1280, "height": 720, "duration_seconds": 5, "fps": 16, "steps": 4}
    assert output.cost_usd == pytest.approx(57.0 * 0.001261)
