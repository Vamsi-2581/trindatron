"""Video editing: cuts, pacing and burned-in captions — ffmpeg on this machine, $0 per job.

The `video_edit` TRANSFORM operation (media/operations.py): one source video in, one edited MP4 out.

  1. Probe  — one ffmpeg pass reads the duration, whether there is an audio track, and every
              stretch of silence (`silencedetect`).
  2. Pace   — `pace` picks how long a pause must be before it is cut and how much air is left
              around speech ("off" keeps the timeline untouched). `plan_cuts` turns the pauses
              into the ranges that stay.
  3. Captions — the audio is transcribed with the local whisper.cpp server (voice/adapters/
              whisper_local.py, also $0), its timed segments are split into short lines for a
              vertical frame and moved onto the edited timeline (`remap_captions`).
  4. Render — trim/atrim + concat keep exactly the planned ranges (frame-accurate even on the
              variable frame rate a phone records), and `subtitles` burns the captions in.

Like VTracer (media/vectorize.py) this is a classical tool, not a model: no weights, no GPU, no
network, and no route to rank against. ffmpeg is an external binary run with a fixed argument
list — no request text ever reaches a command line: captions go through a file in a private
temporary directory, referred to by a relative name so no path needs escaping.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from src.media.engines import EngineFailed, EngineUnavailable, FailureKind, MediaOutput

OPERATION = "video_edit"

#: pace -> (shortest pause that is cut, seconds of air kept on each side of speech).
PACES: dict[str, tuple[float, float] | None] = {
    "off": None,
    "calm": (1.0, 0.25),
    "normal": (0.6, 0.15),
    "fast": (0.35, 0.08),
}
DEFAULT_PACE = "normal"
SILENCE_THRESHOLD_DB = -35
MIN_KEEP_SECONDS = 0.1  # a kept sliver shorter than this is a click, not speech
MAX_CAPTION_CHARS = 32  # one short line, readable on a 9:16 frame
MIN_CAPTION_SECONDS = 0.1
CAPTION_STYLE = ("FontName=Arial,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=36")
FFMPEG_TIMEOUT_S = 900.0
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2})?$")

Range = tuple[float, float]


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


Transcriber = Callable[[bytes, str, str | None], Awaitable[object]]


# ── Pure planning (no ffmpeg) ────────────────────────────────────────────────

def parse_probe(stderr: str) -> tuple[float | None, bool, list[Range]]:
    """(duration, has_audio, silences) from the stderr of an ffmpeg `silencedetect` pass.
    A silence still open at the end of the file is closed at the duration."""
    duration = None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    has_audio = re.search(r"Stream #\d+:\d+.*?: Audio:", stderr) is not None
    silences: list[Range] = []
    start: float | None = None
    for kind, value in re.findall(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)", stderr):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            silences.append((start, float(value)))
            start = None
    if start is not None and duration is not None and duration > start:
        silences.append((start, duration))
    return duration, has_audio, silences


def plan_cuts(duration: float, silences: list[Range], pace: str = DEFAULT_PACE) -> list[Range]:
    """The ranges of the source that stay, in order. Pauses at least as long as the pace's
    threshold are cut, keeping `padding` of air next to speech; a pause at the very start or end
    of the file is cut entirely. Never returns an empty list: a file that is all pause is kept
    whole (there is nothing to pace), and "off" keeps everything."""
    whole = [(0.0, duration)]
    rule = PACES.get(pace)
    if rule is None or duration <= 0:
        return whole
    min_silence, padding = rule
    cuts: list[Range] = []
    for start, end in sorted(silences):
        start, end = max(0.0, start), min(duration, end)
        if end - start < min_silence:
            continue
        cut_start = 0.0 if start <= 0.0 else start + padding
        cut_end = duration if end >= duration else end - padding
        if cut_end > cut_start:
            cuts.append((cut_start, cut_end))
    keep: list[Range] = []
    cursor = 0.0
    for cut_start, cut_end in cuts:
        if cut_start > cursor:
            keep.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
    if cursor < duration:
        keep.append((cursor, duration))
    keep = [(a, b) for a, b in keep if b - a >= MIN_KEEP_SECONDS]
    return keep or whole


def edited_duration(keep: list[Range]) -> float:
    return sum(b - a for a, b in keep)


def count_cuts(keep: list[Range], duration: float) -> int:
    """How many stretches were removed: the gaps between kept ranges, plus a trimmed start or end."""
    return (len(keep) - 1) + (keep[0][0] > 0) + (keep[-1][1] < duration)


def remap_captions(captions: list[Caption], keep: list[Range]) -> list[Caption]:
    """Move captions from the source timeline onto the edited one. A caption that falls in a cut
    is dropped; one that straddles a cut is clipped to what survives (the kept pieces are adjacent
    after the cut, so it stays one caption)."""
    offsets, total = [], 0.0
    for a, b in keep:
        offsets.append(total)
        total += b - a
    out: list[Caption] = []
    for caption in captions:
        pieces = [(max(caption.start, a) - a + offset, min(caption.end, b) - a + offset)
                  for (a, b), offset in zip(keep, offsets) if min(caption.end, b) > max(caption.start, a)]
        if not pieces:
            continue
        start, end = pieces[0][0], pieces[-1][1]
        if end - start >= MIN_CAPTION_SECONDS:
            out.append(Caption(round(start, 3), round(end, 3), caption.text))
    return out


def split_caption(caption: Caption, max_chars: int = MAX_CAPTION_CHARS) -> list[Caption]:
    """Break one transcript segment into lines of at most `max_chars` (whole words), sharing its
    time in proportion to each line's length — a vertical frame fits one short line, not a paragraph."""
    words = caption.text.split()
    if not words:
        return []
    lines: list[str] = []
    for word in words:
        if lines and len(lines[-1]) + 1 + len(word) <= max_chars:
            lines[-1] += " " + word
        else:
            lines.append(word)
    span = caption.end - caption.start
    total = sum(len(line) for line in lines)
    out, cursor = [], caption.start
    for i, line in enumerate(lines):
        end = caption.end if i == len(lines) - 1 else cursor + span * len(line) / total
        out.append(Caption(round(cursor, 3), round(end, 3), line))
        cursor = end
    return out


def _srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(captions: list[Caption]) -> str:
    blocks = []
    for index, caption in enumerate(captions, start=1):
        text = " ".join(caption.text.split())  # a blank line inside a block would end it early
        blocks.append(f"{index}\n{_srt_time(caption.start)} --> {_srt_time(caption.end)}\n{text}\n")
    return "\n".join(blocks)


def render_filter(keep: list[Range], *, has_audio: bool, subtitles: str | None) -> str:
    """The filter graph that keeps exactly `keep` (trim/atrim + concat) and optionally burns in
    `subtitles` — a file name relative to ffmpeg's working directory, never a user string."""
    parts, inputs = [], []
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        inputs.append(f"[v{i}]")
        if has_audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
            inputs.append(f"[a{i}]")
    video_out = "[vcut]" if subtitles else "[vout]"
    audio = "1" if has_audio else "0"
    parts.append(f"{''.join(inputs)}concat=n={len(keep)}:v=1:a={audio}{video_out}{'[aout]' if has_audio else ''}")
    if subtitles:
        parts.append(f"[vcut]subtitles={subtitles}:force_style='{CAPTION_STYLE}'[vout]")
    return ";".join(parts)


def read_parameters(parameters: dict | None) -> tuple[str, bool, str | None]:
    """(pace, captions, language) from an operation's parameters; anything malformed is refused."""
    parameters = parameters or {}
    pace = parameters.get("pace", DEFAULT_PACE)
    captions = parameters.get("captions", True)
    language = parameters.get("language", "pt")
    if pace not in PACES or not isinstance(captions, bool):
        raise EngineFailed("invalid video_edit parameters", kind=FailureKind.REJECTED)
    if language is not None and (not isinstance(language, str) or not _LANGUAGE.match(language)):
        raise EngineFailed("invalid video_edit language", kind=FailureKind.REJECTED)
    return pace, captions, language


# ── Running ffmpeg ───────────────────────────────────────────────────────────

def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


async def _ffmpeg(args: list[str], *, cwd: Path, timeout: float = FFMPEG_TIMEOUT_S) -> tuple[int, bytes, str]:
    binary = ffmpeg_path()
    if binary is None:
        raise EngineUnavailable("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(binary, "-hide_banner", "-nostdin", *args, cwd=str(cwd),
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise EngineFailed("ffmpeg timed out", kind=FailureKind.TIMEOUT, retryable=False) from None
    return process.returncode, stdout, stderr.decode("utf-8", "replace")


def _default_transcriber() -> Transcriber | None:
    from src.voice.adapters.whisper_local import build_whisper_local_provider

    provider = build_whisper_local_provider()
    if provider is None:
        return None

    async def transcribe(audio: bytes, content_type: str, language: str | None):
        return await provider.transcribe(audio, content_type=content_type, language=language)

    return transcribe


class FfmpegVideoEditEngine:
    """The `OperationEngine` shape for `video_edit` (duck-typed, like VTracerEngine). Registered as
    route "video_edit@local" (media/registry.py) and off until MEDIA_OPERATION_ROUTES lists it.
    `transcriber` is injectable for tests; by default it is the local whisper.cpp server, and a
    request that asks for captions while that is not configured fails plainly rather than
    silently delivering a video without them."""

    operation = OPERATION

    def __init__(self, transcriber: Transcriber | None | object = ..., *, timeout_s: float = FFMPEG_TIMEOUT_S):
        self._transcriber = transcriber
        self._timeout = timeout_s

    async def is_available(self) -> bool:
        return ffmpeg_path() is not None

    def estimate_usd(self, request) -> float:
        return 0.0

    def _transcriber_or_none(self) -> Transcriber | None:
        return _default_transcriber() if self._transcriber is ... else self._transcriber  # type: ignore[return-value]

    async def run(self, request, resolved: dict) -> MediaOutput:
        pace, want_captions, language = read_parameters(getattr(request, "parameters", None))
        data, _content_type = resolved.get("source_video", (b"", ""))
        if not data:
            raise EngineFailed("empty source video", kind=FailureKind.REJECTED)
        transcriber = self._transcriber_or_none() if want_captions else None
        if want_captions and transcriber is None:
            raise EngineFailed("captions need local transcription (STT_LOCAL_ENABLED)", kind=FailureKind.UNAVAILABLE,
                               retryable=False)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="video_edit_") as tmp:
            work = Path(tmp)
            (work / "source").write_bytes(bytes(data))
            code, _, probe = await _ffmpeg(
                ["-i", "source", "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d=0.2", "-f", "null", "-"],
                cwd=work, timeout=self._timeout)
            duration, has_audio, silences = parse_probe(probe)
            if code != 0 or not duration:
                raise EngineFailed("source video could not be read", kind=FailureKind.UNSUPPORTED_FORMAT,
                                   retryable=False)
            keep = plan_cuts(duration, silences, pace) if has_audio else [(0.0, duration)]

            subtitles = None
            captions: list[Caption] = []
            if transcriber is not None and has_audio:
                captions = await self._captions(transcriber, work, language, keep)
                if captions:
                    (work / "captions.srt").write_text(to_srt(captions), encoding="utf-8")
                    subtitles = "captions.srt"

            code, _, log = await _ffmpeg(
                ["-i", "source", "-filter_complex", render_filter(keep, has_audio=has_audio, subtitles=subtitles),
                 "-map", "[vout]", *(["-map", "[aout]"] if has_audio else []),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                 *(["-c:a", "aac", "-b:a", "128k"] if has_audio else []),
                 "-movflags", "+faststart", "-y", "edited.mp4"],
                cwd=work, timeout=self._timeout)
            output = work / "edited.mp4"
            if code != 0 or not output.exists() or output.stat().st_size == 0:
                raise EngineFailed("ffmpeg could not render the edit", kind=FailureKind.REJECTED)
            rendered = output.read_bytes()

        from src.media.runners.base import video_metadata

        width, height, seconds = video_metadata(rendered)
        return MediaOutput(
            data=rendered, content_type="video/mp4", width=width, height=height,
            duration_seconds=seconds or round(edited_duration(keep), 3),
            engine_seconds=round(time.monotonic() - started, 3), cost_usd=0.0,
            metrics={"pace": pace, "source_seconds": round(duration, 3), "edited_seconds": round(edited_duration(keep), 3),
                     "cuts": count_cuts(keep, duration), "captions": len(captions)},
        )

    async def _captions(self, transcriber: Transcriber, work: Path, language: str | None,
                        keep: list[Range]) -> list[Caption]:
        # Mono 16 kHz Opus: whisper.cpp's own rate, ~10 MB per hour, under the server's upload cap.
        code, _, _ = await _ffmpeg(["-i", "source", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                                    "-b:a", "24k", "-y", "audio.ogg"], cwd=work, timeout=self._timeout)
        audio = work / "audio.ogg"
        if code != 0 or not audio.exists():
            raise EngineFailed("audio could not be extracted", kind=FailureKind.REJECTED)
        try:
            result = await transcriber(audio.read_bytes(), "audio/ogg", language)
        except Exception as exc:
            raise EngineFailed(f"transcription failed: {type(exc).__name__}", kind=FailureKind.TEMPORARY,
                               retryable=False) from None
        segments = getattr(result, "segments", None) or ()
        lines = [line for segment in segments
                 for line in split_caption(Caption(float(segment.start), float(segment.end), str(segment.text)))]
        return remap_captions(lines, keep)
