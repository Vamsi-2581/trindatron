# TRINDATRON — Media Engines (image understanding, image and video creation)

Internal, technical. Nothing in this document is ever shown to a user: people only
ever meet TRINDATRON. Engines, vendors, models and prices stay in the Core.

Research and prices checked on **2026-09-14**. Prices live in
`packages/core/src/media/settings.py` (with this date), never in code, so a price
change is a configuration change and every estimate, reservation and plan limit
follows it.

---

## 1. Architecture

```
Composer (Mobile/Web)
  ├─ attach image ──► POST /sessions/{id}/attachments ──► attachment_service
  │                        └─ image? ──► vision_service.read_image ──► VisionEngine.describe
  │                                          (reserve → call → finalize measured cost)
  │                        └─ description stored as the attachment's text ──► TaskContext <attachments>
  └─ "crie uma imagem/vídeo…" ──► POST /sessions/{id}/message ──► classifier IMAGE/VIDEO
                                   └─ chat_media.start_from_chat ──► media_service.create_*_job
                                        └─ 202-style job ──► background run_job ──► ImageEngine/VideoEngine
                                             └─ Artifact (object storage) ──► GET /media/jobs/{id}
```

* `media/engines.py` — contracts: `ImageEngine`, `VideoEngine`, `VisionEngine`.
  Every engine reports what it cost (`cost_usd`) or its GPU time (`engine_seconds`),
  and estimates its own price before a job is accepted (`estimate_usd`).
* `media/hosted.py` — hosted adapters: `FalImageEngine` (Z-Image Turbo),
  `FalVideoEngine` (Wan 2.2 A14B), `OpenAIImageEngine`, `VeoVideoEngine`.
* `media/vision.py` — `GeminiVisionEngine`.
* `media/comfyui.py` — self-hosted open-weight engines (unchanged).
* `media/registry.py` — which engine runs, by configuration
  (`MEDIA_IMAGE_ENGINE`, `MEDIA_VIDEO_ENGINE`, `MEDIA_VISION_ENGINE`).
* The Model Router is **not** involved and was not changed: vision describes the
  image once at attach time, and that text reaches whichever chat model the router
  already picks.

## 2. Research — image understanding

| Option | Price (per 1M tokens) | Free tier | Image cost | Notes |
|---|---|---|---|---|
| Gemini 2.5 Flash-Lite | $0.10 in / $0.40 out | yes | cheapest | **retired 2026-10-16** — not chosen |
| **Gemini 3.1 Flash-Lite** | **$0.25 in / $1.50 out** | yes (rate-limited, not contractual) | 1,120 tokens per image (default resolution) | **chosen** |
| Gemini 3.5 Flash-Lite | $0.30 in / $2.50 out | yes | same tokens | more expensive, no need |
| Gemini 3.8 Flash | $0.75 in / $3.75 out (2026), doubles 2027 | yes | same tokens | better quality, 3× the price |

Chosen: **Gemini 3.1 Flash-Lite** — cheapest model that is not being retired, key
already present, GA, simple REST (`generateContent` with inline image).
Worst case per reading (reserved): (1,120 + 300) × $0.25 + 900 × $1.50 per 1M =
**$0.0017**. Measured on a real 1024² photo: **$0.000673**, 5.7 s.

## 3. Research — image generation

| Option | Price | Licence of the model | Key here | Notes |
|---|---|---|---|---|
| **Z-Image Turbo on fal** | **$0.005 / megapixel** (1024² ≈ $0.0052) | **Apache-2.0** (Tongyi-MAI) | no | cheapest, open weights, commercial use permitted — **preferred** |
| gpt-image-1-mini (low) | $2 text in, $8 image out per 1M tokens (1024² low = 272 tokens ≈ $0.0022) | proprietary API | **yes** | **running now** |
| Gemini 3.1 Flash Lite Image | ~$0.034 per 1K image | proprietary API | yes | 6× gpt-image-1-mini |
| Grok Imagine Image 2.0 | $0.04 per image | proprietary API | yes | 8× |
| Imagen 4 Fast | $0.02 per image (third-party listings) | proprietary API | yes | not on the official price page checked |
| Self-hosted Z-Image Turbo (ComfyUI) | GPU time (≈ $0.0015 per 1024² at $0.69/h, 30 % utilisation) | Apache-2.0 | n/a | needs an always-on GPU; no GPU on this machine |

Decision: the adapter for **Z-Image Turbo on fal** is the production preference
(cheapest per image and open weights) and is ready; it runs as soon as `FAL_KEY` is
configured. Until then the Core runs **gpt-image-1-mini** (the cheapest option with
a key present — within a few tenths of a cent of Z-Image).
Measured real call: 1024², low quality — **$0.002214**, 9.4 s (reservation sized
at $0.003776, which covers the longest accepted prompt).

## 4. Research — video generation

| Option | Price | Licence | Key here | Notes |
|---|---|---|---|---|
| **Wan 2.2 A14B on fal** | $0.04/s 480p, $0.06/s 580p, $0.08/s 720p (billed at 16 fps) | **Apache-2.0** | no | cheapest open option — **preferred** |
| Wan 2.2 5B on fal | $0.15 per video (720p, ≤5 s) | Apache-2.0 | no | cheaper per clip at 5 s, lower quality |
| **Veo 3.1 Lite** (Gemini API) | $0.05/s 720p, $0.08/s 1080p, audio included; 4/6/8 s clips | proprietary API | yes, **but quota/billing not enabled** | adapter ready |
| Veo 3.1 Fast | $0.10–$0.30/s | proprietary | same | 2× Lite |
| LTX-2 / 2.3 | fal $0.04/s (1080p fast) | **LTX-2 Community Licence**: free only under **$10M annual revenue**; SaaS allowed; attribution and use restrictions; paid licence above the threshold | no | **not used**: revenue-conditional licence is a legal dependency — adapter not built |

Decision: **Wan 2.2 A14B on fal** (Apache-2.0) is preferred; **Veo 3.1 Lite** is the
adapter that runs with the key present. Real call on 2026-09-14: Veo answered
`429 RESOURCE_EXHAUSTED — check your plan and billing details` before generating
anything, so **no video was produced and nothing was charged**. Video stays
**off** in the local Core until either a fal key or Veo billing is enabled — a
capability is never offered when its engine cannot run.

## 5. Licences

| Model | Licence | Commercial / SaaS | Restrictions | Used? |
|---|---|---|---|---|
| Z-Image Turbo | Apache-2.0 | yes | none beyond Apache (notice) | yes (preferred, via fal) |
| Wan 2.2 (5B, A14B) | Apache-2.0 | yes | responsible-use statement in the repo | yes (preferred, via fal) |
| LTX-2.x | LTX Community Licence | only under $10M revenue | attribution, acceptable-use list, output must be marked machine-generated, termination on breach | **no** |
| Gemini / Veo / gpt-image | provider API terms | yes, as API customer | provider usage policies | yes |

## 6. Economics

TCU = $0.01 of provider cost. Rules (tests: `test_media_hosted_economics.py`,
`test_media_economics.py`):

1. One operation must fit the plan's per-request ceiling (`max_tcu_per_request`).
2. Each kind (images, videos, image readings) may use at most **25 %** of what the
   plan can spend per day — the smaller of its daily window and a seventh of its
   weekly window — at the most one operation can cost on the configured engine.
3. So all three kinds used to the maximum every day for a week stay under **75 %**
   of the weekly window that prices the plan; chat keeps the rest.
4. Every operation reserves first and is finalized with its measured cost; a
   failed operation is cancelled and costs nothing.
5. FREE is also bounded plan-wide by `free_plan_global_daily_budget_usd`.

Per-plan offer, computed by the code (`media_service.image_unit_usd`,
`media_service.video_offer`, `plans.vision_daily_limit`):

| Plan | Per call | A day (sustainable) | 25 % share | Images/day (gpt-image-1-mini · Z-Image) | Video (Veo Lite · Wan on fal) | Image readings/day |
|---|---:|---:|---:|---|---|---:|
| FREE | 2 TCU | 2.86 TCU | 0.71 | **1** ($0.0049) · 1 ($0.0052) | none · none | **4** |
| START | 10 | 15.71 | 3.93 | **8** · 7 | none · none | 23 |
| PRO | 11 | 33.57 | 8.39 | **17** · 16 | none · **1 × 2 s 480p** ($0.0825) | 49 |
| MAX | 60 | 135.71 | 33.93 | **23** (medium quality, $0.0143) · 64 | **1 × 4 s 720p** ($0.20) · 1 × 4 s 720p ($0.325) | 198 |
| TEAM | 60 | 135.71 | 33.93 | 23 · 64 | 1 × 4 s · 1 × 4 s | 198 |

Why FREE has no video: the cheapest clip any engine makes (Wan 480p, 1 s, 17 frames
= $0.0425 = 4.25 TCU) is above FREE's 2 TCU per-request ceiling.
Why START has no video: its share of a day is 3.93 TCU, and the cheapest 2 s clip
costs 8.25 TCU; Veo's shortest clip (4 s) costs 20 TCU, above its 10 TCU ceiling.
Why FREE gets one image a day: FREE's week is 20 TCU; a quarter of a seventh of it
is 0.71 TCU, and one image costs 0.49–0.52 TCU.

## 7. Real calls on 2026-09-14 (total spent)

| Call | Result | Measured cost |
|---|---|---:|
| Image, gpt-image-1-mini, 1024² low | ok, 97 KB JPEG, 9.4 s | $0.002214 |
| Image reading, Gemini 3.1 Flash-Lite | ok, 1,253 in / 240 out tokens, 5.7 s | $0.000673 |
| Video, Veo 3.1 Lite, 4 s 720p (submitted twice: attempt + diagnostic) | refused 429 before generation | $0.00 |
| **Total** | | **$0.002887** |

## 8. Configuration

```
MEDIA_VISION_ENGINE=gemini          # GEMINI_API_KEY
MEDIA_IMAGE_ENGINE=fal|openai       # FAL_KEY | OPENAI_API_KEY
MEDIA_VIDEO_ENGINE=fal|veo          # FAL_KEY | GEMINI_API_KEY with Veo billing
```

Leave a value empty to keep that capability off; `GET /users/me/capabilities`
then reports it unavailable and no client shows it.

---

## 9. Multi-engine × multi-runner platform (2026-09-15)

TRINDATRON does not depend on one model or one cloud. Every creation is served by
a **route** = one **engine** (what the model is) on one **runner** (where it runs),
chosen per request and replaced automatically when it fails.

```
media_service ──► VideoEngineChain / ImageEngineChain   (media/orchestrator.py)
                    ├─ VideoRoute(Wan22-TI2V-5B adapter, RunPodRunner)   "wan22_ti2v_5b@runpod"
                    ├─ VideoRoute(Wan22-TI2V-5B adapter, ModalRunner)    "wan22_ti2v_5b@modal"
                    ├─ VideoRoute(LTX-Video adapter,     ModalRunner)    "ltx_video_13b_distilled@modal"
                    ├─ VideoRoute(Wan22-I2V-A14B adapter, ZeroGPURunner) "wan22_i2v_a14b@zerogpu"  (experimental)
                    └─ HostedVideoRoute(xAI API)                         "grok_imagine_video@xai"
```

| Layer | Module | Knows |
|---|---|---|
| EngineAdapter | `media/adapters.py` | tasks, sizes and proportions, durations, fps, steps, quality grade, measured GPU seconds, catalog entry |
| Runner | `media/runners/` | protocol, credentials, queue/cold start, failures, GPU price |
| Route | `media/routes.py` | joins one adapter and one runner; licence and experimental policy |
| Orchestrator | `media/orchestrator.py` | selection, ranking, budget, fallback, trace |
| Health | `media/health.py` | cooldown per route and per runner |
| Catalog | `media/engine_catalog.py` | researched / approved / rejected, licence conditions; integrated / available / healthy computed |

### Selection, per request

capability → task (text-to-video, image-to-video, text-to-image, edit) → task
compatibility (no network) → licence → quality floor (draft 2, standard 3, premium 4)
→ ranking: healthy first, priced first, **cheapest for this request**, then operator
order → budget (never more than was reserved) → availability → execution → fallback.
MAX/TEAM have a higher floor, not a license to spend: the cheapest route above the
floor still wins.

### Fallback policy

| Failure kind | Next route | Cooldown (threshold) |
|---|---|---|
| `quota` (429 with quota, 402) | yes | **runner-wide** 900 s (1) |
| `rate_limited` (429) | yes | **runner-wide** 60 s or Retry-After (1) |
| `gpu_unavailable` | yes | route 90 s (1) |
| `unavailable` (down, sleeping, not deployed) | yes | route 60 s (2) |
| `timeout`, `cold_start_timeout` | yes | route 300 s (2) |
| `temporary` (5xx, connection) | yes | route 120 s (3) |
| `oom`, `unsupported_format`, `unsupported_duration` | yes | none (request-specific) |
| `rejected`, `filtered` | **stop** | none |

Each route is tried at most once per call; cooldowns are capped at one hour; one
success clears a scope. Everything is recorded internally in the job
(`params.engine_trace`, `params.engine_metrics`: route, queue, cold start,
inference, GPU and total seconds, output bytes) and never returned by any API.

### Runners

| Runner | Kind | Contract | Cost |
|---|---|---|---|
| `zerogpu` | **experimental**, not commercial, refused in production | Gradio queue protocol of a public Space (`/config` → `fn_index`, `/gradio_api/upload`, `/gradio_api/queue/join` + `/gradio_api/queue/data` SSE until `process_completed`). The simpler `/gradio_api/call/{api}` hides every error as `data: null` and was replaced after a real run turned an exhausted quota into an anonymous failure. Space state from the Hub API; `HF_TOKEN` for account quota | $0 inside quota |
| `modal` | commercial | TRINDATRON worker protocol over HTTPS | GPU seconds × `MEDIA_MODAL_GPU_USD_PER_SECOND` (L40S $0.000542) |
| `runpod` | commercial | RunPod Serverless `/run`, `/status`, `/cancel`, `/health` with the worker job as `input.job` | `executionTime` × `MEDIA_RUNPOD_GPU_USD_PER_SECOND` (48GB PRO flex $0.000486) |
| `xai`, `openai`, `fal`, `gemini`, `comfyui` | hosted APIs / self-hosted, wrapped as routes | their own adapters | their own pricing |

**Worker protocol** (one image, any cloud). Job (`RunnerJob.payload()`):
`{engine, task, prompt, width, height, seed, duration_seconds, fps, frames, steps, guidance, negative_prompt, source: {content_type, data_base64}}`.
Output: `{content_type, data_base64 | url, width, height, duration_seconds, metrics: {queue_seconds, cold_start_seconds, inference_seconds, gpu_seconds}}`.
Failure: `{kind: <failure kind>, message}`. Modal: `POST /v1/jobs {"job"} → {id}`,
`GET /v1/jobs/{id} → {status: queued|running|completed|failed, output|error}`, `GET /v1/health`.
When a worker reports no GPU seconds, the measured wall time is used as an upper bound
and recorded (`cost_basis`); nothing is priced from a guess.

### Engine × runner matrix

| Engine (licence) | zerogpu | modal | runpod | hosted |
|---|---|---|---|---|
| Wan2.2-TI2V-5B (Apache-2.0) — T2V + I2V, q3 | ✅ Space `KingNish/wan2-2-fast` | ✅ worker | ✅ worker | fal (A14B) |
| Wan2.2-I2V-A14B (Apache-2.0) — I2V, q4 | ✅ Space `zerogpu-aoti/wan2-2-fp8da-aoti-faster` | ✅ worker | ✅ worker | — |
| LTX-Video 0.9.8 13B distilled (conditional: < US$10M revenue) — T2V + I2V, q3 | ✅ Space `Lightricks/ltx-video-distilled` | ✅ worker | ✅ worker | — |
| Z-Image-Turbo (Apache-2.0) — image, q3 | ✅ Space `Tongyi-MAI/Z-Image-Turbo` | ✅ worker | ✅ worker | fal |
| Qwen-Image (Apache-2.0) — image, q4 | ✅ Space `Qwen/Qwen-Image` | ✅ worker | ✅ worker | — |
| grok-imagine video/image (API terms), q4 | — | — | — | xai |
| gpt-image-1-mini (API terms), q3 | — | — | — | openai |
| Veo 3.1 Lite (API terms), q5 | — | — | — | gemini — optional paid fallback, never required |

✅ = supported by the runner code; a modal/runpod cell runs only where the operator
has deployed that engine (`MEDIA_MODAL_ENGINES`, `MEDIA_RUNPOD_ENGINES`).
Researched, approved, no adapter yet: MAGI-1 (Apache-2.0), LongCat-Video (MIT),
Kandinsky 5.0 Video Lite (MIT), Mochi 1 (Apache-2.0), Qwen-Image-Edit (Apache-2.0).
Rejected by licence: HunyuanVideo-1.5 (excludes EU/UK/KR), MiniMax H3 (excludes
US/EU/UK/KR), SkyReels-V2 (unreviewed custom licence), FLUX.1 dev (non-commercial),
FLUX.2 klein (licence unconfirmed), Sora 2 (API retiring).

### Economics: guaranteed vs experimental

A plan's promise (daily counts, longest clip) is sized only on **commercial**
routes (`media_service.guaranteed_engine`). A free runner never makes a promise,
even at $0. Outside production, if only experimental routes exist, paid plans may
try them (`video_guaranteed=false`), and FREE gets video only when the operator
sets `MEDIA_FREE_EXPERIMENTAL_VIDEO_DAILY_LIMIT` (START's ceilings). In production
experimental routes are never eligible. Every job still reserves the dearest
affordable fallback and is finalized with the measured cost of the route that
delivered.

### Configuration

```
MEDIA_VIDEO_ROUTES=wan22_ti2v_5b@runpod,wan22_ti2v_5b@modal,ltx_video_13b_distilled@modal,grok_imagine_video@xai
MEDIA_IMAGE_ROUTES=z_image_turbo@runpod,gpt_image_1_mini@openai,grok_imagine_image@xai
MEDIA_ACCEPTED_LICENSE_CONDITIONS=ltx_revenue_below_10m   # only while true
MEDIA_MODAL_URL / MEDIA_MODAL_TOKEN / MEDIA_MODAL_ENGINES / MEDIA_MODAL_GPU_USD_PER_SECOND
RUNPOD_API_KEY / MEDIA_RUNPOD_ENDPOINT_ID / MEDIA_RUNPOD_ENGINES / MEDIA_RUNPOD_GPU_USD_PER_SECOND
HF_TOKEN / MEDIA_ZEROGPU_*_SPACE                          # experimental, never production
```

### Real runs on 2026-09-15 (local test Core, scripted nothing)

| Request | Route path (internal) | Result | Cost |
|---|---|---|---|
| "Anime esta imagem por 3 segundos…" (MAX) | `wan22_i2v_a14b@zerogpu` completed | MP4 560×832, 3.06 s, 16 fps, 39 s; motion checked frame by frame; history restored | $0 |
| "Crie um vídeo vertical 9:16 de 3 segundos…" (MAX) | `wan22_ti2v_5b@zerogpu` failed → `ltx_video_13b_distilled@zerogpu` failed → `grok_imagine_video@xai` completed | MP4 480×848, 3.04 s, 88.6 s | $0.15 |
| "Crie uma imagem vertical 9:16…" (MAX, premium floor) | two grade-3 routes skipped by quality → `qwen_image@zerogpu` failed → `grok_imagine_image@xai` completed | JPEG 720×1280, 12 s | $0.02 |
| "Crie uma imagem vertical 9:16…" (PRO, queue protocol) | `qwen_image@zerogpu` failed (quota, retry in 18 h) → `z_image_turbo@zerogpu` skipped (runner cooling) → `gpt_image_1_mini@openai` completed | JPEG 1024×1536, 14 s | $0.0033 |

The two ZeroGPU failures in the second and third rows were the anonymous daily quota
(confirmed through gradio_client: "You have exceeded your ZeroGPU quota (120s
requested vs. 40s left)"); the old `/call` protocol reported them without a reason,
which is why the runner moved to the queue protocol (fourth row). No API response
contained an engine, runner or model name.

### Voice and agents (planned on the same separation, not implemented here)

* **STT / TTS**: capability → engine (Whisper large-v3-turbo, Kokoro, Chatterbox,
  ElevenLabs, Gemini TTS) → runner (Groq, ElevenLabs, Gemini, a GPU worker for
  open weights) → the same health and failure kinds. Today `api/routes/voice.py`
  already falls back across providers; the next step is to express those providers
  as routes so voice shares `media/health.py` and the catalog.
* **Agents / computer use**: capability → tool (browser, shell, desktop) → runner
  (Docker sandbox now; a remote sandbox provider later) → fallback. No second
  orchestrator: the route + health + failure-kind contract is reused.

## 10. Higgsfield as a hosted route (2026-09-18, fakes only — no real call yet)

Higgsfield is one more hosted route in the orchestrator, never a path of its own:
TRINDATRON → Media Orchestrator → (ranking, budget, fallback) → a Higgsfield route.
Code: `packages/core/src/media/higgsfield.py`. Server-side only; nothing about it
reaches a client (provider, model, URL, price, request id or provider error text).

### Routes (media/registry.py)

| Route | Task | Model path | Default state | Price used (USD) |
|---|---|---|---|---|
| `soul_2@higgsfield` | text→image | `higgsfield-ai/soul/v2/standard` | on with credentials; 4:3 only | 0.0032 (720p) / 0.0057 (1080p) per image |
| `image_edit@higgsfield` | image edit | *(none verified)* | off | set with the model |
| `wan_3@higgsfield` | text→video (image→video off) | `alibaba/wan-3.0/text-to-video` | off: no verified aspect ratio | 0.05/s 480p, 0.10/s 720p |
| `kling_3_std@higgsfield` | text→video (image→video off) | `kling-video/v3.0/std/text-to-video` | on with credentials; 16:9 only | 0.084/s (list; 0.042/s promo ignored) |
| `grok_imagine_video_1_5@higgsfield` | text→video | `xai/grok-imagine-video/v1.5/reference-to-video` | off: no verified aspect ratio | 0.08/s 480p, 0.14/s 720p |

A route joins a chain only when listed in `MEDIA_IMAGE_ROUTES` / `MEDIA_VIDEO_ROUTES`,
and runs only with credentials **and** the operator's acceptance of the API terms
(`MEDIA_ACCEPTED_LICENSE_CONDITIONS=higgsfield_api_terms`, engine_catalog.py). With no
routes listed, nothing changes. Listed without credentials: "unavailable", and the chain
serves the next route.

Only parameters seen in the official request examples are sent. A shape that is not a
verified aspect ratio is refused before anything is sent (UNSUPPORTED_FORMAT, no cost,
the chain moves on) — a portrait request is never bent into the only verified landscape.
Image→video and editing stay off until the image field of a model is verified; set
`MEDIA_HIGGSFIELD_*_IMAGE_FIELD` / `*_ASPECT_RATIOS` after verifying. Prices and model
paths are all `MEDIA_HIGGSFIELD_*` settings. Responses carry no cost, so a finished job is
billed at the configured price of exactly what was asked (never above the reservation).

### Configuration

```
HIGGSFIELD_API_KEY=<key id>:<key secret>   # official format (the ID/SECRET pair is still read, not recommended)
MEDIA_ACCEPTED_LICENSE_CONDITIONS=higgsfield_api_terms
MEDIA_IMAGE_ROUTES=soul_2@higgsfield,<other routes>
MEDIA_VIDEO_ROUTES=kling_3_std@higgsfield,<other routes>
```

Values live only in `packages/core/.env` (git-ignored). Never in Mobile, code, tests or logs.

### Job lifecycle and idempotency

Submit → the request id is persisted on the job (`media_jobs.provider_request_id`,
`provider_ref`, migration 0042) **before** any polling → poll status with a global deadline
→ download (no credentials sent to the output host) → object storage → Artifact → settle.

| Call | Repeatable | Why |
|---|---|---|
| submit | **never** | no idempotency key: a second POST is a second paid job |
| status | yes | read-only; transient errors tolerated up to `MEDIA_HIGGSFIELD_POLL_ERROR_LIMIT` |
| cancel | yes | a second cancel is answered 400 |
| upload | yes | new URL each time, nothing billed |
| download | yes (3 tries) | outputs are kept ≥ 7 days |

- A submit that may have reached the provider without an answer (read timeout, dropped
  connection, 502/504, 2xx without request id) fails the job with no retry and no
  fallback (`EngineFailed.may_have_started`). The person is not charged.
- A connection that never opened, or a clear error answer (400/401/403/404/422/423/429/500/503),
  goes to the next route in the chain — never back to the same route
  (`EngineFailed.resubmit_safe=False`, honoured by `media_service._generate`).
- Provider `failed` → next route (not billed). `nsfw` → stops the chain (FILTERED).
  `canceled` → job `cancelled`.
- Deadline passed: cancel if still queued (refunded) and let the next route try; if it
  is already running, stop without resubmitting.
- Restart: jobs with a provider request id are followed again at startup
  (`media_service.resume_provider_jobs` → `resume_job`), never resubmitted, and are not
  reclaimed as stale for `PROVIDER_FOLLOW_DAYS` (6).

### Cancel

`POST /media/jobs/{id}/cancel` → queued: cancelled locally, reservation released. Running:
only if the provider job is still queued there and the provider accepts (202, refunded).
Once generation started the provider bills it, so the answer is `409 cannot_cancel` and the
creation completes. Status `cancelled` is terminal and hidden from Creations.

### Not done (by design)

- **Webhook**: not used. Higgsfield webhooks (`?hf_webhook=`) are not signed, so a
  delivery cannot be authenticated; polling is used. A future webhook must only be a hint
  to poll, never a source of truth.
- No real call has been made; the catalog prices and fields come from the model pages
  (read 2026-09-18) and must be confirmed on the first authorized generation (CP3).

### First real generation and follow-ups (CP3–CP5, 2026-09-18)

- CP3: HTTP 422 before generation — the Core sent `seed=1163474900`; Soul 2 accepts 1…1,000,000.
  Fixed in `higgsfield.py` (`provider_seed`, `validate_body`); $0 spent.
- CP4: one submit, request `4c0af596-c1a3-46d0-aa19-170e943daa66`, completed in ~20 s, Artifact
  PNG **1536×1152** for `resolution=720p`, `aspect_ratio=4:3`; settled 0.32 TCU (US$0.0032, the
  configured price).
- **Resolution is a tier, not pixels.** The Soul 2 docs define only the values (`720p`, `1080p`)
  and seven ratios; no page states output pixels. The observed 720p 4:3 output was 1536×1152, so
  the Artifact keeps the measured size and nothing assumes 960×720. Pricing follows the request
  parameters ("the exact cost depends on the selected model and parameters", Billing and
  retention), not the delivered pixels.
- **Official estimate endpoint** (missed in CP1): `POST https://api.higgsfield.ai/estimate/<model
  path>` with the same body returns `{"credits", "usd"}` — "the authoritative amount" for the
  account; it creates no generation. Not called yet (needs authorization). No balance endpoint is
  documented. The price of CP4 therefore remains unconfirmed until the estimate or the dashboard is
  read; internal prices are unchanged.
- Unchanged by design: `enhance_prompt` not sent (provider default `true` applies); only 4:3
  enabled; 1:1 would be asked at 1080p ($0.0057) by the current sizing rule.

## 11. CP18 — additive capabilities (2026-09-20)

Independent cross-audit (CP17) proposed 7 candidates. Two fit the existing architecture cleanly
and are implemented, additively — nothing already working was removed or reordered as preferred.
Five needed a genuinely new capability type or input modality (a driving video, an audio clip, a
video-to-video task) that `ImageRequest`/`VideoRequest`/`RunnerJob` do not carry today; per CP18's
own instruction not to force an integration that does not fit, they were left for a dedicated
future checkpoint instead of a rushed schema change to types every existing engine depends on.

| Capability | Provider/model | License | State | Fallback position | Limitation |
|---|---|---|---|---|---|
| STT | Whisper local (whisper.cpp) | MIT | Registered only if `STT_LOCAL_ENABLED=true`; joins `STT_CAPABLE` last | Last resort, after elevenlabs/groq | Never becomes preferred by this change alone; $0 by construction (`calculate_whisper_local_stt_cost_usd`) |
| text-to-image | FLUX.1-schnell (fal) | Apache-2.0, unconditional commercial use | Off until `MEDIA_FAL_FLUX_SCHNELL_ENDPOINT` is set | Additional route (`flux1_schnell@fal`) alongside `z_image_turbo@fal`/`qwen_image`/`soul_2@higgsfield`, ranked by the existing cost/quality/health rules | No source-image editing; fal.ai only — Black Forest Labs does not sell this model on their own API |

**Not implemented this checkpoint, with the specific gap found by reading the code:**
- **Wan2.2-Animate-14B** (video-to-video/character animation) and **Wan2.2-S2V-14B**
  (audio-driven video) — both need a second media input (`RunnerJob`/`VideoRequest` carry one
  `source`, documented as "the first frame"); Animate needs a driving video, S2V needs an audio
  clip. Adding either is a real schema change to types every image/video engine shares, not an
  adapter registration.
- **SeedVR2** (video upscale/restoration) — same missing video-to-video task type, plus its own
  research found it needs ≥1×H100-80GB for 720p — disproportionate for this checkpoint's scope.
- **BiRefNet** (background removal) needs real model inference (weights download, torch,
  GPU-or-slow-CPU) and a new Engine ABC (none of `ImageEngine`/`VideoEngine`/`VisionEngine` fit a
  promptless image-in/image-with-alpha-out transform); building that correctly, plus its own
  job/artifact path, is comparable in size to the video items above and was left for the same
  reason — a dedicated follow-up checkpoint, not a rushed addition under this one's time budget.

**VTracer (vectorization, raster-to-SVG) did fit, and is implemented** — see `src/media/vectorize.py`.
Unlike every other capability here, it needs no model, no GPU and no network: it is a deterministic
algorithm (hierarchical clustering + curve fitting), so it runs as a small, separate, synchronous,
non-billed local utility rather than going through the GPU-job polling machinery the generative
capabilities need.

**Security**: `src/media/runners/base.py`'s new `assert_public_https_url()` closes the SSRF-adjacent
gap CP16/CP17 flagged (a provider-returned output URL was fetched with only an `https://` prefix
check, no protection against a literal private/internal address) — applied to `hosted.py`'s
`_download()` (fal/OpenAI/Veo) and `higgsfield.py`'s `download()`. It does not resolve DNS names,
so a hostname that only resolves to a private address at connect time is not caught — a narrower,
minimal fix, not a full SSRF defense.

## 12. CP19 — the permanent TRANSFORM/COMPOSE contract (2026-09-20)

CP18 found BiRefNet needed "a new Engine ABC" and left it for a follow-up rather than force it
onto `ImageEngine`. That follow-up is `src/media/operations.py`: `OperationRequest` (operation,
typed `inputs[]`, parameters, budget) + `OperationEngine` (`is_available`/`estimate_usd`/`run`) +
`OperationEngineChain`, which reuses `orchestrator.rank()`/`run_chain()` **unchanged** — those were
already duck-typed, never `ImageEngine`-specific, so no existing GENERATE code path was touched.

**No database migration.** `MediaJob.kind` stays the output type ("image"); `MediaJob.operation`
becomes the operation name ("background_remove", "vectorize"); the typed input list lives in the
already-existing `params` JSON column. This reuses the image daily-quota, kill-switch and
output-size-cap machinery as-is — no new entitlement dimension.

**BiRefNet** (`OPERATION_ADAPTERS["birefnet"]`, `media/adapters.py`) is the first real TRANSFORM
operation: `background_remove`, one image in, one transparent PNG out — registered via the same
Adapter × Runner split as every self-hosted image/video model, on `ZeroGPURunner`. **Off by
default**: `MEDIA_ZEROGPU_BIREFNET_SPACE` is unset, because no public Gradio Space's exact input
schema has been live-verified (`media/runners/zerogpu.py` `_birefnet` — a reasonable guess at the
common one-image-in/one-image-out shape, not a confirmed one). MIT license, ~220M params, $0 inside
ZeroGPU's quota. Verify the chosen Space's own `/config` before setting it.

**VTracer** now also implements the `OperationEngine` shape (`VTracerEngine`,
`media/vectorize.py`) for conceptual consistency, but `POST /media/vectorize` still calls it
directly and synchronously — not through `create_operation_job`/the job queue — since forcing a
free, always-sub-100ms, deterministic operation through async polling adds latency, not value.

**Admission control** (`media_service._admission_check`): two DB-backed limits —
`MEDIA_MAX_GLOBAL_IN_FLIGHT` (default 500) and `MEDIA_MAX_PER_OPERATION_IN_FLIGHT` (default 100) —
a `COUNT` query against `MediaJob.status`, no Redis/Kafka. Per-tenant fairness already existed
structurally (one active job per `(user, kind)`, pre-CP19). Per-provider/per-runner limits are a
real future dimension but that data lives only inside `params`/`provider_ref` JSON today; adding
an indexed column for it is a deliberate follow-up, not a redesign of `_admission_check`.

**Not built**: COMPOSE (no real use case yet — the contract supports multi-input, nothing
implements it), a fairness/priority scheduler (the admission check is the smallest persistent
mechanism, not a scheduler), Wan2.2-Animate/S2V/SeedVR2 (still need a genuinely new input modality
— a driving video, an audio clip — the contract is *designed* for this via `inputs[]`, but no
engine has been written yet).
