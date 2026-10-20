# TRINDATRON — Local AI lab (llama.cpp, Ollama, Whisper, Crawl4AI, RuFlo)

Evaluated 2026-09-18 on the development laptop. **Lab only**: nothing here is a Core
dependency, a Model Router provider, a registered voice provider or a user-facing
capability. Binaries, models and virtualenvs live in `C:\TRINITY-LAB` (outside Git).
Open source is not free to run: every entry carries CPU/RAM/disk/maintenance cost.

Machine: Intel i7-1255U (10 cores / 12 threads, AVX2), 11.7 GB RAM, Intel Iris Xe (no
CUDA), 54 GB free disk. Everything below ran **CPU-only**.

## Versions pinned

| Component | Version / pin | Artifact (sha256 verified) | License |
|---|---|---|---|
| llama.cpp | v0.4.1 = build b10964, commit b29c606e | `llama-b10964-bin-win-cpu-x64.zip` 18.4 MB, `917f39c0…` | MIT |
| Model | Qwen2.5-0.5B-Instruct Q4_K_M | `qwen2.5-0.5b-instruct-q4_k_m.gguf` 491 MB, `74a4da8c…` | Apache-2.0 |
| Ollama | v0.34.2 (portable zip, no installer/service) | `ollama-windows-amd64.zip` 1.46 GB, `8f3fd071…` | MIT |
| whisper.cpp | v1.9.4 = build b5130, commit 927cfce3 | `whisper-bin-x64.zip` 8.6 MB, `f9ec6c52…` | MIT |
| Whisper model | ggml small, q5_1 | `ggml-small-q5_1.bin` 190 MB, `ae85e4a9…` | MIT |
| Crawl4AI | 0.9.3 (isolated venv, no browser) | PyPI wheel; venv 552 MB, 98 packages | Apache-2.0 **+ attribution clause** |
| RuFlo | ruflo 3.42.4 + @claude-flow/cli 3.42.4 (override) | npm, `--ignore-scripts --omit=optional`, 115 MB | MIT |

## Measured (smoke tests)

| Component | Result |
|---|---|
| llama.cpp | bench: 206 t/s prompt, 57.5 t/s generation (8 threads); server ready in ~1.2 s; one PT-BR answer in 1.47 s; ~528 MB RAM; listens on 127.0.0.1 only |
| Ollama | same GGUF imported (no second download); 3.0 s cold / 1.5 s warm; runs its own llama-server child on a random loopback port; `OLLAMA_NO_CLOUD=1`, iGPU ignored by default; 1.8 GB on disk (mostly CUDA libs unused here) |
| whisper.cpp | 30 s of PT-BR speech: 13.9 s (CLI), 16 s WAV / 22 s WebM via server (`--convert`, ffmpeg); language detected `portuguese`; ~407 MB RAM |
| Crawl4AI | used **only as an offline extractor**: example.com fetched by our guarded client in 0.53 s, extracted in 2.3 s; worker peak ~110 MB; sockets disabled in the worker |
| RuFlo | swarm init → supervisor + 2 agents → task → assign → status → stop: all exit 0 in ~2 s per command; **agents are records only** (task stayed `in_progress` 0%: execution needs an LLM via Claude Code/MCP or a configured provider); 0 outbound connections observed |

Quality note: the 0.5B model answers simple PT-BR prompts but fails trivial instructions;
it proves the pipeline, not usable quality. A 1.5B–3B model (1.1–2 GB) is the next step
for a real benchmark.

## How to run (all loopback)

```
C:\TRINITY-LAB\llama.cpp\llama-server.exe -m C:\TRINITY-LAB\models\qwen2.5-0.5b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8181 -t 8 -c 4096 --no-webui
set OLLAMA_HOST=127.0.0.1:11434 & set OLLAMA_NO_CLOUD=1 & set OLLAMA_MODELS=C:\TRINITY-LAB\ollama-models & C:\TRINITY-LAB\ollama\ollama.exe serve
C:\TRINITY-LAB\whisper.cpp\Release\whisper-server.exe -m C:\TRINITY-LAB\models\ggml-small-q5_1.bin --host 127.0.0.1 --port 8282 -t 8 --convert
```

Never bind to `0.0.0.0`, never open the firewall, never set `OLLAMA_ORIGINS=*`.

## Core side (isolated, off by default, removable)

| Module | What it is | Switch (empty = off) |
|---|---|---|
| `src/local_runtime/llm.py` | `LocalLLMClient`: OpenAI-style chat against llama.cpp or Ollama; **loopback URLs only**; returns usage and tokens/s for pricing | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` |
| `src/voice/adapters/whisper_local.py` | `WhisperLocalVoiceProvider` on the existing `VoiceProvider` contract; loopback only; **not registered** | `WHISPER_LOCAL_BASE_URL` (future) |
| `src/capability/web_research/` | guard (URL policy + connect-time public-IP check), guarded fetch (manual redirects, robots.txt, size/type/time limits), extractor interface, Crawl4AI worker, `research()` | `WEB_RESEARCH_CRAWL4AI_PYTHON` |

None is imported by the Model Router, the Quality Gate, billing, entitlements or the voice
registry. Deleting any of them breaks nothing else (tests assert this).

### Web research security

- URL: http/https, ports 80/443, hostname only (no literal IPs), no credentials, no
  `localhost`/`.local`/`.internal`/single-label names, ≤ 2048 chars.
- Connect time: every connection resolves DNS itself and refuses unless **every** address
  is global (private, loopback, link-local incl. 169.254.169.254, CGNAT, multicast,
  IPv4-mapped/Teredo refused), then connects to the checked address (defeats DNS rebinding
  and redirects to names pointing inside). Verified live: `localtest.me` (→127.0.0.1) blocked.
- Redirects followed by hand (≤ 3), each re-checked; robots.txt honoured; only
  text/html, xhtml, text/plain; ≤ 2 MB streamed; one overall deadline; no cookies, no
  proxies from the environment; logs carry the host only.
- Crawl4AI never touches the network: it runs in its own venv, as a child process with a
  minimal environment (no Core secrets), sockets disabled, a deadline and an output cap.
  Its browser, stealth and LLM features are not used.
- Output sanitised: control characters removed, `javascript:`/`data:`/`file:` links
  neutralised, length capped.

**Crawl4AI attribution (required by its license for any public use or distribution):**
"This product includes software developed by UncleCode (https://x.com/unclecode) as part of
the Crawl4AI project (https://github.com/unclecode/crawl4ai)." — before this capability
reaches users, add it to an About/Credits page.

## RuFlo — evaluation only

- Version: **3.42.4** pinned for both `ruflo` and `@claude-flow/cli` (npm `latest` =
  `alpha` = `v3alpha`: the v3 line is published as alpha). The 3.41.2 seen earlier was a
  stale view; repo tag v3.42.4 = npm 3.42.4. `SECURITY.md` still lists 3.5.x as supported
  (not maintained).
- Advisories: GHSA-c4hm-4h84-2cf3 (critical, unauthenticated RCE in the MCP bridge's default
  docker-compose deployment, < 3.16.3 — 3.42.4 is not affected). npm audit: 3 high via `toml` ≤ 4.1.2 (GHSA-82x6-q7mm-w9cf, GHSA-v5mp-jgw5-2x6j);
  **lab override `toml@4.3.0` → 0 vulnerabilities**, CLI still runs.
- Surface: 531 JS files; `child_process` in 79, `shell: true` in 7, runtime `npx -y`/`npm i -g`
  in 19, `--dangerously-skip-permissions` path (explicit flag only), `fetch` in 29, servers
  in 8 (federation host configurable), OS keychain, `~/.claude` config writes, IPFS/Pinata
  plugin registry, update checks; postinstall rewrites `agentdb` copies up to 12 parent
  directories (not run: `--ignore-scripts`, optionals omitted).
- Run only with `HOME/USERPROFILE/APPDATA` redirected to `C:\TRINITY-LAB\ruflo\home`, in
  `C:\TRINITY-LAB\ruflo\work`; never `init`, `mcp start`, `daemon`, `swarm start`,
  `hive-mind`, `plugins`, `update`. Its Ollama provider defaults to a **cloud** model.
- Verdict so far: coordination state is bookkeeping; execution, retries, memory and model
  choice sit outside TRINDATRON's billing/TCU/Quality Gate. Not better than the existing
  Execution Runtime on anything measured; stays in the lab.

## Not installed (by decision)

LangChain, LangGraph, Fooocus, Dify, Open WebUI — no proven need; LangGraph would duplicate
the Execution Runtime before it is measured against it.

## Removing everything

Stop the three servers, delete `C:\TRINITY-LAB`. In the repo: delete the three modules and
their two test files. Nothing else references them.

## CP6 — controlled integration (2026-09-18)

Three capabilities became real, modular and **off by default**. Ollama and RuFlo stay out of the Core.

| Capability | Where | Switch | Reaches users? | Chosen automatically? |
|---|---|---|---|---|
| `llm.local` | `src/local_runtime/provider.py` + `trinity_router.lab` entry `local-qwen2.5-0.5b` | `LOCAL_LLM_ENABLED` | no | **no** — the Model Router selects only from `PROVIDER_CATALOG`; the lab registry is unreachable (`tests/test_lab_isolation.py`) |
| `stt.local` | `src/voice/adapters/whisper_local.py` | `STT_LOCAL_ENABLED` | no | **no** — registered only when enabled, never in `STT_CAPABLE` (the fallback), and `voice/budget.py` has no cost bound for it, so even when selected it is left out until priced |
| `web_research` | `src/capability/web_research/capability.py` + `contract.py` | `WEB_RESEARCH_ENABLED` | no | no — nothing outside tests calls it |

Status in one place: `src/local_runtime/capabilities.py` (switches and health only — no engine, model,
path or address).

### Limits and safety added

- **llm.local**: loopback only; `LOCAL_LLM_MAX_TOKENS` cap; prompt + completion must fit
  `LOCAL_LLM_CONTEXT_TOKENS` (estimated at 3 chars/token, refused before sending); timeout; metrics
  (`local_llm_requests_total`, `local_llm_latency_ms`, `local_llm_tokens_total`); errors are a code only;
  the runtime's model identifier (may be a file path) is never passed on.
- **stt.local**: loopback only; formats checked by content type **and** by the audio's own signature
  (WAV/WebM/Ogg/M4A/MP3); size cap before sending; language code validated; one job at a time; timeout;
  fixed error messages; never reads a path, never runs a command — ffmpeg runs inside the server with its
  own fixed arguments; metrics (`stt_local_*`).
- **web_research**: stable contract (`WebResearchRequest` → `WebResearchResponse{pages, refused}` with a
  closed set of refusal codes); domain policy (`WEB_RESEARCH_BLOCKED_DOMAINS` always wins,
  `WEB_RESEARCH_ALLOWED_DOMAINS` restricts) applied to the first URL **and every redirect hop**;
  per-actor rate limit (in process — needs a shared store before multi-instance); page and character
  ceilings the request cannot raise; duplicates read once; metrics (`web_research_*`). Live check: a
  public page read; `localtest.me` → `blocked_destination`; a blocked domain → `domain_not_allowed`.

### Local LLM benchmark (`bench/local_llm_benchmark.py`, results in `bench/results/local_llm_benchmark.json`)

Local: Qwen2.5-0.5B Q4_K_M on llama.cpp (CPU, ~540 MB RAM, ~60 t/s). API: the cheapest catalog model,
`groq/openai/gpt-oss-20b` ($0.075/$0.30 per 1M). 10 PT-BR tasks, deterministic scoring. API spend
US$0.000436. Local compute priced at an assumed US$0.17/h (4 vCPU / 16 GB on-demand VM).

| Task | Local quality | API quality | Local latency | API latency | Local cost/task* | API cost/task | Recommendation |
|---|---|---|---|---|---|---|---|
| Classification | 1.00 | 1.00 | 0.42 s | 0.47 s | $0.000020 | $0.000024 | **economy-tier candidate** (fixed label sets; validate the label, it may add words) |
| Structured extraction | 1.00 | 1.00 | 0.95 s | 0.48 s | $0.000045 | $0.000047 | candidate **only with schema validation** (returned a number as a string, wrapped JSON in fences) |
| Transformation | 1.00 | 1.00 | 0.69 s | 0.35 s | $0.000033 | $0.000028 | candidate for mechanical transforms (dates, lists) with output validation |
| Short summary | 0.50 | 1.00 | 0.98 s | 0.62 s | $0.000046 | $0.000096 | **not** — ignores length limits, Spanish-isms |
| Simple PT-BR answer | 0.00 | 1.00 | 0.59 s | 0.39 s | $0.000028 | $0.000023 | **never** — answered "São Paulo" for the capital of Brazil |

\* Local cost is compute time at the assumed rate — the same order as the API. Local is only cheaper where
the hardware is already paid for and idle; it is never "free". Nothing is promoted: an economy tier would
need a bigger local model (1.5B–3B), output validators, and its own benchmark before any router change.

### Local STT benchmark (`bench/local_stt_benchmark.py`, results in `bench/results/local_stt_benchmark.json`)

Two 30 s PT-BR clips from a local recording; WER measured against the transcript the current default
provider (ElevenLabs Scribe) produced for the same recording — distance from today's provider, not
human-verified accuracy. Hosted Groq spend ~US$0.001 in total.

| Clip | Local WER | Groq WER | Local time | Groq time | Local $/min* | Groq $/min | ElevenLabs $/min |
|---|---|---|---|---|---|---|---|
| A | 9.1% | 8.8% | 10.6 s (RTF 0.35) | 1.6 s | $0.0010 | $0.00067 | $0.0037 |
| B | 16.0% | 14.1% | 16.0 s (RTF 0.53) | 1.6 s | $0.0015 | $0.00067 | $0.0037 |

Model `ggml-small-q5_1` (190 MB), ~370 MB RAM, CPU. Language detected `portuguese` both times.

- **Fallback**: yes — close to Groq on accuracy, works offline, no key; right when hosted STT is down or
  over budget, for short clips.
- **First option**: only where latency does not matter (batch/offline transcription) or data must not
  leave the machine. Not for live voice: 6–10x slower than Groq on this CPU.
- **Not good enough yet**: noisier or faster speech (clip B, +2 pts WER over Groq); nothing replaces a
  human-checked reference before a decision. And it is not cheaper than Groq at a rented-CPU rate.

No provider priority changed: `STT_CAPABLE` is still `("elevenlabs", "groq")`.
