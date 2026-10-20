# TRINDATRON video worker (Modal)

One app, one GPU class per video-mode class. A job goes to the class that serves its `engine`:

| Class | Engine | GPU | Modes |
|---|---|---|---|
| `Wan5B` | `wan22_ti2v_5b` (FastWan2.2-TI2V-5B, 3 steps, 24 fps) | H100 | Economy 480p, Quality 720p, 1–6 s |
| `WanA14B` | `wan22_t2v_a14b` (Wan2.2-T2V-A14B + lightx2v Lightning 4-step LoRA 250928, 16 fps) | H200, no offload | Maximum 720p, 1–5 s |

The sections below describe the 5B; Maximum is at the end.

Wan 2.2 TI2V-5B distilled (FastVideo `FastWan2.2-TI2V-5B-FullAttn-Diffusers`, 3 DMD steps, 24 fps)
on one H100 per container. Serves the video modes **Economy (480p)** and **Quality (720p)**,
1–6 s, text to video. It speaks the worker protocol the Core's `ModalRunner` uses
(`packages/core/src/media/runners/gpu_worker.py`).

| File | What |
|---|---|
| `video_worker.py` | the Modal app: GPU class `Wan5B`, HTTP endpoint `api`, CPU `prefetch` |
| `video_worker_logic.py` | sizes, frames, budget check, refusals — no Modal, tested in the Core suite (`tests/unit/test_modal_video_worker.py`) |

## Behaviour

- **Warm while used, zero when idle:** `min_containers=0`, `scaledown_window=120 s`, at most 2 GPUs (`TRINDATRON_VIDEO_MAX_CONTAINERS`).
- **Cold start ≈ 80–100 s** (weights from the volume + a warm-up clip); never billed to a job.
- **Budget:** the Core sends `limits.max_gpu_seconds` (the job's reservation ÷ GPU price). A clip predicted to need more is refused before any GPU second (`rejected`). A job's `timeout` is 180 s.
- **Cancel:** `DELETE /v1/jobs/{id}`. A job already running takes its container with it (interrupting FastVideo mid-run breaks the container's next jobs); a failed generation retires its container too.
- **Output:** the mp4 as base64; the Core stores it in its own storage (R2). The worker holds no storage credentials.
- **Metrics per job:** queue, cold start, inference, encode and GPU seconds, GPU, container, size.
- **Image-to-video is refused:** this checkpoint ignored the image (measured). Photo → Movement is a separate engine.

## Operate

```bash
# once: token shared with the Core, and the weights (CPU only)
modal secret create trindatron-video-worker WORKER_TOKEN=<random>
modal run infra/modal/video_worker.py::prefetch

modal serve infra/modal/video_worker.py    # isolated, ephemeral (…-api-dev.modal.run)
modal deploy infra/modal/video_worker.py   # permanent (…-api.modal.run)
```

Core configuration (Railway) to route Economy/Quality here:

```
MEDIA_MODAL_URL=https://<workspace>--trindatron-video-worker-api.modal.run
MEDIA_MODAL_TOKEN=<the same WORKER_TOKEN>
MEDIA_MODAL_ENGINES=wan22_ti2v_5b
MEDIA_MODAL_TASKS=text_to_video
MEDIA_MODAL_GPU_USD_PER_SECOND=0.001097        # H100
MEDIA_VIDEO_ROUTES=wan22_ti2v_5b@modal,grok_imagine_video@xai   # premium stays confirmation-only
```

## Measured (2026-09-26, H100, warm)

| Clip | GPU s | Real cost | TCU | Reserved TCU |
|---|---|---|---|---|
| Economy 480p 5 s | 8.1 | US$ 0.0089 | 0.89 | 1.24 |
| Quality 720p 5 s | 15.4 | US$ 0.0169 | 1.69 | 2.31 |
| Economy 480p 6 s | 9.2 | US$ 0.0101 | 1.01 | 1.49 |
| Quality 720p 6 s | 18.4 | US$ 0.0202 | 2.02 | 2.77 |

## Maximum — Wan 2.2 T2V-A14B + Lightning on H200

```bash
modal run infra/modal/video_worker.py::prefetch_a14b   # once: A14B + LoRA into the volume (CPU only)
modal run infra/modal/video_worker.py::smoke_a14b      # one real clip on an ephemeral app, nothing deployed
```

Core configuration, added to the 5B's (same URL and token, same worker):

```
MEDIA_MODAL_ENGINES=wan22_ti2v_5b,wan22_t2v_a14b
MEDIA_MODAL_GPU_USD_PER_SECOND_BY_ENGINE=wan22_t2v_a14b=0.001261   # H200 (the default)
MEDIA_VIDEO_ROUTES=wan22_ti2v_5b@modal,wan22_t2v_a14b@modal,grok_imagine_video@xai
```

The mode decides the route (`media/modes.py`): Maximum reaches only `wan22_t2v_a14b`, Economy/Quality only the 5B.
The job is reserved at the Maximum reference (9.31 TCU for 5 s) and settles at the GPU seconds the worker
reports × the H200 price.

Measured 2026-09-27 (`smoke_a14b`, H200, 1280×720, 81 frames, 4 steps): **58.5 GPU s → US$ 0.0737 = 7.37 TCU**
(reserved 9.31 TCU; the worker was allowed 73.8 GPU s). Cold start 204 s (image + weights + a warm-up clip),
never billed to a job. Motion steady across all 5 seconds.

## Photo → Movement — Wan 2.2 I2V-A14B + Lightning on H200

The person's photo is the first frame (class `WanI2V`, engine `wan22_i2v_a14b_lightning`). Maximum mode only, so
PRO, MAX and TEAM (the plans that have Maximum); a photo never goes to the 5B, which ignores it.

```bash
modal run infra/modal/video_worker.py::prefetch_i2v                       # once: I2V-A14B + its Lightning LoRA
modal run infra/modal/video_worker.py::smoke_i2v --image photo.jpg        # one real clip, ephemeral app
```

Core configuration, added to what is already there (same worker, URL and token):

```
MEDIA_MODAL_ENGINES=wan22_ti2v_5b,wan22_t2v_a14b,wan22_i2v_a14b_lightning
MEDIA_MODAL_TASKS=text_to_video,image_to_video
MEDIA_VIDEO_ROUTES=wan22_ti2v_5b@modal,wan22_t2v_a14b@modal,wan22_i2v_a14b_lightning@modal,grok_imagine_video@xai
```

`image_to_video` in `MEDIA_MODAL_TASKS` is safe for the 5B: a photo job only ever considers Maximum-class routes
(media_service.create_video_job), and the worker refuses a photo for any engine but the I2V one.

Measured 2026-09-29 (`smoke_i2v`, a 832×480 photo → 1280×720, 81 frames, 4 steps): **62.5 GPU s → US$ 0.0788 =
7.88 TCU** (reserved 9.31 TCU). Cold start 320 s on a fresh image pull, never billed to a job. First frame is the
photo; motion across all 5 seconds.
