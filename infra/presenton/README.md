# Presenton — the presentation engine service

Not deployed yet. This is the configuration a deploy would use, written down
so the decisions are reviewable before anything costs money.

Presenton runs as its own Railway service, separate from TRINITY CORE, reached
only over Railway's private network. It is never exposed publicly.

---

## 1. Why a separate service at all

The engine is a 5.45 GB image that runs a Next.js renderer, a Python API and a
headless browser for PDF export. Putting that inside the core API image would
triple core's deploy size and cold-start time for a capability most requests
never touch. Separate service, separate lifecycle, separate scaling.

---

## 2. The running decision: scale to zero

Four options were considered against the real usage shape — occasional decks,
each taking minutes.

| Option | Monthly cost shape | Latency | Verdict |
|---|---|---|---|
| **A. Always on** | Pays 24/7 for a service used minutes a day | Instant | Rejected — the cost is almost entirely idle |
| **B. Scale to zero** | Pays only while generating | Cold start on first request after idle | **Chosen** |
| **C. Start per job, tear down after** | Same as B, plus orchestration we would have to write | Cold start every time, including back-to-back decks | Rejected — B gets the same saving with none of the code |
| **D. Managed Presenton cloud** | Per-deck vendor pricing | Instant | Rejected — sends briefs and the model choice to a third party, and the mission requires self-hosted |

**Why the cold start is affordable here, and only here.** Generation is
already an async job: the user gets a job id immediately and collects the file
when it is done. Nobody watches a spinner. A cold start of tens of seconds
sits inside a task that takes minutes regardless, so it moves the total by a
modest fraction rather than changing what the product feels like.

The same wait inside an HTTP request/response cycle would be unacceptable.
That is precisely why the capability was built as a job first and the engine
made sleepable second — the architecture is what makes the cheap option
viable, not the other way round.

**How the wake is handled.** `PresentonProvider.wait_until_ready()` polls
`/api/v1/auth/status` until the container answers, bounded by
`PRESENTON_WAKE_TIMEOUT_S` (default 180s). A warm engine returns on the first
probe and costs nothing. If the engine never becomes ready the job is released
for retry rather than failed — a sleeping service is not a broken one.

---

## 3. Service configuration

| Setting | Value | Why |
|---|---|---|
| Image | `ghcr.io/presenton/presenton:latest` | Upstream official image |
| Port | **`80`** | The container listens on 80, not the 5001 its docs suggest — verified against the running image on 2026-09-11. Railway must target 80 |
| Health check | `GET /api/v1/auth/status` | Returns `{"configured": true}` when genuinely ready, unlike a bare TCP check |
| Volume | `/app_data`, 10 GB | Presenton keeps its SQLite database and working files here. Without a volume every deploy loses the engine's own state |
| Memory | 2 GB | The PDF path runs a headless browser; 1 GB is where that starts failing |
| CPU | 1 vCPU | Generation is I/O-bound on model calls, not CPU-bound |
| Networking | Private only | No public domain. Core reaches it at `http://presenton.railway.internal` (port 80) |
| Sleep | Enabled | See section 2 |

Generated decks do **not** live on this volume — they are uploaded to R2 by
core and the engine's copy is incidental. The volume exists for the engine's
own database, not as artifact storage.

---

## 4. Environment

Generate both sides instead of typing them — secrets come out as placeholders only:

```bash
cd packages/core && uv run python -m src.presentation.deploy_env
```


Produced in code by `src/presentation/presenton.engine_environment()` so it can
be asserted on in tests rather than living only in a dashboard. The
load-bearing entries:

| Variable | Value | Why |
|---|---|---|
| `LLM` | `custom` | Not `google` — see below |
| `CUSTOM_LLM_URL` | `http://trinity-ai-dev.railway.internal:8080/internal/presenton/llm/v1` | Core's gateway, not Google. This project's Core is the Railway service `TRINITY-AI-DEV` on Railway's default port 8080 — not `core.railway.internal:8000`, which this table used to say |
| `CUSTOM_LLM_API_KEY` | *(secret)* `PRESENTON_LLM_GATEWAY_TOKEN` | A token we mint and rotate |
| `CUSTOM_MODEL` | `models/gemini-3.8-flash` | Stated explicitly — never left to a default |
| `WEB_GROUNDING` | `true` | Model-native grounding |
| `WEB_SEARCH_PROVIDER` | `native` | No Tavily / Exa / Brave / Serper / SearXNG |
| `IMAGE_PROVIDER` | `pexels` | Stock-first is the default deployment |
| `DISABLE_IMAGE_GENERATION` | `true` | Generation is opt-in, not the default |
| `ENABLE_PARALLEL_IMAGE_GENERATION` | `false` | **Load-bearing** — see below |
| `PEXELS_API_KEY` | *(secret)* | Free tier: 200 req/hour, 20k/month |
| `CAN_CHANGE_KEYS` | `false` | The engine ships a UI that can rewrite provider keys |

### Why `LLM=custom` and not `LLM=google`

Pointing the engine straight at Google would mean handing a third-party
container our Gemini key, accepting whatever model its defaults pick, and
having no idea how many calls it made. Routing it through core's gateway
(`src/api/routes/presenton_gateway.py`) gives us:

- the Gemini key never leaves our side; the engine holds a token we can rotate
- the model is **forced** to `settings.presentation_model` — the engine's
  requested model is discarded, so "Gemini only" is enforced rather than
  merely configured
- every call's real token usage is recorded, so a deck is billed on
  measurement instead of estimate
- concurrency against the provider is bounded in one place

### The model-name trap

Three different things are called "Gemini Flash" in this stack, and none of
them is automatically the model we selected:

| Where | Default | What it actually is |
|---|---|---|
| Presenton `GOOGLE_MODEL` | `models/gemini-2.0-flash` | A two-generation-old text model |
| Presenton `IMAGE_PROVIDER=gemini_flash` | — | Google's **image** model, not the text one |
| TRINDATRON `presentation_model` | `models/gemini-3.8-flash` | What we actually chose |

This is why `CUSTOM_MODEL` is set explicitly and why the gateway overrides the
model on every request regardless. Neither `GOOGLE_API_KEY` nor `GOOGLE_MODEL`
is set on this service at all.

### Why parallel image generation is off

Measured during the audit: six concurrent image calls against the provider
returned **0/6** successes. The same six issued in sequence returned **5/6**.
The default is `true`. A rate limit is not overcome by asking faster.

---

## 5. Secrets

None of these belong in git. Set them in Railway's variable store.

| Secret | Service | Format | Notes |
|---|---|---|---|
| `PRESENTON_LLM_GATEWAY_TOKEN` | both core and presenton | random ≥32 chars | Same value on both; core compares it in constant time |
| `PRESENTON_API_KEY` | core | `sk-presenton-...` | Minted by the engine's admin endpoint. Preferred over username/password |
| `PEXELS_API_KEY` | presenton | provider key | Free tier |
| `GEMINI_API_KEY` | **core only** | provider key | Must never be set on the presenton service |
| `R2_ENDPOINT` | core | `https://<account-id>.r2.cloudflarestorage.com` | |
| `R2_BUCKET` | core | bucket name | Private bucket |
| `R2_ACCESS_KEY_ID` | core | R2 token id | Scope to this one bucket |
| `R2_SECRET_ACCESS_KEY` | core | R2 token secret | |

---

## 6. What a real end-to-end run proved (2026-09-11)

A 5-slide deck was generated for real against this exact configuration:
Presenton container -> core's gateway -> Gemini -> object storage.

| | |
|---|---|
| Result | `presentation.pptx`, 442,093 bytes, 5 slides, 36 embedded media, zip-valid |
| Duration | 67.5 s |
| Gemini calls | **10** (not the 6 an "outline + one per slide" model predicts) |
| Tokens | 12,852 in / 2,901 out |
| Measured cost | **$0.0205** (2.05 TCU) |
| Pre-flight estimate | $0.0714 (7.14 TCU) — ~3.5x conservative, which is the safe direction |
| Model used | `models/gemini-3.5-flash-lite` |

Two things that only a real run could have found, both now fixed:

**The engine streams.** It sends `stream=true`, `stream_options` and
`response_format=json_schema`. The gateway originally stripped `stream` to
keep usage totals visible, and the failure was silent and expensive: Google
answered 200 and billed 436 output tokens, the engine's SSE parser
accumulated nothing from a non-streamed body, and generation died with
`Expecting value: line 1 column 1` on an empty outline. The gateway now
proxies SSE verbatim and forces `include_usage` to keep the accounting.

**Thinking tokens hide in the total.** Google's OpenAI-compatible endpoint
reported `prompt_tokens=10, completion_tokens=0`, no reasoning field, and
`total_tokens=58`. Those 48 tokens are billed at the output rate; trusting
the explicit fields under-reported that call ~25x. The gateway reconstructs
the remainder from the total.

Note on the model: `models/gemini-3.8-flash` is the configured default, but
its free-tier daily quota was exhausted at the time of the run, so the run
used `gemini-3.5-flash-lite` — same provider, same gateway, same code path.
The configured model is unchanged; only this validation used a different one.

---

## 7. Still required before this can run

1. **Gemini billing (Tier 1, no activation fee).** The free tier is 20
   requests/day and one 10-slide deck needs ~11. This is a hard prerequisite,
   not a tuning issue — no amount of sequencing beats a daily cap.
2. **R2 bucket + scoped API token.** Until then core falls back to the local
   object store, which works but cannot mint signed URLs and does not survive
   a redeploy.
3. **A Pexels key**, if stock imagery is wanted at launch.
