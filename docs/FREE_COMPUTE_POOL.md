# Free Compute Pool

Free inference quota used first, never depended on. Code: `packages/model-router/src/trinity_router/free_pool.py`
and the router hooks in `router.py` (`_with_free_prefix`, `_free_blocked`, `_free_on_error`, `_free_on_success`).

## Research (2026-09-27)

Criteria, in order: cost, quality, reliability, licence/terms, scale. A source is used only when its free allowance
is a normal, recurring feature of the account, reached through the provider's own API, allowed for this use, and
**the data is not used for training** — TRINDATRON's paying customers' conversations never feed someone's model.

| Source | Free allowance | Data | Verdict |
|---|---|---|---|
| **Groq** (gpt-oss-120b, gpt-oss-20b, qwen) | per model: 30 req/min, 1,000 req/day, 8,000 tokens/min, 200,000 tokens/day; `x-ratelimit-*` headers | not retained by default, not trained on | **Used.** TRINDATRON's account is on this tier (confirmed from its headers) |
| **Cerebras** (gpt-oss-120b) | 1,000,000 tokens/day, ~5 req/min, context held to 8,192 | not stored, logged or reused | **Integrated, off until `CEREBRAS_API_KEY` is set** |
| **Cloudflare Workers AI** (`@cf/openai/gpt-oss-120b`) | 10,000 Neurons/day per account, reset 00:00 UTC (≈ US$ 0.11 at US$ 0.35 / 0.75 per 1M tokens), 300 req/min; called on the official OpenAI-compatible endpoint `/accounts/{id}/ai/v1` | not used for training | **Integrated as the 3rd source, off until `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` are set.** Text only (tools stay with Groq/Cerebras until verified live). A "daily allocation of Neurons" answer keeps it out until 00:00 UTC; a busy minute, one minute |
| Modal | US$ 30/month compute credits (Starter) | own workers | Already in use: the video worker's GPU seconds come out of it first |
| Google Gemini free tier | generous | **used to improve Google products** | Rejected |
| Mistral free "Experiment" | ~1B tokens/month | **trains by default** (opt-out) | Rejected |
| OpenRouter `:free` | 50 req/day (1,000 after a US$ 10 top-up), 20 req/min | depends on the host behind each model | Rejected |
| GitHub Models, NVIDIA API catalog | small | — | Rejected: terms limit them to prototyping |
| SambaNova free | 20 req/day | not published | Rejected: too small, data terms unclear |

Never used: several accounts to multiply a quota, scraping, or anything a provider's terms forbid.

gpt-oss-120b is OpenAI's open-weight (Apache-2.0) 120B reasoning model: a step above gpt-4o-mini / mistral-small /
gpt-oss-20b for reasoning and code, below Claude Sonnet/Opus. All three sources serve it.

## Architecture

```
request → router picks its chain (plan, capability, complexity) — unchanged
        → free prefix: free sources at least as good as the chain's first model, able to serve the request
             (quality class, tools, context, plain text); never for a forced model
        → each free source: quota check (local count vs published allowance × 0.9, sliding minute, UTC day,
             dollar allowance) and cooldown → skip at no cost
        → call: 429/quota → cool down (retry-after | 1 min | next UTC day), breaker untouched, next source;
                any other failure → breaker records it, next source; never an error to the person
        → the paid chain, exactly as before (its failover allowance untouched by the free prefix)
```

- **Quality first:** a free model only stands in for a model it is at least as good as. For PRO/MAX a simple request
  is already routed to a low-class model by the router's own complexity ordering, so the free model answers those; a
  request the router sends to Claude never sees a free model.
- **Billing unchanged:** a free answer is billed to the plan exactly as the model it stood in for. Plans and prices
  do not move; the saving is the company's.
- **Tracked chain models:** gpt-oss-20b on Groq (START's first choice) runs on the same free tier. It stays where it
  was, but a 429 now cools it down instead of opening its circuit breaker.
- **Kill switch:** `FREE_POOL_ENABLED=false`. Per source: remove its credentials, or disable its key through the
  existing runtime provider switch.
- **State:** in memory per process, like the circuit breaker. Processes that overdraw a source together get a 429,
  handled the same way. A shared (Redis) tracker is the next step if the Core runs on several instances.

## Capacity for 500 paying users

Free allowances are small next to 500 active users (≈ 500 × 20 turns × ~1.5k tokens ≈ 15M tokens/day):

| Source | Tokens/day free |
|---|---|
| Groq gpt-oss-120b | 200,000 (+200,000 on gpt-oss-20b, already START's model) |
| Cerebras | 1,000,000 |
| Cloudflare | ≈ 150,000–300,000 (US$ 0.11 of compute) |

≈ 1.5–1.7M tokens/day: roughly 10% of the traffic of simple, low-class requests, served at zero marginal cost; the
rest goes to the paid chain as before. The pool lowers the bill; it never carries the service.
