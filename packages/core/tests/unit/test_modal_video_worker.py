"""The Modal video worker's own decisions (infra/modal/video_worker_logic.py), without Modal or a GPU."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[4] / "infra" / "modal" / "video_worker_logic.py"
_spec = importlib.util.spec_from_file_location("video_worker_logic", _PATH)
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)


def job(**overrides) -> dict:
    values = {"engine": "wan22_ti2v_5b", "task": "text_to_video", "prompt": "ondas nas pedras",
              "width": 832, "height": 480, "seed": 7, "duration_seconds": 5, "fps": 24, "frames": 121, "steps": 4}
    values.update(overrides)
    return values


@pytest.mark.parametrize("seconds,frames", [(5, 121), (6, 145), (1, 25), (3, 73)])
def test_frames_are_wan_shaped(seconds, frames):
    assert logic.frames_for(seconds) == frames and (frames - 1) % 4 == 0


@pytest.mark.parametrize("asked,made", [
    ((832, 480), (832, 480)),       # economy 480p as asked
    ((1248, 704), (1248, 704)),     # quality 720p (the Core's adapter size)
    ((1280, 720), (1248, 704)),     # a 720p ask: short side to 704, proportion kept, multiples of 32
    ((1920, 1080), (1248, 704)),    # anything larger comes down to the 720p class
    ((480, 832), (480, 832)),       # vertical keeps its proportion
    ((1080, 1920), (704, 1248)),
])
def test_sizes_stay_in_the_720p_class_and_on_the_grid(asked, made):
    assert logic.size_for(*asked) == made
    assert all(side % 32 == 0 for side in made)


def test_the_plan_the_gpu_runs():
    plan = logic.validate(job(width=1248, height=704, duration_seconds=6))
    assert (plan["width"], plan["height"], plan["frames"], plan["fps"], plan["steps"]) == (1248, 704, 145, 24, 3)
    # Predicted from the measured runs: 8.7 s for 832x480x121, 16.2 s for 1280x704x121.
    assert logic.predicted_seconds(832, 480, 121) == pytest.approx(8.7, abs=0.05)
    assert logic.predicted_seconds(1280, 704, 121) == pytest.approx(16.2, abs=0.05)


@pytest.mark.parametrize("overrides,kind", [
    ({"engine": "wan22_i2v_a14b"}, "rejected"),
    ({"task": "image_to_video"}, "rejected"),   # this checkpoint ignored the image: never served
    ({"prompt": "  "}, "rejected"),
    ({"prompt": "x" * 2001}, "rejected"),
    ({"duration_seconds": 7}, "unsupported_duration"),
    ({"duration_seconds": 8}, "unsupported_duration"),  # where the 8 s run froze
    ({"duration_seconds": None}, "unsupported_duration"),
    ({"width": 100, "height": 60}, "unsupported_format"),
])
def test_a_job_the_worker_will_not_run_is_refused_with_a_core_failure_kind(overrides, kind):
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(job(**overrides))
    assert refused.value.kind == kind


def test_a_clip_its_reservation_cannot_pay_for_never_reaches_the_gpu():
    # 5 s of 720p needs ~16 GPU s; a reservation worth 10 GPU s is refused before anything runs.
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(job(width=1248, height=704), {"max_gpu_seconds": 10})
    assert refused.value.kind == "rejected"  # the request, not the runner: no cooldown, no costlier fallback
    assert logic.validate(job(), {"max_gpu_seconds": 11.3})["max_gpu_seconds"] == 11.3


def test_motion_by_second_shows_a_frozen_second_half():
    np = pytest.importorskip("numpy")  # present in the worker image, not in the Core

    moving = [np.full((64, 64, 3), (i * 7) % 255, dtype=np.uint8) for i in range(25)]
    frozen = [moving[-1]] * 24
    per_second = logic.motion_by_second(moving + frozen)
    assert per_second[0] > 1 and per_second[-1] == 0


# ── Maximum: Wan 2.2 T2V-A14B + Lightning on H200 ───────────────────────────

def a14b_job(**overrides) -> dict:
    return job(**{"engine": "wan22_t2v_a14b", "width": 1280, "height": 720, "duration_seconds": 5, **overrides})


def test_maximum_runs_the_measured_shape_720p_5s_at_16fps_in_4_steps():
    plan = logic.validate(a14b_job())
    assert (plan["engine"], plan["width"], plan["height"], plan["frames"], plan["fps"], plan["steps"]) == \
        ("wan22_t2v_a14b", 1280, 720, 81, 16, 4)
    assert plan["guidance"] == 1.0
    assert plan["predicted_seconds"] == pytest.approx(56.8, abs=0.1)  # the H200 run it was measured on


def test_maximum_keeps_a_vertical_proportion_on_its_16px_grid():
    plan = logic.validate(a14b_job(width=720, height=1280))
    assert (plan["width"], plan["height"]) == (720, 1280)


def test_maximum_is_at_most_5_seconds_and_the_5b_still_goes_to_6():
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(a14b_job(duration_seconds=6))
    assert refused.value.kind == logic.UNSUPPORTED_DURATION
    assert logic.validate(job(duration_seconds=6))["engine"] == "wan22_ti2v_5b"


def test_a_maximum_clip_its_reservation_cannot_pay_for_never_reaches_the_h200():
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(a14b_job(), {"max_gpu_seconds": 40})
    assert refused.value.kind == logic.REJECTED
    # The Maximum reference reservation (9.31 TCU = US$ 0.0931) at the H200 price buys ~73.8 s: enough.
    assert logic.validate(a14b_job(), {"max_gpu_seconds": round(0.0931 / 0.001261, 2)})["max_gpu_seconds"] > 56.8


# ── Photo → Movement: Wan 2.2 I2V-A14B + Lightning on H200 ──────────────────

import base64 as _b64  # noqa: E402
import io as _io  # noqa: E402


def _jpeg(width: int = 832, height: int = 480) -> bytes:
    from PIL import Image

    buffer = _io.BytesIO()
    Image.new("RGB", (width, height), (200, 160, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def i2v_job(data: bytes | None = None, content_type: str = "image/jpeg", **overrides) -> dict:
    data = _jpeg() if data is None else data
    return job(**{"engine": "wan22_i2v_a14b_lightning", "task": "image_to_video", "width": 1280, "height": 720,
                  "duration_seconds": 5,
                  "source": {"content_type": content_type, "data_base64": _b64.b64encode(data).decode()}, **overrides})


def test_photo_movement_runs_the_maximum_shape_with_the_photo():
    plan = logic.validate(i2v_job())
    assert (plan["engine"], plan["width"], plan["height"], plan["frames"], plan["fps"], plan["steps"]) == \
        ("wan22_i2v_a14b_lightning", 1280, 720, 81, 16, 4)
    assert plan["source"][:3] == b"\xff\xd8\xff"  # the decoded photo rides to the GPU class


def test_a_photo_job_without_the_photo_is_refused_never_run_as_text():
    job_ = i2v_job()
    job_.pop("source")
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(job_)
    assert refused.value.kind == logic.REJECTED


@pytest.mark.parametrize("data, content_type, kind", [
    (b"%PDF-1.7 not an image", "image/jpeg", "unsupported_format"),   # says JPEG, is not
    (b"GIF89a", "image/gif", "unsupported_format"),                    # a type this engine does not take
])
def test_a_source_that_is_not_the_image_it_claims_is_refused(data, content_type, kind):
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(i2v_job(data, content_type))
    assert refused.value.kind == kind


def test_a_source_image_too_large_is_refused_before_the_gpu():
    big = b"\xff\xd8\xff" + b"0" * (logic.MAX_SOURCE_BYTES + 1)
    with pytest.raises(logic.JobRefused) as refused:
        logic.validate(i2v_job(big))
    assert refused.value.kind == logic.REJECTED


def test_each_engine_serves_only_its_own_task():
    with pytest.raises(logic.JobRefused):
        logic.validate(i2v_job(task="text_to_video"))
    with pytest.raises(logic.JobRefused):
        logic.validate(job(task="image_to_video"))  # the 5B ignores photos: it never takes one


def test_the_photo_becomes_a_first_frame_of_the_exact_size_without_stretching():
    frame = logic.fit_image(_jpeg(832, 480), 1280, 720)
    assert frame.size == (1280, 720) and frame.mode == "RGB"
    portrait = logic.fit_image(_jpeg(600, 900), 720, 1280)
    assert portrait.size == (720, 1280)
