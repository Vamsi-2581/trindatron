"""Media configuration, read from the environment.

Kept in its own settings object rather than src/config.py so the media
capability can be configured, reviewed and tested on its own. Every value has a
safe default: with nothing set, no engine is configured and every media
capability reports itself unavailable — an honest "not here", never a fake
result.

Prices are configuration, not code. Each default below is the provider's
published price on the date noted (docs/MEDIA_ENGINES.md §7 has the sources);
when a price changes, the setting changes and every estimate, reservation and
plan limit follows.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MediaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Which engine serves each operation. "" = not configured (capability off).
    #   image:  "fal" (open-weight Z-Image Turbo, hosted) | "openai" (hosted) |
    #           "comfyui" (self-hosted) | "dev" (refused when ENVIRONMENT=production)
    #   video:  "fal" (open-weight Wan 2.2, hosted) | "veo" (hosted) | "comfyui"
    #   vision: "gemini" (hosted)
    media_image_engine: str = ""
    media_video_engine: str = ""
    media_vision_engine: str = ""

    # ── Engine × runner routes (media/routes.py) ────────────────────────────
    # Ordered candidates as "engine@runner", e.g.
    #   MEDIA_VIDEO_ROUTES="wan22_ti2v_5b@runpod,wan22_ti2v_5b@modal,ltx_video_13b_distilled@modal,grok_imagine_video@xai"
    # When set they replace MEDIA_VIDEO_ENGINE / MEDIA_IMAGE_ENGINE. The orchestrator ranks them per
    # request (quality floor, cost, health); the order only breaks ties.
    media_video_routes: str = ""
    media_image_routes: str = ""
    # CP19: TRANSFORM/COMPOSE routes ("operation@runner", e.g. "birefnet@zerogpu"). "" = the
    # operation has no engine and is refused as media_unavailable (same convention as above).
    media_operation_routes: str = ""
    # Free/shared infrastructure (ZeroGPU) may be used outside production for prototypes. Always refused in production.
    media_allow_experimental_runners: bool = True
    # Licence conditions the operator has confirmed are met (engine_catalog.py), e.g. "ltx_revenue_below_10m".
    media_accepted_license_conditions: str = ""
    # Outside production only: FREE may try video on experimental runners, this many a day. 0 = never.
    media_free_experimental_video_daily_limit: int = 0

    # ── Runner: Hugging Face ZeroGPU (experimental) ─────────────────────────
    hf_token: str = ""
    media_zerogpu_wan22_i2v_space: str = "zerogpu-aoti/wan2-2-fp8da-aoti-faster"
    media_zerogpu_wan22_ti2v_space: str = "KingNish/wan2-2-fast"
    media_zerogpu_ltx_video_space: str = "Lightricks/ltx-video-distilled"
    media_zerogpu_z_image_space: str = "Tongyi-MAI/Z-Image-Turbo"
    media_zerogpu_qwen_image_space: str = "Qwen/Qwen-Image"
    # CP19: "" = off. CP20 verified the schema for ZhengPeng7/BiRefNet_demo (the official
    # author Space) against its own /gradio_api/info and app.py source — api_name="image",
    # [image, resolution, weights], 2-item ImageSlider output (media/runners/zerogpu.py
    # _birefnet/_imageslider_result). Still "" = off deliberately: no real generation has been
    # run against it yet, and this value is not pre-filled the way the other Spaces above are —
    # activation is a separate decision. Set to "ZhengPeng7/BiRefNet_demo" only after that
    # decision. License: MIT (github.com/ZhengPeng7/BiRefNet).
    media_zerogpu_birefnet_space: str = ""
    # CP22: "" = off (no official Wan-AI Space exists — the linked one 404s). CP23/CP24 audited
    # and verified the one real, live community candidate, alexnasa/Wan2.2-Animate-ZEROGPU:
    # source-code confirmed as a vendored copy of the official Wan-Video/Wan2.2 inference code
    # loading the real Wan-AI/Wan2.2-Animate-14B checkpoint; live /gradio_api/info confirmed
    # api_name="animate_scene", 5 real parameters, output index 0 = the final video (media/
    # runners/zerogpu.py _animate). Still "" = off deliberately — activation for a real LAB run
    # is its own separate decision. Set to "alexnasa/Wan2.2-Animate-ZEROGPU" only after that
    # decision. License: Apache-2.0 (Space's own LICENSE.txt, independently confirmed).
    media_zerogpu_wan22_animate_space: str = ""
    media_zerogpu_timeout_s: int = 600

    # ── Runners: serverless GPU workers (media/runners/gpu_worker.py) ───────
    # Engines each endpoint has deployed, comma-separated engine ids (media/adapters.py).
    media_modal_url: str = ""
    media_modal_token: str = ""
    media_modal_engines: str = ""
    # Tasks the deployed worker serves ("text_to_video"); empty = every task its engines have.
    # The Wan 2.2 5B distilled worker is text-to-video only (its image-to-video ignored the image).
    media_modal_tasks: str = ""
    media_modal_gpu_usd_per_second: float = 0.001097  # H100 (infra/modal/video_worker.py), modal.com/pricing 2026-09-25
    # Engines that run on another GPU class in the same worker, "engine=usd_per_second,...". The A14B
    # (Maximum) runs on H200: 0.001261 (modal.com/pricing 2026-09). Unlisted engines use the price above.
    media_modal_gpu_usd_per_second_by_engine: str = "wan22_t2v_a14b=0.001261,wan22_i2v_a14b_lightning=0.001261"
    media_modal_cold_start_timeout_s: int = 600
    media_modal_poll_interval_s: float = 3.0
    # How long status checks may keep failing in a row (a slow answer, a dropped connection, a 5xx) before
    # the job is given up. One bad check is not a failed video: the clip keeps running on the GPU meanwhile.
    media_modal_poll_failure_grace_s: float = 90.0
    runpod_api_key: str = ""
    media_runpod_base_url: str = "https://api.runpod.ai/v2"
    media_runpod_endpoint_id: str = ""
    media_runpod_engines: str = ""
    media_runpod_gpu_usd_per_second: float = 0.000486  # 48GB PRO (L40/L40S) flex, runpod.io/serverless-gpu 2026-09-15
    media_runpod_cold_start_timeout_s: int = 600
    media_runpod_poll_interval_s: float = 3.0
    # Seconds added to a GPU estimate so the reservation also covers a billed cold start. Not a price.
    media_gpu_cold_start_allowance_s: float = 120.0

    # Provider credentials. The same variables the rest of the Core already reads;
    # never logged, never returned.
    fal_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # ── Self-hosted engine ──────────────────────────────────────────────────
    # The URL is private (never shown to users); the token, if the deployment
    # puts ComfyUI behind an authenticating proxy, is sent as a Bearer header.
    media_comfyui_url: str = ""
    media_comfyui_token: str = ""
    # API-format workflows exported from ComfyUI ("Save (API Format)"). Each
    # contains the placeholders documented in media/comfyui.py. A missing
    # workflow leaves that operation unavailable.
    media_comfyui_image_workflow: str = ""
    media_comfyui_image_edit_workflow: str = ""
    media_comfyui_video_workflow: str = ""
    media_comfyui_image_to_video_workflow: str = ""
    media_comfyui_poll_interval_s: float = 1.5
    # GPU price used to turn measured engine seconds into cost (USD/hour).
    # Default = on-demand RTX 4090 class GPU (docs/MEDIA_ENGINES.md §6).
    media_gpu_usd_per_hour: float = 0.69

    # ── Hosted image: Z-Image Turbo on fal (Apache-2.0 weights) ─────────────
    media_fal_image_endpoint: str = "fal-ai/z-image/turbo"
    media_fal_image_usd_per_megapixel: float = 0.005  # fal.ai model page, 2026-09-14

    # ── Hosted image: FLUX.1-schnell on fal (Apache-2.0 weights, CP18) ──────
    # "" = not configured = unavailable, same convention as every other route. Set
    # MEDIA_FAL_FLUX_SCHNELL_ENDPOINT to add FLUX.1-schnell as an additional text-to-image
    # option alongside Z-Image Turbo and Qwen-Image — it never replaces them.
    media_fal_flux_schnell_endpoint: str = ""
    media_fal_flux_schnell_usd_per_megapixel: float = 0.003  # fal.ai/models/fal-ai/flux-1/schnell, 2026-09-20

    # ── Hosted video: Wan 2.2 A14B on fal (Apache-2.0 weights) ──────────────
    media_fal_video_endpoint: str = "fal-ai/wan/v2.2-a14b/text-to-video"
    # Per second of output, billed at 16 frames per second (fal.ai model page, 2026-09-14).
    media_fal_video_usd_per_second_480p: float = 0.04
    media_fal_video_usd_per_second_580p: float = 0.06
    media_fal_video_usd_per_second_720p: float = 0.08
    media_fal_poll_interval_s: float = 3.0

    # ── character_animate on fal: Wan 2.2 Animate Move (CP46) ────────────────
    # fal.ai/models/fal-ai/wan/v2.2-14b/animate/move, read 2026-09-22. Chosen over the sibling
    # fal-ai/wan-motion as character_animate's low-cost PRIMARY (Higgsfield Genjutsu remains
    # fallback/premium): wan-motion is fixed at 720p ($0.06/billed-second floor), while this model
    # offers a 480p tier at $0.04 — the only way to reach the cheapest rate at all.
    media_fal_animate_move_endpoint: str = "fal-ai/wan/v2.2-14b/animate/move"
    # DOCUMENTED billing formula (same page): total_frames = duration * fps (real, never assumed);
    # billed video-seconds = total_frames / 16, NOT rounded — the model page's own worked example
    # (300 frames / 16 = 18.75, billed at exactly 18.75) keeps the fraction. Price per billed
    # video-second, by resolution:
    media_fal_animate_move_usd_per_second_480p: float = 0.04
    media_fal_animate_move_usd_per_second_580p: float = 0.06
    media_fal_animate_move_usd_per_second_720p: float = 0.08

    # ── Hosted image: OpenAI Images API ─────────────────────────────────────
    media_openai_base_url: str = "https://api.openai.com/v1"
    media_openai_image_model: str = "gpt-image-1-mini"
    # Per 1M tokens (developers.openai.com/api/docs/pricing, 2026-09-14).
    media_openai_text_input_usd_per_mtok: float = 2.00
    media_openai_image_input_usd_per_mtok: float = 2.50
    media_openai_image_output_usd_per_mtok: float = 8.00

    # ── Hosted video: Veo on the Gemini API ─────────────────────────────────
    media_gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    media_veo_model: str = "veo-3.1-lite-generate-preview"
    media_veo_usd_per_second_720p: float = 0.05  # ai.google.dev pricing, 2026-09-14 (audio included)
    media_veo_poll_interval_s: float = 5.0

    # ── Hosted image and video: xAI Imagine API ─────────────────────────────
    xai_api_key: str = ""
    media_xai_base_url: str = "https://api.x.ai/v1"
    media_xai_image_model: str = "grok-imagine-image"
    media_xai_video_model: str = "grok-imagine-video"
    # docs.x.ai models page, 2026-09-14: $0.02 per image; $0.05/s for this video model.
    # 2k output and 720p are priced conservatively until a returned usage says otherwise.
    media_xai_image_usd_per_image: float = 0.02
    media_xai_image_2k_multiplier: float = 2.0
    media_xai_image_input_usd: float = 0.002
    media_xai_video_usd_per_second_480p: float = 0.05
    media_xai_video_usd_per_second_720p: float = 0.07
    media_xai_poll_interval_s: float = 5.0

    # ── Hosted image and video: Higgsfield (server-side only) ───────────────
    # Credentials: HIGGSFIELD_API_KEY_ID + HIGGSFIELD_API_KEY_SECRET, or HIGGSFIELD_API_KEY="id:secret".
    # Empty = every Higgsfield route is unavailable and the chain behaves as if it did not exist.
    higgsfield_api_key_id: str = ""
    higgsfield_api_key_secret: str = ""
    higgsfield_api_key: str = ""
    media_higgsfield_base_url: str = "https://api.higgsfield.ai"
    media_higgsfield_poll_interval_s: float = 3.0
    media_higgsfield_poll_max_interval_s: float = 10.0
    # Consecutive failed status reads (timeouts, 5xx) tolerated before the job is treated as lost.
    media_higgsfield_poll_error_limit: int = 5
    # Model paths, verified shapes and prices. Source: open.higgsfield.ai model pages, read 2026-09-18.
    # Prices are the highest published figure for exactly what is asked (promotions ignored), so an
    # estimate never undershoots; the API returns no cost, so a finished job is priced from these.
    # Aspect ratios list only values seen in the official request examples — anything else is refused
    # before submitting (no cost) until verified.
    media_higgsfield_soul_model: str = "higgsfield-ai/soul/v2/standard"
    # Soul 2 documents 9:16, 16:9, 4:3, 3:4, 1:1, 2:3, 3:2 (higgsfield.SOUL_2_ASPECT_RATIOS); only 4:3 is enabled
    # until the others are enabled deliberately. A value outside the documented list is refused before sending.
    media_higgsfield_soul_aspect_ratios: str = "4:3"
    media_higgsfield_soul_usd_720p: float = 0.0032
    media_higgsfield_soul_usd_1080p: float = 0.0057
    # Image editing: no Higgsfield model with a documented source-image field was found. Set all three
    # after verifying one; until then the route is unavailable.
    media_higgsfield_edit_model: str = ""
    media_higgsfield_edit_image_field: str = ""
    media_higgsfield_edit_aspect_ratios: str = ""
    media_higgsfield_edit_usd_per_image: float = 0.0
    media_higgsfield_wan_model: str = "alibaba/wan-3.0/text-to-video"
    media_higgsfield_wan_i2v_model: str = "alibaba/wan-3.0/image-to-video"
    media_higgsfield_wan_image_field: str = ""  # undocumented: image-to-video off until verified
    media_higgsfield_wan_aspect_ratios: str = ""  # only "adaptive" documented
    media_higgsfield_wan_usd_per_second_480p: float = 0.05
    media_higgsfield_wan_usd_per_second_720p: float = 0.10
    media_higgsfield_kling_model: str = "kling-video/v3.0/std/text-to-video"
    media_higgsfield_kling_i2v_model: str = "kling-video/v3.0/std/image-to-video"
    media_higgsfield_kling_image_field: str = ""  # undocumented: image-to-video off until verified
    media_higgsfield_kling_aspect_ratios: str = "16:9"
    media_higgsfield_kling_usd_per_second: float = 0.084  # list price; $0.042/s promotional
    media_higgsfield_grok_video_model: str = "xai/grok-imagine-video/v1.5/reference-to-video"
    media_higgsfield_grok_video_image_field: str = ""  # undocumented: image-to-video off until verified
    media_higgsfield_grok_video_aspect_ratios: str = ""  # only "auto" documented
    media_higgsfield_grok_video_usd_per_second_480p: float = 0.08
    media_higgsfield_grok_video_usd_per_second_720p: float = 0.14
    # CP34/35: higgsfiled/genjutsu/motion-transfer/v1.0 (open.higgsfield.ai, read 2026-09-21). Two typed
    # inputs (image_urls, video_url) — does not fit HiggsfieldModel's single image_field shape, so it is
    # its own small engine (HiggsfieldGenjutsuEngine) rather than a catalog() entry. List price; promotions
    # ignored, same convention as the other Higgsfield models above.
    media_higgsfield_genjutsu_motion_transfer_model: str = "higgsfiled/genjutsu/motion-transfer/v1.0"
    media_higgsfield_genjutsu_usd_per_second_480p: float = 0.318
    media_higgsfield_genjutsu_usd_per_second_720p: float = 0.681

    # ── Hosted vision: Gemini ───────────────────────────────────────────────
    media_vision_model: str = "gemini-3.1-flash-lite"
    # Per 1M tokens, image input billed as text (ai.google.dev pricing, 2026-09-14).
    media_vision_input_usd_per_mtok: float = 0.25
    media_vision_output_usd_per_mtok: float = 1.50
    # Gemini 3 bills one image at its default media resolution as 1,120 tokens.
    media_vision_image_tokens: int = 1120
    media_vision_max_output_tokens: int = 900
    media_vision_timeout_s: int = 45

    # Longest a single generation may run before it is failed and its
    # reservation released.
    media_image_timeout_s: int = 180
    media_video_timeout_s: int = 1800

    # Created media is kept this long, then swept with its object.
    media_retention_days: int = 30

    # Uploaded source images for editing / image-to-video.
    media_max_source_bytes: int = 10 * 1024 * 1024
    # CP22: uploaded source VIDEOS (e.g. Wan2.2-Animate's driving_video) — a separate, larger
    # limit from the image one above on purpose: a several-second 720p driving clip legitimately
    # runs tens of MB, and sharing one cap between the two would either starve video uploads or
    # let image uploads balloon for no reason. 100MB is a deliberate, bounded middle ground — a
    # short driving clip fits comfortably under it; a full-length video does not, by design.
    media_max_video_source_bytes: int = 100 * 1024 * 1024

    # ── Admission control (CP19) ────────────────────────────────────────────
    # DB-backed (Postgres, counting queued/running MediaJob rows) — no Redis/Kafka: at the
    # traffic this serves today, a COUNT query against an indexed `status` column is cheap
    # and correct, and it works from any number of API processes with no shared state of its
    # own. Revisit only if this count query is ever measured to be a real bottleneck.
    # 0 = unlimited (disabled), for local dev/tests.
    media_max_global_in_flight: int = 500
    # Per operation (job.operation), across all users — stops one capability's provider
    # outage/slowness from starving every other capability's share of the global cap.
    media_max_per_operation_in_flight: int = 100


@lru_cache
def media_settings() -> MediaSettings:
    return MediaSettings()
