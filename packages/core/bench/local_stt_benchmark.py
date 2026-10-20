"""Local STT (whisper.cpp, stt.local) vs hosted STT on PT-BR speech. Not imported by src/.

    uv run --no-sync python bench/local_stt_benchmark.py <reference.json> <clip.wav> [<clip.wav> ...] [--local-only]

Both sides go through the Core's real adapters (WhisperLocalVoiceProvider, GroqVoiceProvider),
so this measures what the product would run, not a script's own client.

Reference: a transcript produced earlier by the configured default STT (ElevenLabs Scribe)
for the same recording — not human-verified. So a word error rate here is distance FROM THE
CURRENT PROVIDER, which is what a swap has to be judged against; it is not absolute accuracy.
The clip is a window of an edited cut, so the reference window is found by alignment.

Costs: hosted per the adapters' own price constants (voice/budget.py), by audio duration;
local priced at LOCAL_CPU_USD_PER_HOUR (an assumption: a 4 vCPU / 16 GB on-demand VM).
Hosted calls are capped at HOSTED_CAP_USD; a missing credential reports BLOCKED.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOCAL_BASE = "http://127.0.0.1:8282"
LOCAL_MODEL = "ggml-small-q5_1 (190 MB)"
LOCAL_CPU_USD_PER_HOUR = 0.17
HOSTED_CAP_USD = 0.01
RESULTS = Path(__file__).resolve().parent / "results" / "local_stt_benchmark.json"


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", text)


def edit_distance(a: list[str], b: list[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def wer_against_best_window(hypothesis: list[str], reference: list[str]) -> tuple[float, int]:
    """WER of `hypothesis` against the reference window it matches best (clips are cut from a longer take)."""
    n = len(hypothesis)
    best, best_start = 1.0, 0
    for length in {max(1, int(n * f)) for f in (0.85, 0.95, 1.0, 1.05, 1.15)}:
        for start in range(0, max(1, len(reference) - length + 1), 2):
            window = reference[start:start + length]
            score = edit_distance(window, hypothesis) / max(1, len(window))
            if score < best:
                best, best_start = score, start
    return best, best_start


async def transcribe(provider, audio: bytes) -> tuple[str, str | None, float]:
    started = time.monotonic()
    result = await provider.transcribe(audio, content_type="audio/wav", language="pt")
    return result.text, result.language, time.monotonic() - started


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reference_path, clips = Path(args[0]), [Path(p) for p in args[1:]]
    reference = words(json.loads(reference_path.read_text(encoding="utf-8")).get("text", ""))

    from src.config import settings
    from src.voice.adapters.groq import GroqVoiceProvider
    from src.voice.adapters.whisper_local import WhisperLocalVoiceProvider
    from src.voice.budget import GROQ_STT_MINIMUM_SECONDS, GROQ_STT_USD_PER_HOUR

    def groq_cost(audio_seconds: float) -> float:
        # Billed by audio duration with a per-request minimum (voice/budget.py). The budget's own
        # stt_cost_bound_usd is a worst case that assumes compressed audio — right for a reservation,
        # far too high for a WAV's real price.
        return max(GROQ_STT_MINIMUM_SECONDS, audio_seconds) / 3600 * GROQ_STT_USD_PER_HOUR

    runners = [("local", WhisperLocalVoiceProvider(base_url=LOCAL_BASE, max_bytes=20 * 1024 * 1024))]
    if "--local-only" not in sys.argv:
        runners.append(("hosted-groq", GroqVoiceProvider(api_key=settings.groq_api_key) if settings.groq_api_key
                        else None))
    rows, hosted_spent = [], 0.0
    for clip in clips:
        audio = clip.read_bytes()
        seconds_audio = (len(audio) - 44) / (16000 * 2)
        for name, provider in runners:
            row = {"clip": clip.name, "audio_s": round(seconds_audio, 1), "runner": name}
            if provider is None:
                rows.append({**row, "error": "BLOCKED: no credential"})
                continue
            if name != "local":
                if hosted_spent + groq_cost(seconds_audio) > HOSTED_CAP_USD:
                    rows.append({**row, "error": "SKIPPED: cost cap"})
                    continue
            try:
                text, language, elapsed = await transcribe(provider, audio)
            except Exception as exc:
                rows.append({**row, "error": type(exc).__name__})
                continue
            wer, _ = wer_against_best_window(words(text), reference)
            if name == "local":
                cost = elapsed / 3600 * LOCAL_CPU_USD_PER_HOUR
            else:
                cost = groq_cost(seconds_audio)
                hosted_spent += cost
            rows.append({**row, "language": language, "seconds": round(elapsed, 2),
                         "real_time_factor": round(elapsed / seconds_audio, 2), "wer_vs_current_provider": round(wer, 3),
                         "cost_usd": round(cost, 7), "cost_usd_per_minute": round(cost / seconds_audio * 60, 6),
                         "text": text[:200]})
    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "local_model": LOCAL_MODEL,
              "reference": "ElevenLabs Scribe transcript of the same recording (not human-verified)",
              "local_cpu_usd_per_hour_assumption": LOCAL_CPU_USD_PER_HOUR, "hosted_spent_usd": round(hosted_spent, 7),
              "rows": rows}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        if "error" in r:
            print(f"{r['clip']:24} {r['runner']:12} {r['error']}")
        else:
            print(f"{r['clip']:24} {r['runner']:12} WER={r['wer_vs_current_provider']:.1%} t={r['seconds']:.1f}s "
                  f"RTF={r['real_time_factor']:.2f} lang={r['language']} ${r['cost_usd_per_minute']:.5f}/min")
    print("hosted spent: $%.6f (cap $%.2f)" % (hosted_spent, HOSTED_CAP_USD))


if __name__ == "__main__":
    asyncio.run(main())
