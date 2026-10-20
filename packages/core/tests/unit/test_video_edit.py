"""video_edit: cuts, pacing and captions (media/video_edit.py).

The planning is pure and tested exhaustively without ffmpeg. The engine is then run for real
against a synthetic clip made by ffmpeg itself (tone, two seconds of silence, tone) — skipped only
where ffmpeg is not installed — with a scripted transcriber standing in for whisper.cpp.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.capability.multimodal import TranscriptSegment, TranscriptionResult
from src.media import video_edit
from src.media.engines import EngineFailed, FailureKind
from src.media.operations import OPERATION_INPUT_KINDS, OPERATION_OUTPUT_KIND, TRANSFORM_OPERATIONS
from src.media.runners.base import video_metadata
from src.media.video_edit import (
    Caption,
    FfmpegVideoEditEngine,
    count_cuts,
    parse_probe,
    plan_cuts,
    read_parameters,
    remap_captions,
    render_filter,
    split_caption,
    to_srt,
)
from src.voice.adapters.whisper_local import _segments

needs_ffmpeg = pytest.mark.skipif(video_edit.ffmpeg_path() is None, reason="ffmpeg is not installed")

PROBE = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'source':
  Duration: 00:00:05.02, start: 0.000000, bitrate: 120 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 320x568, 30 fps
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 16000 Hz, mono, fltp, 69 kb/s
[silencedetect @ 0000] silence_start: 1.0065
[silencedetect @ 0000] silence_end: 3.0012 | silence_duration: 1.9947
[silencedetect @ 0000] silence_start: 4.7
"""


# ── The operation is declared like every other TRANSFORM ────────────────────

def test_video_edit_is_a_transform_from_one_video_to_a_video():
    assert "video_edit" in TRANSFORM_OPERATIONS
    assert OPERATION_INPUT_KINDS["video_edit"] == {"source_video": "video"}
    assert OPERATION_OUTPUT_KIND["video_edit"] == "video"


# ── Probe ───────────────────────────────────────────────────────────────────

def test_probe_reads_duration_audio_and_closes_a_trailing_silence_at_the_end():
    duration, has_audio, silences = parse_probe(PROBE)
    assert duration == pytest.approx(5.02)
    assert has_audio is True
    assert silences == [(1.0065, 3.0012), (4.7, pytest.approx(5.02))]


def test_probe_of_a_silent_film_has_no_audio():
    duration, has_audio, silences = parse_probe("  Duration: 00:01:02.50, start: 0\n  Stream #0:0: Video: h264\n")
    assert duration == pytest.approx(62.5) and has_audio is False and silences == []


# ── Cuts and pacing ─────────────────────────────────────────────────────────

def test_normal_pace_cuts_a_long_pause_but_leaves_air_around_speech():
    keep = plan_cuts(5.0, [(1.0, 3.0)], "normal")
    assert keep == [(0.0, pytest.approx(1.15)), (pytest.approx(2.85), 5.0)]
    assert count_cuts(keep, 5.0) == 1


def test_a_pause_shorter_than_the_pace_threshold_stays():
    assert plan_cuts(5.0, [(1.0, 1.5)], "normal") == [(0.0, 5.0)]
    assert plan_cuts(5.0, [(1.0, 1.5)], "fast") != [(0.0, 5.0)]  # 0.5 s is a pause worth cutting at a fast pace


def test_faster_pace_cuts_more():
    silences = [(1.0, 1.8), (3.0, 4.2), (6.0, 6.4)]
    lengths = {pace: sum(b - a for a, b in plan_cuts(8.0, silences, pace)) for pace in ("off", "calm", "normal", "fast")}
    assert lengths["off"] == 8.0
    assert lengths["off"] > lengths["calm"] > lengths["normal"] > lengths["fast"]


def test_pauses_at_the_very_start_and_end_are_cut_entirely():
    keep = plan_cuts(10.0, [(0.0, 2.0), (8.5, 10.0)], "normal")
    assert keep == [(2.0 - 0.15, 8.5 + 0.15)]
    assert count_cuts(keep, 10.0) == 2


def test_off_and_all_silence_keep_the_whole_video():
    assert plan_cuts(5.0, [(1.0, 3.0)], "off") == [(0.0, 5.0)]
    assert plan_cuts(5.0, [(0.0, 5.0)], "fast") == [(0.0, 5.0)]  # nothing to pace: never an empty edit


def test_a_sliver_between_two_cuts_is_not_kept():
    keep = plan_cuts(6.0, [(1.0, 2.0), (2.25, 4.0)], "fast")  # 0.25 s gap minus 0.16 s of padding
    assert all(b - a >= video_edit.MIN_KEEP_SECONDS for a, b in keep)


# ── Captions ────────────────────────────────────────────────────────────────

def test_captions_move_onto_the_edited_timeline_and_the_ones_in_a_cut_disappear():
    keep = [(0.0, 1.0), (3.0, 5.0)]
    captions = [Caption(0.2, 0.8, "antes"), Caption(1.5, 2.5, "no corte"), Caption(3.5, 4.5, "depois"),
                Caption(0.5, 3.5, "atravessa o corte")]
    out = remap_captions(captions, keep)
    assert out == [Caption(0.2, 0.8, "antes"), Caption(1.5, 2.5, "depois"), Caption(0.5, 1.5, "atravessa o corte")]


def test_a_long_segment_becomes_short_lines_that_share_its_time():
    lines = split_caption(Caption(10.0, 14.0, "vamos editar esse vídeo agora mesmo com legendas automáticas"), 20)
    assert all(len(line.text) <= 20 for line in lines)
    assert " ".join(line.text for line in lines) == "vamos editar esse vídeo agora mesmo com legendas automáticas"
    assert lines[0].start == 10.0 and lines[-1].end == 14.0
    assert all(a.end == b.start for a, b in zip(lines, lines[1:]))


def test_srt_is_numbered_timed_and_never_carries_a_blank_line_inside_a_block():
    srt = to_srt([Caption(0.0, 1.5, "olá"), Caption(61.25, 3661.001, "linha\n\ncom quebra")])
    assert srt == ("1\n00:00:00,000 --> 00:00:01,500\nolá\n\n"
                   "2\n00:01:01,250 --> 01:01:01,001\nlinha com quebra\n")


def test_whisper_segments_are_read_and_malformed_ones_skipped():
    raw = [{"start": 0.0, "end": 1.2, "text": " olá  pessoal "}, {"start": 2, "end": 1, "text": "invertido"},
           {"start": "x", "end": 3, "text": "sem número"}, {"start": 3, "end": 4, "text": "   "}, "lixo"]
    assert _segments(raw) == (TranscriptSegment(0.0, 1.2, "olá pessoal"),)
    assert _segments(None) == ()


# ── Parameters and the filter graph ─────────────────────────────────────────

def test_parameters_default_to_normal_pace_with_portuguese_captions():
    assert read_parameters(None) == ("normal", True, "pt")
    assert read_parameters({"pace": "fast", "captions": False, "language": "en"}) == ("fast", False, "en")


@pytest.mark.parametrize("parameters", [{"pace": "turbo"}, {"captions": "yes"}, {"language": "pt; rm -rf"},
                                        {"language": 3}])
def test_malformed_parameters_are_refused(parameters):
    with pytest.raises(EngineFailed) as refused:
        read_parameters(parameters)
    assert refused.value.kind == FailureKind.REJECTED


def test_filter_graph_keeps_each_range_and_burns_a_relative_subtitle_file():
    graph = render_filter([(0.0, 1.15), (2.85, 5.0)], has_audio=True, subtitles="captions.srt")
    assert "[0:v]trim=start=0.000:end=1.150" in graph and "[0:a]atrim=start=2.850:end=5.000" in graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcut][aout]" in graph
    assert graph.endswith("[vout]") and "subtitles=captions.srt:" in graph


def test_filter_graph_for_a_silent_film_has_no_audio_branch():
    graph = render_filter([(0.0, 3.0)], has_audio=False, subtitles=None)
    assert graph == "[0:v]trim=start=0.000:end=3.000,setpts=PTS-STARTPTS[v0];[v0]concat=n=1:v=1:a=0[vout]"


# ── The engine, for real ────────────────────────────────────────────────────

def make_clip(path: Path) -> bytes:
    """5 s vertical clip: a tone for 1 s, 2 s of silence, a tone for 2 s."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=5",
         "-f", "lavfi", "-i", "aevalsrc='if(lt(t,1)+gt(t,3),0.5*sin(2*PI*440*t),0)':s=16000:d=5",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(path)],
        check=True)
    return path.read_bytes()


def frame_at(video: bytes, seconds: float, tmp_path: Path, name: str) -> bytes:
    (tmp_path / f"{name}.mp4").write_bytes(video)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(tmp_path / f"{name}.mp4"),
                    "-frames:v", "1", "-f", "image2", "-c:v", "png", "-y", str(tmp_path / f"{name}.png")], check=True)
    return (tmp_path / f"{name}.png").read_bytes()


class ScriptedTranscriber:
    def __init__(self, segments):
        self.segments, self.calls = segments, []

    async def __call__(self, audio: bytes, content_type: str, language):
        self.calls.append((content_type, language, audio[:4]))
        return TranscriptionResult(text="", segments=tuple(TranscriptSegment(*s) for s in self.segments))


def _request(**parameters):
    return SimpleNamespace(operation="video_edit", parameters=parameters)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if video_edit.ffmpeg_path() is None:
        pytest.skip("ffmpeg is not installed")
    return make_clip(tmp_path_factory.mktemp("clip") / "clip.mp4")


@needs_ffmpeg
async def test_the_pause_is_cut_and_captions_are_burned_in(clip, tmp_path):
    transcriber = ScriptedTranscriber([(0.1, 0.9, "olá pessoal"), (1.5, 2.5, "isto cai no corte"),
                                       (3.2, 4.8, "vamos editar esse vídeo com legendas automáticas")])
    engine = FfmpegVideoEditEngine(transcriber)
    output = await engine.run(_request(pace="normal", language="pt"),
                              {"source_video": (clip, "video/mp4")})

    assert output.content_type == "video/mp4" and output.cost_usd == 0.0
    assert output.width == 320 and output.height == 568
    assert output.duration_seconds == pytest.approx(3.3, abs=0.25)  # 5 s minus ~1.7 s of pause
    assert output.metrics["cuts"] == 1
    assert output.metrics["captions"] == 3  # "olá pessoal" + the long line split in two; the cut one is gone
    assert transcriber.calls == [("audio/ogg", "pt", b"OggS")]

    plain = await FfmpegVideoEditEngine(None).run(_request(pace="normal", captions=False),
                                                  {"source_video": (clip, "video/mp4")})
    assert plain.metrics["captions"] == 0
    assert plain.duration_seconds == pytest.approx(output.duration_seconds, abs=0.1)
    # Same cut, same frame time: the only difference is the caption drawn on it.
    assert frame_at(output.data, 0.5, tmp_path, "captioned") != frame_at(plain.data, 0.5, tmp_path, "plain")


@needs_ffmpeg
async def test_pace_off_keeps_the_whole_timeline(clip):
    output = await FfmpegVideoEditEngine(None).run(_request(pace="off", captions=False),
                                                   {"source_video": (clip, "video/mp4")})
    assert output.duration_seconds == pytest.approx(5.0, abs=0.15) and output.metrics["cuts"] == 0
    assert video_metadata(output.data)[2] == pytest.approx(5.0, abs=0.15)


@needs_ffmpeg
async def test_captions_without_local_transcription_fail_plainly_before_any_work():
    with pytest.raises(EngineFailed) as failed:
        await FfmpegVideoEditEngine(None).run(_request(), {"source_video": (b"not read", "video/mp4")})
    assert failed.value.kind == FailureKind.UNAVAILABLE and failed.value.retryable is False


@needs_ffmpeg
async def test_a_file_that_is_not_a_video_is_refused_not_retried():
    with pytest.raises(EngineFailed) as failed:
        await FfmpegVideoEditEngine(None).run(_request(captions=False), {"source_video": (b"\x00" * 256, "video/mp4")})
    assert failed.value.kind == FailureKind.UNSUPPORTED_FORMAT and failed.value.retryable is False


async def test_it_costs_nothing_and_is_available_exactly_when_ffmpeg_is(monkeypatch):
    engine = FfmpegVideoEditEngine(None)
    assert engine.estimate_usd(_request()) == 0.0
    monkeypatch.setattr(video_edit, "ffmpeg_path", lambda: None)
    assert await engine.is_available() is False
    monkeypatch.setattr(video_edit, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    assert await engine.is_available() is True
