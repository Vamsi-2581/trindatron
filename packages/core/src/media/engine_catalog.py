"""Engine catalog: what was researched, whether its licence allows TRINDATRON to use it, and why. Internal only.

Status is layered, and each layer is decided separately:

  RESEARCHED  a record exists here (sources read, facts or unknowns written down)
  APPROVED    the licence allows the intended commercial use (`license_decision`
              "allowed", or "conditional" with the condition confirmed in
              MEDIA_ACCEPTED_LICENSE_CONDITIONS) — independent of cost
  REJECTED    the licence forbids it or is unclear enough that it must not run
  DISABLED    approved, but switched off by the operator (not in any route list)
  INTEGRATED  an adapter exists (media/adapters.py) or a hosted route (media/registry.py)
  AVAILABLE   a configured route for it answers is_available() now
  HEALTHY     that route is not cooling down (media/health.py)

The last three are computed at run time (`status_report`). A record here is
never an integration, and open weights are never assumed free to run: GPU
time, storage, bandwidth and operation are priced by the runner that executes
them (media/runners/).

Sources read on 2026-09-14 and 2026-09-15 (official model cards, licence files,
repositories and pricing pages). A value that could not be confirmed from an
official source is None and noted in `limitations`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ALLOWED, CONDITIONAL, FORBIDDEN = "allowed", "conditional", "forbidden"


@dataclass(frozen=True)
class EngineRecord:
    name: str
    capabilities: tuple[str, ...]
    license: str
    commercial_use: str  # "yes" | "conditional" | "no"
    status: str  # researched verdict: "approved" | "conditional" | "rejected" | "researched"
    deployment: str  # "hosted_api" | "self_hosted" | "both"
    self_hostable: bool
    api_available: bool
    engine_id: str | None = None  # the id routes use; None when nothing can run it
    license_decision: str = ALLOWED
    license_condition: str | None = None  # the condition id an operator must confirm for CONDITIONAL
    architecture: str | None = None
    model_size: str | None = None
    minimum_vram_gb: float | None = None
    recommended_vram_gb: float | None = None
    speed: str | None = None
    quality: str | None = None
    estimated_cost: str | None = None
    limitations: tuple[str, ...] = field(default_factory=tuple)


LTX_CONDITION = "ltx_revenue_below_10m"
# Higgsfield's API terms were not reviewed for commercial resale of outputs: an operator confirms them first.
HIGGSFIELD_CONDITION = "higgsfield_api_terms"
_HIGGSFIELD_NOTE = "hosted on Higgsfield (credits, ~$0.0625 each); prices from open.higgsfield.ai, 2026-09-18"

CATALOG: tuple[EngineRecord, ...] = (
    # ── Image generation / editing ──────────────────────────────────────────
    EngineRecord("gpt-image-1-mini", ("image_generation",), "proprietary API terms", "yes", "approved",
                 "hosted_api", False, True, engine_id="gpt_image_1_mini",
                 estimated_cost="~$0.002-0.004 per 1024 image (low quality), token-billed",
                 speed="~8 s measured", quality="good",
                 limitations=("three output sizes only (1024x1024, 1536x1024, 1024x1536)", "editing not wired")),
    EngineRecord("grok-imagine-image", ("image_generation", "image_editing"), "proprietary API terms", "yes",
                 "approved", "hosted_api", False, True, engine_id="grok_imagine_image",
                 estimated_cost="$0.02 per image (docs.x.ai)", speed="7.5 s generate, 9.8 s edit measured",
                 quality="very good (validated)", limitations=("15 aspect ratios, 1k/2k resolution",)),
    EngineRecord("Z-Image-Turbo", ("image_generation",), "Apache-2.0", "yes", "approved", "both", True, True,
                 engine_id="z_image_turbo", architecture="single-stream diffusion transformer", model_size="6B",
                 minimum_vram_gb=16.0, speed="8 steps; sub-second on H800 (model card)",
                 estimated_cost="hosted ~$0.005/MP; ZeroGPU inside quota $0",
                 limitations=("no editing",)),
    EngineRecord("FLUX.1-schnell", ("image_generation",), "Apache-2.0", "yes", "approved", "hosted_api", False, True,
                 engine_id="flux1_schnell", architecture="12B rectified-flow transformer", model_size="12B",
                 speed="few-step (1-4); no wall-clock published by Black Forest Labs",
                 estimated_cost="hosted (fal.ai) $0.003/MP, 2026-09-20; BFL's own API does not sell this model",
                 quality="good (BFL's own claim; no numeric benchmark published for schnell specifically)",
                 limitations=("no source-image editing", "additional option alongside Z-Image-Turbo/Qwen-Image, "
                              "not a replacement", "off until MEDIA_FAL_FLUX_SCHNELL_ENDPOINT is set")),
    EngineRecord("Qwen-Image", ("image_generation",), "Apache-2.0", "yes", "approved", "both", True, True,
                 engine_id="qwen_image", architecture="MMDiT", model_size="20B",
                 limitations=("heavy: 50 steps by default; VRAM not stated on the model card",)),
    EngineRecord("Qwen-Image-Edit-2509", ("image_editing",), "Apache-2.0", "yes", "approved", "self_hosted", True, False,
                 engine_id=None, architecture="MMDiT", model_size="20B",
                 limitations=("no adapter yet", "VRAM not stated on the model card")),
    EngineRecord("FLUX.1 [dev] / Kontext [dev]", ("image_generation", "image_editing"),
                 "FLUX.1 [dev] Non-Commercial License", "no", "rejected", "self_hosted", True, False,
                 license_decision=FORBIDDEN, model_size="12B",
                 limitations=("commercial use requires a separate licence from Black Forest Labs",)),
    EngineRecord("FLUX.2 [klein] 4B", ("image_generation",), "Apache-2.0 (secondary source only)", "conditional",
                 "researched", "self_hosted", True, False, license_decision=FORBIDDEN, model_size="4B",
                 limitations=("licence not yet confirmed on the official model card: not selectable until it is",)),
    # ── Vectorization (deterministic, not generative — CP18) ────────────────
    EngineRecord("VTracer", ("vectorization",), "MIT", "yes", "approved", "self_hosted", True, True,
                 engine_id="vtracer", architecture="classical (hierarchical clustering + curve fitting), not a model",
                 estimated_cost="$0 always (no GPU, no network, deterministic algorithm)",
                 limitations=("raster to SVG only; no model, so not part of the quality-floor GPU/hosted ranking",)),
    # ── Video editing: cuts, pacing, captions (deterministic, not generative) ─
    EngineRecord("FFmpeg video edit", ("video_editing",), "LGPL-2.1+ / GPL-2.0+ (external binary, run as a process)",
                 "yes", "approved", "self_hosted", True, False, engine_id="ffmpeg_video_edit",
                 architecture="classical (silencedetect + trim/concat + libass subtitles), not a model",
                 estimated_cost="$0 always (CPU on this machine; captions from local whisper.cpp, also $0)",
                 limitations=("cuts are pauses in the audio only; no scene or visual analysis",
                              "captions need STT_LOCAL_ENABLED (whisper.cpp server); without it, ask with "
                              "captions=false",
                              "off until MEDIA_OPERATION_ROUTES lists video_edit@local")),
    # ── Background removal — first TRANSFORM operation (CP19, schema verified CP20) ─
    EngineRecord("BiRefNet", ("background_remove",), "MIT", "yes", "conditional", "self_hosted", True, True,
                 engine_id="birefnet", architecture="bilateral reference segmentation network (BiRefNet)",
                 model_size="~220M", minimum_vram_gb=None,
                 estimated_cost="ZeroGPU inside quota $0 (same convention as Z-Image-Turbo/Qwen-Image)",
                 quality="reported SOTA on DIS/HRSOD/UHRSD benchmarks (project's own README, not independently verified)",
                 limitations=("off by default: MEDIA_ZEROGPU_BIREFNET_SPACE is unset even though its schema is now "
                              "verified (CP20, live-read against ZhengPeng7/BiRefNet_demo's /gradio_api/info and "
                              "app.py source: api_name='image', args=[image, resolution, weights], output is a "
                              "2-item ImageSlider pair — media/runners/zerogpu.py _birefnet/_imageslider_result); "
                              "no real generation has been run yet, so activation is a separate, later decision",
                              "not commercial infrastructure (ZeroGPU): never sizes a plan's promise, "
                              "never used in production")),
    # ── Character animation — first multi-input TRANSFORM operation (CP22) ───
    EngineRecord("Wan2.2-Animate-14B", ("character_animate",), "Apache-2.0", "yes", "conditional", "self_hosted",
                 True, True, engine_id="wan22_animate", architecture="DiT, built on Wan2.2-I2V-A14B",
                 model_size="14B", minimum_vram_gb=None,
                 estimated_cost="ZeroGPU inside quota $0 (real, measured — see CP25's LAB run)",
                 quality="not independently measured; paper claims SOTA on holistic-replication benchmarks",
                 limitations=("off by default: MEDIA_ZEROGPU_WAN22_ANIMATE_SPACE is unset even though its "
                              "schema is now verified end-to-end (CP23 source-code audit: the Space vendors "
                              "the official Wan-Video/Wan2.2 code and loads the real Wan-AI/Wan2.2-Animate-14B "
                              "checkpoint, not a fork/mismatch; CP24 live /gradio_api/info verification: "
                              "api_name='animate_scene', 5 real params, output index 0 = final video, "
                              "confirmed by two independent primary sources; CP25: one real LAB generation "
                              "run end to end through the full TRINDATRON path). It is not an official Wan-AI "
                              "Space (a community remix, alexnasa/Wan2.2-Animate-ZEROGPU, 335+ likes, actively "
                              "maintained) — activation beyond LAB is a separate, later decision. No audio "
                              "input (confirmed from official generate.py/preprocess_data.py source, not this "
                              "community Space)",
                              "not commercial infrastructure (ZeroGPU): never sizes a plan's promise, "
                              "never used in production")),
    # ── Vision ──────────────────────────────────────────────────────────────
    EngineRecord("gemini-3.1-flash-lite (vision)", ("vision",), "proprietary API terms", "yes", "approved",
                 "hosted_api", False, True, engine_id="gemini_vision",
                 estimated_cost="~$0.0006 per image read (measured)", speed="~5 s measured"),
    # ── Video / image-to-video ──────────────────────────────────────────────
    EngineRecord("Wan2.2-TI2V-5B", ("video_generation", "image_to_video"), "Apache-2.0", "yes", "approved",
                 "both", True, True, engine_id="wan22_ti2v_5b", architecture="DiT with high-compression VAE",
                 model_size="5B", minimum_vram_gb=24.0, recommended_vram_gb=80.0,
                 speed="5 s of 720p24 in under 9 min on one RTX 4090 (model card); distilled 4-step variants much faster",
                 estimated_cost="GPU seconds x runner price; ZeroGPU inside quota $0",
                 limitations=("1280x704 / 704x1280 native, 24 fps, up to 5 s",
                              "the public ZeroGPU Space runs the FastWan distilled checkpoint (third-party Space)")),
    EngineRecord("Wan2.2-I2V-A14B", ("image_to_video",), "Apache-2.0", "yes", "approved", "both", True, True,
                 engine_id="wan22_i2v_a14b", model_size="14B (MoE, two experts)", minimum_vram_gb=48.0,
                 speed="50.5 s for 3.56 s at 560x832 measured on ZeroGPU (FP8 + AoT, 6 steps), 2026-09-15",
                 quality="very good (validated visually)",
                 limitations=("image-to-video only", "the ZeroGPU Space uses a Lightning distillation LoRA whose "
                              "licence was not verified")),
    EngineRecord("Wan2.2-T2V-A14B + Lightning (Modal H200)", ("video_generation",),
                 "Apache-2.0 (weights; lightx2v Wan2.2-Lightning LoRA also Apache-2.0)", "yes", "approved",
                 "self_hosted", True, False, engine_id="wan22_t2v_a14b", model_size="14B (MoE, two experts)",
                 minimum_vram_gb=80.0, recommended_vram_gb=141.0, architecture="DiT (MoE) + 4-step Lightning distillation",
                 speed="56.8 s warm for 5 s of 1280x720 on H200, no offload (2026-09-25)",
                 estimated_cost="~US$0.072 per 5 s clip at H200 US$0.001261/s (warm), before the reference markup",
                 limitations=("text-to-video only", "Maximum mode only (media/modes.py)",
                              "off until MEDIA_MODAL_ENGINES lists wan22_t2v_a14b and the worker is deployed")),
    EngineRecord("Wan2.2-I2V-A14B + Lightning (Modal H200)", ("image_to_video",),
                 "Apache-2.0 (weights; lightx2v Wan2.2-Lightning I2V LoRA also Apache-2.0)", "yes", "approved",
                 "self_hosted", True, False, engine_id="wan22_i2v_a14b_lightning", model_size="14B (MoE, two experts)",
                 minimum_vram_gb=80.0, recommended_vram_gb=141.0, architecture="DiT (MoE) + 4-step Lightning distillation",
                 estimated_cost="about the T2V sibling's ~US$0.07 per 5 s at 720p on H200 until measured",
                 limitations=("image-to-video only: the photo is the first frame", "Maximum mode only (media/modes.py)",
                              "off until MEDIA_MODAL_ENGINES lists wan22_i2v_a14b_lightning and MEDIA_MODAL_TASKS "
                              "includes image_to_video")),
    EngineRecord("Wan2.2-A14B on fal", ("video_generation",), "Apache-2.0 (weights) + fal API terms", "yes",
                 "approved", "hosted_api", False, True, engine_id="wan22_a14b",
                 estimated_cost="$0.04/s 480p, $0.08/s 720p (fal.ai)", limitations=("no fal key configured",)),
    EngineRecord("Wan 2.2 Animate Move on fal", ("character_animate",), "Apache-2.0 (weights) + fal API terms",
                 "yes", "approved", "hosted_api", False, True, engine_id="wan_animate_move",
                 estimated_cost="$0.04/480p, $0.06/580p, $0.08/720p per BILLED video-second "
                                "(= real_frames / 16, not simple output duration — CP46, "
                                "fal.ai/models/fal-ai/wan/v2.2-14b/animate/move, read 2026-09-22)",
                 limitations=("no documented raw REST file-upload endpoint — inputs sent as base64 data: URIs "
                              "(fal's own documented fallback), fine for a photo + a short clip but not for large "
                              "files; upgrade to a real CDN upload if/when input sizes grow",
                              "fal's billing-on-failure policy for this model specifically was not found "
                              "documented — treated conservatively as possibly billable (no automatic fallback "
                              "to another provider after any post-submit failure)")),
    EngineRecord("grok-imagine-video", ("video_generation", "image_to_video"), "proprietary API terms", "yes",
                 "approved", "hosted_api", False, True, engine_id="grok_imagine_video",
                 estimated_cost="$0.05/s at 480p (measured $0.10 for 2 s); 720p priced at $0.07/s until confirmed",
                 speed="~20 s for a 2 s clip measured", quality="good (validated)",
                 limitations=("1-15 s", "7 aspect ratios", "480p/720p/1080p")),
    EngineRecord("veo-3.1-lite-generate-preview", ("video_generation",), "proprietary API terms", "yes", "approved",
                 "hosted_api", False, True, engine_id="veo_3_1_lite",
                 estimated_cost="$0.05/s 720p, $0.08/s 1080p (ai.google.dev)",
                 limitations=("optional paid fallback only, never required",
                              "project quota/billing blocked (HTTP 429 RESOURCE_EXHAUSTED) as of 2026-09-14",
                              "4/6/8 s only")),
    EngineRecord("LTX-Video 0.9.8 13B distilled", ("video_generation", "image_to_video"),
                 "LTX-Video Open Weights License", "conditional", "conditional", "self_hosted", True, False,
                 engine_id="ltx_video_13b_distilled", license_decision=CONDITIONAL, license_condition=LTX_CONDITION,
                 model_size="13B", speed="distilled: faster than real time on large GPUs (model card)",
                 limitations=("entities with annual revenue of at least US$10M need a paid commercial licence",
                              "use restrictions in Attachment A of the licence")),
    EngineRecord("LTX-2 / LTX-2.3", ("video_generation", "image_to_video", "audio_generation"),
                 "LTX-2 Community License", "conditional", "conditional", "self_hosted", True, False,
                 engine_id=None, license_decision=CONDITIONAL, license_condition=LTX_CONDITION, model_size="19B",
                 limitations=("free commercial use only below US$10M annual revenue", "no adapter yet")),
    EngineRecord("MAGI-1", ("video_generation", "image_to_video"), "Apache-2.0", "yes", "approved", "self_hosted",
                 True, False, engine_id=None, limitations=("autoregressive; very large; no adapter yet",)),
    EngineRecord("LongCat-Video", ("video_generation", "image_to_video"), "MIT", "yes", "approved", "self_hosted",
                 True, False, engine_id=None, model_size="13.6B", limitations=("no adapter yet",)),
    EngineRecord("Kandinsky 5.0 Video Lite", ("video_generation", "image_to_video"), "MIT", "yes", "approved",
                 "self_hosted", True, False, engine_id=None, model_size="2B",
                 limitations=("no adapter yet",)),
    EngineRecord("Mochi 1", ("video_generation",), "Apache-2.0", "yes", "approved", "self_hosted", True, False,
                 engine_id=None, limitations=("high VRAM", "no adapter yet")),
    EngineRecord("HunyuanVideo-1.5", ("video_generation", "image_to_video"), "Tencent Hunyuan Community License",
                 "conditional", "rejected", "self_hosted", True, False, license_decision=FORBIDDEN,
                 model_size="8.3B", minimum_vram_gb=14.0,
                 limitations=("licence territory excludes the European Union, United Kingdom and South Korea",)),
    EngineRecord("MiniMax H3", ("video_generation",), "MiniMax H3 Community License", "no", "rejected",
                 "self_hosted", True, False, license_decision=FORBIDDEN,
                 limitations=("licence excludes the United States, European Union, United Kingdom and South Korea",)),
    EngineRecord("SkyReels-V2", ("video_generation", "image_to_video"), "Skywork community licence", "conditional",
                 "rejected", "self_hosted", True, False, license_decision=FORBIDDEN,
                 limitations=("custom licence not reviewed: not selectable",)),
    EngineRecord("Sora 2 (API)", ("video_generation",), "proprietary API terms", "yes", "rejected", "hosted_api",
                 False, True, license_decision=FORBIDDEN, estimated_cost="$0.10/s 720p",
                 limitations=("API scheduled for removal on 2026-09-24 with no successor announced",)),
    # ── Higgsfield (one hosted provider, many models) ───────────────────────
    EngineRecord("Soul 2 (Higgsfield)", ("image_generation",), "Higgsfield API terms", "conditional",
                 "conditional", "hosted_api", False, True, engine_id="higgsfield_soul_2",
                 license_decision=CONDITIONAL, license_condition=HIGGSFIELD_CONDITION,
                 estimated_cost="$0.0032 720p / $0.0057 1080p per image",
                 limitations=(_HIGGSFIELD_NOTE, "only the 4:3 aspect ratio is verified", "no editing",
                              "not yet validated with a real generation")),
    EngineRecord("Image editing (Higgsfield)", ("image_editing",), "Higgsfield API terms", "conditional",
                 "researched", "hosted_api", False, True, engine_id="higgsfield_image_edit",
                 license_decision=CONDITIONAL, license_condition=HIGGSFIELD_CONDITION,
                 limitations=("no Higgsfield model with a documented source-image field was found "
                              "(Grok Imagine 2.0 is text-to-image only); off until one is configured",)),
    EngineRecord("Wan 3.0 (Higgsfield)", ("video_generation", "image_to_video"), "Higgsfield API terms",
                 "conditional", "conditional", "hosted_api", False, True, engine_id="higgsfield_wan_3",
                 license_decision=CONDITIONAL, license_condition=HIGGSFIELD_CONDITION,
                 estimated_cost="$0.05/s 480p, $0.10/s 720p (model page; catalog shows $0.03, taken the higher)",
                 limitations=(_HIGGSFIELD_NOTE, "2-30 s", "only aspect_ratio 'adaptive' documented: off until "
                              "ratios are verified", "image-to-video field undocumented")),
    EngineRecord("Kling 3.0 Standard (Higgsfield)", ("video_generation", "image_to_video"), "Higgsfield API terms",
                 "conditional", "conditional", "hosted_api", False, True, engine_id="higgsfield_kling_3_std",
                 license_decision=CONDITIONAL, license_condition=HIGGSFIELD_CONDITION,
                 estimated_cost="$0.084/s list ($0.042/s promotional)",
                 limitations=(_HIGGSFIELD_NOTE, "3-15 s", "only 16:9 verified", "image-to-video field undocumented")),
    EngineRecord("Grok Imagine Video 1.5 (Higgsfield)", ("video_generation", "image_to_video"),
                 "Higgsfield API terms", "conditional", "conditional", "hosted_api", False, True,
                 engine_id="higgsfield_grok_imagine_video_1_5", license_decision=CONDITIONAL,
                 license_condition=HIGGSFIELD_CONDITION, estimated_cost="$0.08/s 480p, $0.14/s 720p",
                 limitations=(_HIGGSFIELD_NOTE, "1-15 s", "reference-to-video endpoint: only aspect_ratio 'auto' "
                              "documented, reference image field undocumented; off until verified")),
    EngineRecord("Genjutsu Motion Transfer (Higgsfield)", ("character_animate",), "Higgsfield API terms",
                 "conditional", "conditional", "hosted_api", False, True,
                 engine_id="genjutsu_motion_transfer", license_decision=CONDITIONAL,
                 license_condition=HIGGSFIELD_CONDITION,
                 estimated_cost="$0.318/s 480p, $0.681/s 720p list (open.higgsfield.ai, read 2026-09-21)",
                 limitations=(_HIGGSFIELD_NOTE, "1-30 s", "model provenance disputed: catalog labels it "
                              "'Higgsfield Exclusive', no official documentation ties it to Wan2.2-Animate",
                              "commercial output terms not found in public docs (CP34)")),
    # ── Operator-provided ───────────────────────────────────────────────────
    EngineRecord("ComfyUI workflow (operator-provided)", ("image_generation", "image_editing", "video_generation",
                                                          "image_to_video"),
                 "per workflow", "conditional", "approved", "self_hosted", True, False, engine_id="comfyui_workflow",
                 limitations=("the operator is responsible for the licence of the models in each workflow",)),
    EngineRecord("development placeholder image", ("image_generation",), "internal", "yes", "approved",
                 "self_hosted", True, False, engine_id="dev", limitations=("refused in production",)),
    # ── Speech-to-text ──────────────────────────────────────────────────────
    EngineRecord("whisper-large-v3-turbo (hosted)", ("transcription", "audio_input"), "MIT (weights)", "yes",
                 "approved", "both", True, True, engine_id="whisper_large_v3_turbo",
                 estimated_cost="$0.04 per hour, 10 s minimum (Groq)",
                 limitations=("batch only (no streaming) on this endpoint",)),
    EngineRecord("ElevenLabs Scribe", ("transcription", "audio_input"), "proprietary API terms", "yes", "approved",
                 "hosted_api", False, True, engine_id="elevenlabs_scribe", estimated_cost="$0.22 per hour"),
    # ── Text-to-speech ──────────────────────────────────────────────────────
    EngineRecord("gemini TTS", ("audio_output",), "proprietary API terms", "yes", "approved", "hosted_api", False,
                 True, engine_id="gemini_tts",
                 estimated_cost="$0.50 input / $10 audio output per 1M tokens (2.5 flash preview)",
                 limitations=("quota errors observed 2026-09-10; ElevenLabs is the fallback",)),
    EngineRecord("ElevenLabs multilingual v2", ("audio_output",), "proprietary API terms", "yes", "approved",
                 "hosted_api", False, True, engine_id="elevenlabs_tts", estimated_cost="$0.10 per 1k characters",
                 quality="natural PT-BR"),
    EngineRecord("Kokoro-82M", ("audio_output",), "Apache-2.0", "yes", "approved", "self_hosted", True, False,
                 model_size="82M", estimated_cost="under $1 per 1M characters served (model card)",
                 limitations=("Brazilian Portuguese voices (pf_dora, pm_alex, pm_santa) carry no quality grade",
                              "CPU-capable; not yet deployed")),
    EngineRecord("Chatterbox Multilingual", ("audio_output",), "MIT", "yes", "approved", "self_hosted", True, False,
                 model_size="500M", limitations=("every output carries an imperceptible watermark",
                                                 "streaming not documented", "not yet deployed")),
    EngineRecord("F5-TTS", ("audio_output",), "CC-BY-NC-4.0 (weights)", "no", "rejected", "self_hosted", True, False,
                 license_decision=FORBIDDEN, limitations=("non-commercial weights",)),
    EngineRecord("XTTS-v2", ("audio_output",), "Coqui Public Model License", "no", "rejected", "self_hosted", True,
                 False, license_decision=FORBIDDEN, limitations=("non-commercial model licence",)),
)


def by_status(status: str) -> tuple[EngineRecord, ...]:
    return tuple(record for record in CATALOG if record.status == status)


def for_capability(capability: str) -> tuple[EngineRecord, ...]:
    return tuple(record for record in CATALOG if capability in record.capabilities)


def record_for(engine_id: str) -> EngineRecord | None:
    return next((record for record in CATALOG if record.engine_id == engine_id), None)


def _accepted_conditions() -> set[str]:
    from src.media.settings import media_settings

    return {part.strip() for part in (media_settings().media_accepted_license_conditions or "").split(",") if part.strip()}


def license_permits(engine_id: str) -> bool:
    """Whether TRINDATRON may run this engine commercially now. An engine with no record may not."""
    record = record_for(engine_id)
    if record is None:
        return False
    if record.license_decision == ALLOWED:
        return True
    if record.license_decision == CONDITIONAL:
        return bool(record.license_condition) and record.license_condition in _accepted_conditions()
    return False


async def status_report(routes: list) -> list[dict]:
    """Every researched media engine with its layered status against the configured routes. Internal only."""
    from src.media.adapters import IMAGE_ADAPTERS, VIDEO_ADAPTERS
    from src.media.health import health, runner_scope

    media_capabilities = {"image_generation", "image_editing", "video_generation", "image_to_video"}
    rows = []
    for record in CATALOG:
        if not media_capabilities.intersection(record.capabilities):
            continue
        mine = [route for route in routes if getattr(route, "engine_id", None) == record.engine_id and record.engine_id]
        available, healthy = [], []
        for route in mine:
            try:
                ok = await (route.is_available(from_image=False) if hasattr(route, "normalize")
                            else route.is_available())
            except Exception:
                ok = False
            if ok:
                available.append(route.label)
            if not health.is_cooling(route.label) and not health.is_cooling(runner_scope(route.runner_name)):
                healthy.append(route.label)
        integrated = bool(record.engine_id) and (record.engine_id in VIDEO_ADAPTERS or record.engine_id in IMAGE_ADAPTERS
                                                 or record.api_available or record.engine_id in ("comfyui_workflow", "dev"))
        approved = bool(record.engine_id) and license_permits(record.engine_id)
        rows.append({
            "engine": record.name, "engine_id": record.engine_id, "researched": True,
            "approved": approved, "rejected": record.license_decision == FORBIDDEN,
            "integrated": integrated, "disabled": approved and integrated and not mine,
            "routes": [route.label for route in mine], "available": available, "healthy": healthy,
        })
    return rows
