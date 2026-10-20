"""TRINDATRON video worker on Modal — one app, one GPU class per video-mode class:

  Wan5B     Wan 2.2 TI2V-5B distilled on H100            Economy 480p / Quality 720p
  WanA14B   Wan 2.2 T2V-A14B + Lightning 4-step on H200  Maximum 720p, up to 5 s
  WanI2V    Wan 2.2 I2V-A14B + Lightning 4-step on H200  Photo -> Movement (Maximum), up to 5 s

A job goes to the class that serves its `engine` (video_worker_logic.PROFILES).

Speaks the worker protocol the Core's ModalRunner already uses
(packages/core/src/media/runners/gpu_worker.py, docs/MEDIA_ENGINES.md §9):

  POST   /v1/jobs       {"job": RunnerJob.payload(), "limits": {"max_gpu_seconds"}} -> {"id"}
  GET    /v1/jobs/{id}  -> {"status": queued|running|completed|failed|cancelled, "output"|"error"}
  DELETE /v1/jobs/{id}  -> {"status": "cancelled"}      (the Core cancels what it stopped waiting for)
  GET    /v1/health     -> 200
  Authorization: Bearer <WORKER_TOKEN>  (Modal secret "trindatron-video-worker")

Shape, and why:
  * one GPU class per engine: weights load once per container (@modal.enter), then every
    job is inference only — 8.7 s for 5 s of 480p and 16.2 s for 5 s of 720p on the 5B (H100),
    56.8 s for 5 s of 720p on the A14B (H200), measured warm;
  * warm while in use, zero when idle: min_containers=0, scaledown_window (default 120 s);
  * a bounded queue: jobs are Modal spawns; at most MAX_CONTAINERS GPUs per class run at once and
    the rest wait in Modal's queue, where the Core's cold-start timeout cancels them;
  * nothing runs above its reservation: validate() refuses a clip whose predicted GPU seconds
    exceed limits.max_gpu_seconds before the GPU is touched, and the per-job timeout bounds
    a run that goes wrong; the Core still settles min(real cost, reservation);
  * the clip comes back as base64 in the job's output and the Core stores it in its own storage
    (R2) — the worker holds no storage credentials;
  * metrics for cost and time on every output: queue, cold start, inference, encode and GPU
    seconds, the GPU, the container, the engine and the job's size.

  Weights (CPU only, once):  modal run infra/modal/video_worker.py::prefetch
                             modal run infra/modal/video_worker.py::prefetch_a14b
                             modal run infra/modal/video_worker.py::prefetch_i2v
  One real Maximum clip on an ephemeral app (nothing deployed):
                             modal run infra/modal/video_worker.py::smoke_a14b
  Try it isolated:           modal serve infra/modal/video_worker.py
  Deploy:                    modal deploy infra/modal/video_worker.py
"""
from __future__ import annotations

import base64
import hmac
import os
import sys
import time
from pathlib import Path

import modal
import modal.experimental  # stop_fetching_inputs: retire a container whose executor broke

sys.path.insert(0, str(Path(__file__).parent))
import video_worker_logic as logic  # noqa: E402

APP_NAME = os.environ.get("TRINDATRON_VIDEO_WORKER_APP", "trindatron-video-worker")
GPU = "H100"
A14B_GPU = "H200"  # 76 GB peak without offload (measured); an H100 needs CPU offload and runs far slower
MAX_CONTAINERS = int(os.environ.get("TRINDATRON_VIDEO_MAX_CONTAINERS", "2"))
SCALEDOWN_SECONDS = int(os.environ.get("TRINDATRON_VIDEO_SCALEDOWN_SECONDS", "120"))
JOB_TIMEOUT_SECONDS = 180       # a warm 720p 6 s clip takes ~20 s on the 5B; anything near this is broken
A14B_JOB_TIMEOUT_SECONDS = 300  # a warm 720p 5 s Maximum clip takes ~57 s
STARTUP_TIMEOUT_SECONDS = 900   # image pull + weights from the volume (~100 s measured)
FASTVIDEO_VERSION = os.environ.get("TRINDATRON_FASTVIDEO_VERSION", "0.2.1")  # resolved by the first build, 2026-09-26

app = modal.App(APP_NAME)
weights = modal.Volume.from_name(os.environ.get("TRINDATRON_VIDEO_WEIGHTS_VOLUME", "trindatron-video-weights"),
                                 create_if_missing=True)
jobs = modal.Dict.from_name("trindatron-video-jobs", create_if_missing=True)
secret = modal.Secret.from_name("trindatron-video-worker")
ENV = {"HF_HOME": "/weights/hf", "HF_HUB_ENABLE_HF_TRANSFER": "1", "TOKENIZERS_PARALLELISM": "false"}

gpu_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "git", "libgl1", "libglib2.0-0")
    .pip_install(f"fastvideo=={FASTVIDEO_VERSION}" if FASTVIDEO_VERSION else "fastvideo",
                 "huggingface_hub[hf_transfer]", "imageio[ffmpeg]", "numpy")
    .env(ENV)
    .add_local_python_source("video_worker_logic")
)
# Maximum runs on Diffusers (WanPipeline + the two Lightning LoRA adapters), the stack it was measured on.
a14b_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install("torch", "diffusers>=0.35", "transformers", "accelerate", "peft", "ftfy", "sentencepiece",
                 "huggingface_hub[hf_transfer]", "imageio[ffmpeg]", "numpy")
    .env(ENV)
    .add_local_python_source("video_worker_logic")
)
web_image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]").add_local_python_source(
    "video_worker_logic")


@app.function(image=gpu_image, volumes={"/weights": weights}, timeout=3600, cpu=4, memory=16384)
def prefetch() -> dict:
    """Download the checkpoint into the volume once (CPU only; no GPU second is spent)."""
    from huggingface_hub import snapshot_download

    started = time.time()
    path = snapshot_download(logic.MODEL)
    weights.commit()
    return {"model": logic.MODEL, "path": path, "seconds": round(time.time() - started, 1)}


@app.function(image=a14b_image, volumes={"/weights": weights}, timeout=3600, cpu=4, memory=8192)
def prefetch_a14b() -> dict:
    """The A14B checkpoint and the Lightning LoRA into the volume once (CPU only)."""
    from huggingface_hub import hf_hub_download, snapshot_download

    started = time.time()
    snapshot_download(logic.A14B_MODEL)
    for name in ("high_noise_model.safetensors", "low_noise_model.safetensors"):
        hf_hub_download(logic.A14B_LORA, f"{logic.A14B_LORA_DIR}/{name}")
    weights.commit()
    return {"model": logic.A14B_MODEL, "lora": logic.A14B_LORA_DIR, "seconds": round(time.time() - started, 1)}


@app.function(image=a14b_image, volumes={"/weights": weights}, timeout=3600, cpu=4, memory=8192)
def prefetch_i2v() -> dict:
    """Photo → Movement: the I2V-A14B checkpoint and its Lightning LoRA into the volume once (CPU only)."""
    from huggingface_hub import hf_hub_download, snapshot_download

    started = time.time()
    snapshot_download(logic.I2V_MODEL)
    for name in ("high_noise_model.safetensors", "low_noise_model.safetensors"):
        hf_hub_download(logic.A14B_LORA, f"{logic.I2V_LORA_DIR}/{name}")
    weights.commit()
    return {"model": logic.I2V_MODEL, "lora": logic.I2V_LORA_DIR, "seconds": round(time.time() - started, 1)}


def _gpu_name() -> str:
    import subprocess

    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _generate(worker, job: dict, limits: dict | None, analyze: bool) -> dict:
    """One job on a loaded GPU class: validate, render, measure. Shared by every class."""
    import tempfile

    call_id = modal.current_function_call_id()
    began = time.time()
    record = jobs.get(call_id) or {}
    jobs[call_id] = {**record, "status": "running", "started": began}
    queue_seconds = round(began - record["submitted"], 2) if record.get("submitted") else None
    cold = worker.cold_start_seconds if worker.served == 0 else 0.0
    worker.served += 1
    try:
        plan = logic.validate(job, limits)
    except logic.JobRefused as refused:
        return {"error": refused.as_error()}
    if plan["engine"] != worker.engine:
        return {"error": {"kind": logic.REJECTED, "message": f"{plan['engine']} is not loaded on this class"}}

    out_dir = tempfile.mkdtemp(prefix="clip-")
    try:
        t0 = time.time()
        path = worker.render(plan, out_dir)
        generated = time.time()
        if not path:
            return {"error": {"kind": logic.TEMPORARY, "message": "no video was written"}}
        data = Path(path).read_bytes()
    except Exception as exc:  # noqa: BLE001 — classify, never leak a traceback to the Core
        text = f"{type(exc).__name__}: {exc}"
        kind = logic.OOM if "out of memory" in text.lower() else logic.TEMPORARY
        # A failed generation can leave the pipeline broken (seen with FastVideo after a mid-run cancel):
        # this container takes no further job and retires; the next job gets a fresh one.
        modal.experimental.stop_fetching_inputs()
        return {"error": {"kind": kind, "message": text[:300]}}
    finished = time.time()
    metrics = {
        "queue_seconds": queue_seconds, "cold_start_seconds": cold,
        "inference_seconds": round(generated - t0, 2), "encode_seconds": round(finished - generated, 2),
        # What the job occupied the GPU for: validation, inference and the mp4, not the weights' load.
        "gpu_seconds": round(finished - began, 2), "predicted_seconds": plan["predicted_seconds"],
        "gpu": worker.gpu, "container": worker.container, "container_job": worker.served,
        "engine": plan["engine"], "width": plan["width"], "height": plan["height"], "frames": plan["frames"],
        "steps": plan["steps"],
    }
    if analyze:
        import imageio.v2 as imageio

        with imageio.get_reader(path) as reader:
            metrics["motion_by_second"] = logic.motion_by_second(reader, plan["fps"])
    return {"content_type": "video/mp4", "data_base64": base64.b64encode(data).decode("ascii"),
            "width": plan["width"], "height": plan["height"], "duration_seconds": plan["seconds"],
            "metrics": metrics}


@app.cls(image=gpu_image, gpu=GPU, volumes={"/weights": weights}, secrets=[secret],
         min_containers=0, max_containers=MAX_CONTAINERS, scaledown_window=SCALEDOWN_SECONDS,
         timeout=JOB_TIMEOUT_SECONDS, startup_timeout=STARTUP_TIMEOUT_SECONDS)
class Wan5B:
    engine = logic.ENGINE

    @modal.enter()
    def load(self) -> None:
        from fastvideo import VideoGenerator

        started = time.time()
        self.generator = VideoGenerator.from_pretrained(logic.MODEL, num_gpus=1)
        # One tiny clip so the first real job runs at warm speed (measured: 14.4 s cold-first vs 9.5 s warm
        # for 6 s of 480p). Counted as cold start, never billed to a job.
        self.generator.generate_video("warm-up", height=480, width=832, num_frames=33, num_inference_steps=logic.STEPS,
                                      fps=logic.FPS, seed=0, output_path="/tmp/warmup", save_video=False)
        self.cold_start_seconds = round(time.time() - started, 2)
        self.served = 0
        self.gpu = _gpu_name() or GPU
        self.container = os.environ.get("MODAL_TASK_ID", "")

    def render(self, plan: dict, out_dir: str) -> str:
        import glob

        self.generator.generate_video(plan["prompt"], height=plan["height"], width=plan["width"],
                                      num_frames=plan["frames"], num_inference_steps=plan["steps"],
                                      fps=plan["fps"], seed=plan["seed"], output_path=out_dir, save_video=True)
        files = glob.glob(os.path.join(out_dir, "**", "*.mp4"), recursive=True)
        return files[0] if files else ""

    @modal.method()
    def generate(self, job: dict, limits: dict | None = None, analyze: bool = False) -> dict:
        return _generate(self, job, limits, analyze)


@app.cls(image=a14b_image, gpu=A14B_GPU, volumes={"/weights": weights}, secrets=[secret],
         min_containers=0, max_containers=MAX_CONTAINERS, scaledown_window=SCALEDOWN_SECONDS,
         timeout=A14B_JOB_TIMEOUT_SECONDS, startup_timeout=STARTUP_TIMEOUT_SECONDS, memory=98304)
class WanA14B:
    engine = logic.A14B_ENGINE

    @modal.enter()
    def load(self) -> None:
        import torch
        from diffusers import FlowMatchEulerDiscreteScheduler, WanPipeline

        started = time.time()
        pipe = WanPipeline.from_pretrained(logic.A14B_MODEL, torch_dtype=torch.bfloat16)
        pipe.load_lora_weights(logic.A14B_LORA, weight_name=f"{logic.A14B_LORA_DIR}/high_noise_model.safetensors",
                               adapter_name="lightning")
        pipe.load_lora_weights(logic.A14B_LORA, weight_name=f"{logic.A14B_LORA_DIR}/low_noise_model.safetensors",
                               adapter_name="lightning_2", load_into_transformer_2=True)
        pipe.set_adapters(["lightning", "lightning_2"], adapter_weights=[1.0, 1.0])
        pipe.scheduler = FlowMatchEulerDiscreteScheduler.from_config(pipe.scheduler.config,
                                                                     shift=logic.A14B_SCHEDULER_SHIFT)
        pipe.to("cuda")  # no CPU offload: the H200 holds all of it (76 GB peak)
        # A short clip so the first real job runs warm; counted as cold start, never billed to a job.
        pipe("warm-up", height=720, width=1280, num_frames=17, num_inference_steps=logic.A14B.steps,
             guidance_scale=logic.A14B.guidance, guidance_scale_2=logic.A14B.guidance,
             generator=torch.Generator("cuda").manual_seed(0))
        self.pipe = pipe
        self.cold_start_seconds = round(time.time() - started, 2)
        self.served = 0
        self.gpu = _gpu_name() or A14B_GPU
        self.container = os.environ.get("MODAL_TASK_ID", "")

    def render(self, plan: dict, out_dir: str) -> str:
        import torch
        from diffusers.utils import export_to_video

        frames = self.pipe(plan["prompt"], height=plan["height"], width=plan["width"], num_frames=plan["frames"],
                           num_inference_steps=plan["steps"], guidance_scale=plan["guidance"],
                           guidance_scale_2=plan["guidance"],
                           generator=torch.Generator("cuda").manual_seed(plan["seed"])).frames[0]
        path = os.path.join(out_dir, "clip.mp4")
        export_to_video(frames, path, fps=plan["fps"])
        return path

    @modal.method()
    def generate(self, job: dict, limits: dict | None = None, analyze: bool = False) -> dict:
        return _generate(self, job, limits, analyze)


@app.cls(image=a14b_image, gpu=A14B_GPU, volumes={"/weights": weights}, secrets=[secret],
         min_containers=0, max_containers=MAX_CONTAINERS, scaledown_window=SCALEDOWN_SECONDS,
         timeout=A14B_JOB_TIMEOUT_SECONDS, startup_timeout=STARTUP_TIMEOUT_SECONDS, memory=98304)
class WanI2V:
    """Photo → Movement: the person's photo is the first frame (Wan2.2-I2V-A14B + Lightning I2V, H200)."""

    engine = logic.I2V_ENGINE

    @modal.enter()
    def load(self) -> None:
        import torch
        from diffusers import FlowMatchEulerDiscreteScheduler, WanImageToVideoPipeline
        from PIL import Image

        started = time.time()
        pipe = WanImageToVideoPipeline.from_pretrained(logic.I2V_MODEL, torch_dtype=torch.bfloat16)
        pipe.load_lora_weights(logic.A14B_LORA, weight_name=f"{logic.I2V_LORA_DIR}/high_noise_model.safetensors",
                               adapter_name="lightning")
        pipe.load_lora_weights(logic.A14B_LORA, weight_name=f"{logic.I2V_LORA_DIR}/low_noise_model.safetensors",
                               adapter_name="lightning_2", load_into_transformer_2=True)
        pipe.set_adapters(["lightning", "lightning_2"], adapter_weights=[1.0, 1.0])
        pipe.scheduler = FlowMatchEulerDiscreteScheduler.from_config(pipe.scheduler.config,
                                                                     shift=logic.A14B_SCHEDULER_SHIFT)
        pipe.to("cuda")
        # A short clip from a flat frame so the first real job runs warm; cold start, never billed to a job.
        pipe(image=Image.new("RGB", (1280, 720), (128, 128, 128)), prompt="warm-up", height=720, width=1280,
             num_frames=17, num_inference_steps=logic.I2V.steps, guidance_scale=logic.I2V.guidance,
             guidance_scale_2=logic.I2V.guidance, generator=torch.Generator("cuda").manual_seed(0))
        self.pipe = pipe
        self.cold_start_seconds = round(time.time() - started, 2)
        self.served = 0
        self.gpu = _gpu_name() or A14B_GPU
        self.container = os.environ.get("MODAL_TASK_ID", "")

    def render(self, plan: dict, out_dir: str) -> str:
        import torch
        from diffusers.utils import export_to_video

        first_frame = logic.fit_image(plan["source"], plan["width"], plan["height"])
        frames = self.pipe(image=first_frame, prompt=plan["prompt"], height=plan["height"], width=plan["width"],
                           num_frames=plan["frames"], num_inference_steps=plan["steps"],
                           guidance_scale=plan["guidance"], guidance_scale_2=plan["guidance"],
                           generator=torch.Generator("cuda").manual_seed(plan["seed"])).frames[0]
        path = os.path.join(out_dir, "clip.mp4")
        export_to_video(frames, path, fps=plan["fps"])
        return path

    @modal.method()
    def generate(self, job: dict, limits: dict | None = None, analyze: bool = False) -> dict:
        return _generate(self, job, limits, analyze)


# Which GPU class runs which engine; anything else is the 5B (whose own validation refuses what it does not serve).
_CLASSES = {logic.A14B_ENGINE: lambda: WanA14B, logic.I2V_ENGINE: lambda: WanI2V}


def _class_for(engine: str | None):
    return _CLASSES.get(engine, lambda: Wan5B)()


@app.function(image=web_image, secrets=[secret], min_containers=0, scaledown_window=60, timeout=120)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException

    web = FastAPI(title="TRINDATRON video worker", docs_url=None, redoc_url=None, openapi_url=None)
    token = os.environ["WORKER_TOKEN"]

    def authorize(authorization: str | None) -> None:
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @web.get("/v1/health")
    def health(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"ok": True, "engines": {logic.ENGINE: GPU, logic.A14B_ENGINE: A14B_GPU, logic.I2V_ENGINE: A14B_GPU}}

    @web.post("/v1/jobs")
    def submit(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        job, limits = body.get("job"), body.get("limits") or {}
        try:
            logic.validate(job, limits)  # refuse here too: a refused job never waits for a GPU
        except logic.JobRefused as refused:
            raise HTTPException(status_code=422, detail=refused.as_error())
        call = _class_for(job.get("engine"))().generate.spawn(job, limits, bool(body.get("analyze")))
        jobs[call.object_id] = {"status": "queued", "submitted": time.time()}
        return {"id": call.object_id}

    @web.get("/v1/jobs/{job_id}")
    def status(job_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if record.get("status") == "cancelled":
            return {"status": "cancelled"}
        try:
            result = modal.FunctionCall.from_id(job_id).get(timeout=0)
        except modal.exception.FunctionTimeoutError:
            # Before the generic TimeoutError below: it subclasses it, and a job past its own
            # timeout must read as failed, never as still running.
            return {"status": "failed", "error": {"kind": logic.TIMEOUT, "message": "job ran past its timeout"}}
        except (TimeoutError, modal.exception.TimeoutError):
            return {"status": record.get("status", "queued")}  # no output yet
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": {"kind": logic.TEMPORARY, "message": type(exc).__name__}}
        if isinstance(result, dict) and result.get("error"):
            return {"status": "failed", "error": result["error"]}
        return {"status": "completed", "output": result}

    @web.delete("/v1/jobs/{job_id}")
    def cancel(job_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown job")
        try:
            # A job already on a GPU takes its container with it: interrupting FastVideo mid-run broke every
            # later job on that container (2026-09-26). A queued job is simply removed.
            modal.FunctionCall.from_id(job_id).cancel(terminate_containers=record.get("status") == "running")
        except Exception:  # noqa: BLE001 — already finished: nothing left to stop
            pass
        jobs[job_id] = {**record, "status": "cancelled"}
        return {"status": "cancelled"}

    return web


@app.local_entrypoint()
def smoke_a14b(prompt: str = ("A yellow vintage tram climbs a steep cobblestone street in Lisbon at golden hour, "
                              "warm sunlight between pastel facades, slow cinematic camera push-in"),
               out: str = "maximum-smoke.json", seconds: int = 5, max_gpu_seconds: float = 120.0) -> None:
    """One real Maximum clip through the same job contract the Core sends, on an ephemeral app (no deploy).
    Writes the worker's full output (the Core's RunnerResult input) to `out`, and the clip next to it."""
    import json

    job = {"engine": logic.A14B_ENGINE, "task": "text_to_video", "prompt": prompt, "width": 1280, "height": 720,
           "seed": 1234, "duration_seconds": seconds}
    started = time.time()
    result = WanA14B().generate.remote(job, {"max_gpu_seconds": max_gpu_seconds}, True)
    result["wall_seconds"] = round(time.time() - started, 1)
    Path(out).write_text(json.dumps(result), encoding="utf-8")
    if result.get("error"):
        raise SystemExit(f"failed: {result['error']}")
    Path(out).with_suffix(".mp4").write_bytes(base64.b64decode(result["data_base64"]))
    print(json.dumps({k: v for k, v in result.items() if k != "data_base64"}, indent=1))


@app.local_entrypoint()
def smoke_i2v(image: str, prompt: str = "The scene comes alive: gentle natural motion, soft light shifting, slow camera push-in",
              out: str = "photo-movement-smoke.json", width: int = 1280, height: int = 720, seconds: int = 5,
              max_gpu_seconds: float = 120.0) -> None:
    """One real Photo → Movement clip from a local photo, through the job contract the Core sends (no deploy).
    Writes the worker's full output to `out`, and the clip next to it."""
    import json
    import mimetypes

    content_type = mimetypes.guess_type(image)[0] or "image/jpeg"
    job = {"engine": logic.I2V_ENGINE, "task": "image_to_video", "prompt": prompt, "width": width, "height": height,
           "seed": 1234, "duration_seconds": seconds,
           "source": {"content_type": content_type, "data_base64": base64.b64encode(Path(image).read_bytes()).decode()}}
    started = time.time()
    result = WanI2V().generate.remote(job, {"max_gpu_seconds": max_gpu_seconds}, True)
    result["wall_seconds"] = round(time.time() - started, 1)
    Path(out).write_text(json.dumps(result), encoding="utf-8")
    if result.get("error"):
        raise SystemExit(f"failed: {result['error']}")
    Path(out).with_suffix(".mp4").write_bytes(base64.b64decode(result["data_base64"]))
    print(json.dumps({k: v for k, v in result.items() if k != "data_base64"}, indent=1))
