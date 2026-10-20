import logging
from typing import ClassVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Known placeholder values that must never be used in production.
INSECURE_JWT_DEFAULTS: frozenset[str] = frozenset({
    "change-this-in-production",
    "MUDE-ISSO-EM-PRODUCAO",
    "MUDE-ISSO-EM-PRODUCAO-gere-um-segredo-real",
    "trinity-dev-secret-change-in-production",
    "",
})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "TRINITY CORE"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False

    # CP8 (test infrastructure stabilization): true only when tests/conftest.py sets
    # TESTING=true before importing this module — never a deployment concern, and refused
    # in production below. Two things key off it and nothing else does:
    #   * database.py's engine builder ignores DATABASE_URL and uses an isolated
    #     in-memory SQLite engine instead, so a test can never reach trinity_dev.db
    #     (or a real Postgres) no matter which code path opens a session;
    #   * main.py's lifespan skips every startup reconciliation call and every
    #     recurring background loop (execution worker, agent scheduler, payment order
    #     expiry, webhook nonce cleanup, entitlement reservation reclaim, artifact
    #     retention, media provider resume) — a TestClient boot becomes a minimal,
    #     deterministic lifespan instead of the full production one.
    testing: bool = False

    database_url: str = "sqlite:///./trinity_dev.db"

    # PostgreSQL connection pool — ignored for SQLite.
    # pool_size: idle connections kept alive in the pool.
    # max_overflow: extra connections allowed above pool_size (burst capacity).
    # pool_timeout: seconds to wait for a connection before raising OperationalError.
    # pool_recycle: recycle connections older than this (seconds) to prevent stale TCP.
    db_pool_size: int = 10
    db_max_overflow: int = 15
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""   # optional — ultra-cheap CHAT routing via Groq
    # "free" when the Groq organization is on the Free plan (it bills nothing inside its limits), "developer"
    # once it pays per token. Only changes recorded real spend; TCU stays at list price (billing/provider_billing.py).
    groq_billing_tier: str = "developer"

    jwt_secret_key: str = "change-this-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiration_minutes: int = 60 * 24

    max_tokens: int = 4096

    agent_workspace_root: str = "workspace/sessions"
    agent_max_iterations: int = 10

    # Security Hardening (SANDBOX-01, CRITICAL) — CodeWorkspace.root_path
    # (POST/PATCH /workspaces/code) is entirely client-chosen, with no
    # concept of per-user filesystem ownership like every other resource
    # in this codebase. "Point this at any host directory" only makes
    # sense for a single-user local/desktop deployment; in the shared,
    # multi-tenant hosted SaaS this app is launching as, any authenticated
    # user could otherwise browse/read anything the server process can
    # read (other projects, .env, credentials). Same "off unless a
    # deployment explicitly needs it" contract already used for optional
    # payment providers (NOWPAYMENTS_ENABLED/BINANCE_PAY_ENABLED) — false
    # everywhere, including production, unless a desktop/local deployment
    # deliberately opts in.
    code_workspace_arbitrary_root_enabled: bool = False
    # Repeated-action circuit breaker (root-cause fix, 2026-09-02): the
    # ReAct loop otherwise has NO way to notice the model is calling the
    # exact same tool with the exact same arguments over and over — only
    # the coarse, whole-task agent_max_iterations bound above stops it,
    # burning up to that many iterations (real TCU/tokens/time) before
    # giving a generic "unable to complete" answer indistinguishable from
    # a genuinely hard multi-step task. 2 = the SAME (tool, args) pair
    # failing twice (not merely being called twice — succeeding on a
    # retry never counts) ends the loop immediately with a specific,
    # diagnosable message instead of silently exhausting the iteration
    # budget. See agent_service.py's stream_agent() for where this is read.
    agent_repeated_action_failure_limit: int = 2
    # Per-tool no-progress budget (hardening, 2026-09-02): the exact-match
    # breaker above is keyed on (tool_name + args) and is deliberately
    # blind to a model that varies its arguments slightly on every retry
    # (tool X(args A) fails, X(args B) fails, X(args C) fails — never the
    # same signature twice, so the exact-match breaker never trips). This
    # is a SECOND, independent, deterministic counter keyed on tool_name
    # ONLY, counting that tool's CONSECUTIVE failures regardless of what
    # its arguments were — reset to 0 the moment that SAME tool succeeds
    # (a different tool succeeding does NOT reset it; one tool working
    # doesn't mean a different tool's problem is resolved). 3 = one more
    # than the exact-match limit, deliberately: it tolerates a single
    # "retry with different arguments" (healthy self-correction) before
    # concluding the model is not converging. Both breakers are purely
    # deterministic counters — no LLM call, no embedding, no semantic
    # comparison of arguments; see agent_service.py's stream_agent().
    agent_tool_failure_streak_limit: int = 3

    # Etapa 5I — Request Idempotency & Execution Recovery. A row still
    # "in_progress"/"admitted"/"running" older than this is treated as
    # orphaned (the process that owned it crashed or was killed before
    # reaching a terminal state) rather than a live concurrent request —
    # see idempotency_service.py/agent_execution_service.py's
    # reconciliation functions. Deliberately set ABOVE
    # agent_scheduler_execution_timeout_seconds (300s default): no
    # legitimate execution can still be genuinely in-flight past that
    # deadline (with_execution_timeout() already guarantees termination by
    # then), so anything older is unambiguously stale, never a false
    # positive against a slow-but-alive request.
    idempotency_stale_after_seconds: float = 360.0

    # Durable Execution Worker — see execution_worker.py's module docstring
    # for the full queue/lease/heartbeat/recovery model. All in-process
    # asyncio-task-based dispatch was replaced by this DB-backed worker;
    # these knobs are deliberately separate from
    # agent_scheduler_execution_timeout_seconds above (that one bounds a
    # single synchronous /agent HTTP request; these bound the durable
    # background worker, a completely different lifecycle).
    #
    # execution_worker_poll_interval_seconds: how often the worker looks
    # for claimable jobs. Short enough that a queued job doesn't sit idle
    # for long, long enough not to hammer Postgres with empty polls.
    execution_worker_poll_interval_seconds: float = 2.0
    # execution_worker_batch_size: max jobs claimed (and run concurrently)
    # per poll cycle, by this ONE worker instance — bounds this process's
    # own execution concurrency. Independent of, and not a replacement
    # for, Request Control / Agent Scheduler (those gate HTTP admission,
    # not background job execution — see agent_scheduler.py's own
    # docstring on why the three layers don't overlap).
    execution_worker_batch_size: int = 5
    # execution_lease_duration_seconds: the FLOOR lease length granted on
    # claim — the actual lease is max(this, job.timeout_seconds +
    # execution_lease_grace_seconds), so a job configured with a longer
    # per-job timeout is never pre-empted mid-step by the recovery sweep
    # (see _lease_seconds_for()). Reuses job.timeout_seconds rather than
    # inventing a second per-job duration field.
    execution_lease_duration_seconds: int = 120
    # execution_lease_grace_seconds: added on top of job.timeout_seconds
    # when deriving the lease length, to comfortably outlive the step's
    # own timeout (network jitter, DB commit latency) without the lease
    # racing the step's own deadline.
    execution_lease_grace_seconds: int = 30
    # execution_recovery_interval_seconds: how often the background sweep
    # reclaims jobs whose lease has expired (crashed/killed worker) — see
    # execution_worker.reclaim_expired_leases(). Also run once at startup.
    execution_recovery_interval_seconds: float = 30.0
    # execution_retry_backoff_base_seconds: base for the retry backoff
    # exponent (next_run_at = now + base * 2**(attempt-1)) — durable now
    # (persisted on the job row), not an in-process asyncio.sleep().
    execution_retry_backoff_base_seconds: float = 5.0

    @field_validator("execution_worker_poll_interval_seconds")
    @classmethod
    def _validate_execution_worker_poll_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("EXECUTION_WORKER_POLL_INTERVAL_SECONDS must be > 0")
        return v

    @field_validator("execution_worker_batch_size")
    @classmethod
    def _validate_execution_worker_batch_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("EXECUTION_WORKER_BATCH_SIZE must be >= 1")
        return v

    @field_validator("execution_lease_duration_seconds")
    @classmethod
    def _validate_execution_lease_duration(cls, v: int) -> int:
        if v < 10:
            raise ValueError("EXECUTION_LEASE_DURATION_SECONDS must be >= 10")
        return v

    @field_validator("execution_recovery_interval_seconds")
    @classmethod
    def _validate_execution_recovery_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("EXECUTION_RECOVERY_INTERVAL_SECONDS must be > 0")
        return v

    @field_validator("execution_retry_backoff_base_seconds")
    @classmethod
    def _validate_execution_retry_backoff_base(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("EXECUTION_RETRY_BACKOFF_BASE_SECONDS must be > 0")
        return v

    # Persistent Agent Runtime V1 — HttpCheckSource (see
    # agent_check_source.py). A schedule's own job_payload.check.timeout_seconds
    # may request a shorter timeout; this is both the default when it
    # doesn't and a hard ceiling regardless of what it requests, so a
    # misconfigured schedule can never stall the schedule poll loop for an
    # unbounded time.
    agent_http_check_default_timeout_seconds: float = 10.0
    agent_http_check_max_timeout_seconds: float = 30.0
    # Response bytes read before the connection is abandoned — a
    # fingerprint of typical page/API content needs nowhere near this
    # much; this bounds worst-case memory use per check, not accuracy.
    agent_http_check_max_response_bytes: int = 65536

    @field_validator("agent_http_check_default_timeout_seconds")
    @classmethod
    def _validate_agent_http_check_default_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("AGENT_HTTP_CHECK_DEFAULT_TIMEOUT_SECONDS must be > 0")
        return v

    @field_validator("agent_http_check_max_timeout_seconds")
    @classmethod
    def _validate_agent_http_check_max_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("AGENT_HTTP_CHECK_MAX_TIMEOUT_SECONDS must be > 0")
        return v

    @field_validator("agent_http_check_max_response_bytes")
    @classmethod
    def _validate_agent_http_check_max_response_bytes(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("AGENT_HTTP_CHECK_MAX_RESPONSE_BYTES must be > 0")
        return v

    # Persistent Agent Instance V1 — consecutive ExecutionJob failures
    # (status="failed") for one AgentInstance's schedule(s), reconciled
    # lazily by agent_schedule_service.py right before the next ACT (see
    # that module's "Health reconciliation" docstring section), before
    # the instance auto-transitions to status="error". A single
    # "retrying" job is never a failure by itself — only a job that
    # ultimately reaches "failed" counts. Any "completed" resets the
    # counter to 0. Explicit and small, per the mission's own instruction
    # not to invent an arbitrary threshold — mirrors the existing
    # precedent of agent_repeated_action_failure_limit above.
    agent_instance_failure_threshold: int = 3

    @field_validator("agent_instance_failure_threshold")
    @classmethod
    def _validate_agent_instance_failure_threshold(cls, v: int) -> int:
        if v < 1:
            raise ValueError("AGENT_INSTANCE_FAILURE_THRESHOLD must be >= 1")
        return v

    # Voice (STT/TTS) — adapter/provider architecture, no single vendor
    # lock-in. Keys are backend-only; never sent to the frontend.
    # See src/voice/ for the provider abstraction.
    #
    # TTS and STT have SEPARATE provider selectors because Gemini 2.5 Flash
    # Preview TTS is a text-to-speech-only model — it has no speech
    # recognition capability, so it cannot serve /voice/transcribe. Gemini is
    # the default for TTS (cost); ElevenLabs remains the default for STT and
    # stays fully selectable for TTS too (set VOICE_PROVIDER=elevenlabs).
    voice_provider: str = "gemini"           # TTS provider
    voice_provider_stt: str = "elevenlabs"   # STT provider

    # ElevenLabs — reference/alternative provider (TTS + STT).
    # ── Presentation capability (Presenton) ──────────────────────────────
    # Presenton runs as its own service, reached over private networking and
    # never exposed publicly. Empty base_url means the capability is simply
    # not available in this deployment — which is a normal state, not a
    # failure: presentation_service.availability() reports it and the
    # capability is not offered.
    presenton_base_url: str = ""
    presenton_username: str = ""
    presenton_password: str = ""
    # Preferred credential: a per-client key the engine issues and can revoke
    # on its own (Bearer sk-presenton-...). Username/password above remain for
    # a freshly-installed engine that has not minted one yet.
    presenton_api_key: str = ""
    presenton_timeout_s: float = 900.0

    # What Presenton is told to use as its LLM. This points at TRINDATRON's
    # own gateway rather than at Google directly, for three reasons that each
    # stand on their own: Presenton fires slide generations in parallel with
    # no setting to pace them, we need a token count it never reports back,
    # and pointing it at Google would mean handing a third-party container
    # our Gemini key. The key stays on this side; Presenton gets the token
    # below, which is ours to rotate.
    presenton_llm_base_url: str = ""
    presenton_llm_gateway_token: str = ""
    # Gemini is the mandated intelligence for this capability. The model id
    # is the OpenAI-compatible form Google's endpoint expects.
    presentation_model: str = "models/gemini-3.8-flash"
    # Presenton runs scaled-to-zero (see infra/presenton/README.md): the first
    # request after an idle period wakes the container, and generation is an
    # async job, so the wake is absorbed rather than watched. This bounds how
    # long we are willing to wait for readiness before declaring it down.
    presenton_wake_timeout_s: float = 180.0
    presenton_wake_poll_interval_s: float = 3.0
    # Stock photography for slides. Free tier (200 req/hour, 20k/month) and
    # NOT an LLM — it is an image search, so it does not touch the
    # Gemini-only rule for the intelligence in this capability. Absent means
    # Presenton falls back to whatever stock source it ships with.
    pexels_api_key: str = ""

    # ── Object storage (Cloudflare R2) ───────────────────────────────────
    # Generated artifacts (PPTX/PDF) are far too large for Postgres — see
    # src/storage/object_store.py. R2 is preferred over S3 because egress is
    # free and a deck is downloaded at least once per generation.
    # Empty credentials select the local-directory store instead, which is
    # the development/test path and cannot mint signed URLs.
    r2_endpoint: str = ""        # https://<account-id>.r2.cloudflarestorage.com
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    local_object_store_root: str = "workspace/objects"
    # How long a download URL stays valid. Short by design: the URL is the
    # only thing standing between a leaked link and the file, and the
    # Workspace mints a fresh one on every download request anyway.
    artifact_signed_url_ttl_s: int = 300
    # How long generated artifacts are kept before the retention sweep
    # removes them. 30 days: long enough that a user coming back to last
    # month's deck still finds it, short enough that storage does not grow
    # without bound.
    artifact_retention_days: int = 30

    elevenlabs_api_key: str = ""
    # A PREMADE voice, not an account-specific one: ElevenLabs ships the same
    # premade catalogue to every account (verified 2026-09-10 against the
    # live /v1/voices endpoint — 21 premade voices, this among them), and it
    # synthesises pt-BR correctly under eleven_multilingual_v2.
    #
    # This used to be "" on the reasoning that a voice id is account-specific
    # and should never be defaulted. That held while ElevenLabs was a
    # provider an operator opted into. It stopped holding when TTS gained
    # failover: with no default, the fallback raised
    # VoiceProviderNotConfigured("ELEVENLABS_TTS_VOICE_ID") and read-aloud
    # died anyway — observed in production while Gemini was returning 429
    # quota errors. A fallback that cannot run is not a fallback.
    #
    # Not a secret: it is a public catalogue identifier. Override with
    # ELEVENLABS_TTS_VOICE_ID to use a cloned or custom voice.
    elevenlabs_tts_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_tts_model_id: str = "eleven_multilingual_v2"
    elevenlabs_stt_model_id: str = "scribe_v1"

    # Gemini TTS — reuses gemini_api_key (already defined above for text
    # routing) rather than duplicating a second Gemini key setting.
    # voice_name/model_id are NOT secrets (public, documented Google
    # identifiers), so a sensible default is fine here — same reasoning now
    # applied to elevenlabs_tts_voice_id above.
    gemini_tts_voice_name: str = "Kore"
    gemini_tts_model_id: str = "gemini-2.5-flash-preview-tts"

    # CORS — origins allowed to call the API from a browser.
    # In production set ALLOWED_ORIGINS='["https://your-domain.com"]'
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    # Model Router — plan assignment for new users.
    # The router enforces limits server-side; clients cannot override these.
    default_user_plan: str = "start"

    # Binance Pay adapter (Billing — sandbox/test phase only, see
    # billing/providers/binance_pay_adapter.py). Binance Pay has no
    # documented public sandbox environment as of this writing — the base
    # URL below is Binance's one documented endpoint, used in production
    # by real merchants. This adapter is registered ONLY when explicitly
    # enabled AND both credentials are non-empty (see
    # binance_pay_adapter.build_binance_pay_adapter()); with any of those
    # missing — the default in every environment that hasn't set them,
    # including this one — the provider is simply not registered and the
    # Core boots exactly as if this setting block didn't exist.
    binance_pay_enabled: bool = False
    binance_pay_api_key: str = ""
    binance_pay_secret_key: str = ""
    binance_pay_base_url: str = "https://bpay.binanceapi.com"
    # Binance's RSA public key (PEM), used to verify webhook signatures.
    # Optional: if empty, the adapter fetches and caches it via the Query
    # Certificate API on first webhook instead of requiring it pinned here.
    binance_pay_public_key_pem: str = ""
    # Reject a webhook whose BinancePay-Timestamp is older than this many
    # seconds — bounds how long a captured (payload, signature) pair could
    # be replayed. Binance's own request-signing docs require requests to
    # arrive within ~1s of their timestamp for OUTBOUND calls; inbound
    # webhooks aren't documented with an equivalent bound, so this is a
    # deliberately generous default (network/processing delay tolerant)
    # rather than a value taken from Binance's own spec.
    binance_pay_webhook_max_clock_skew_seconds: int = 300

    # NOWPayments adapter (Billing — first ACTIVE crypto provider, see
    # billing/providers/nowpayments_adapter.py). Same "never registered
    # unless explicitly enabled AND fully configured" contract as Binance
    # Pay above (see nowpayments_adapter.build_nowpayments_adapter()) —
    # absence of these three values leaves the Core booting exactly as if
    # this setting block didn't exist. Binance Pay itself is left intact
    # and registrable alongside this — NOWPayments does not replace it in
    # code, only in which provider is actually configured for launch.
    nowpayments_enabled: bool = False
    nowpayments_api_key: str = ""
    nowpayments_ipn_secret: str = ""
    nowpayments_base_url: str = "https://api.nowpayments.io"
    # Optional — where NOWPayments should POST the IPN callback, and where
    # the hosted invoice page should redirect the customer after payment.
    # Left empty in every environment that hasn't set them; NOWPayments
    # falls back to whatever is configured in the merchant dashboard for
    # ipn_callback_url, and simply omits success/cancel redirects.
    nowpayments_ipn_callback_url: str = ""
    nowpayments_success_url: str = ""
    nowpayments_cancel_url: str = ""

    # Mercado Pago adapter (Payments Foundation etapa — first FiatPaymentProvider,
    # see billing/providers/mercadopago_adapter.py). Same "never registered
    # unless explicitly enabled AND fully configured" contract as Binance Pay/
    # NOWPayments above (see mercadopago_adapter.build_mercadopago_adapter()) —
    # absence of these two values leaves the Core booting exactly as if this
    # setting block didn't exist. Uses Checkout Pro (hosted preference/redirect)
    # — no card data ever reaches TRINITY Core (PCI stays with Mercado Pago).
    mercadopago_enabled: bool = False
    mercadopago_access_token: str = ""
    mercadopago_webhook_secret: str = ""
    mercadopago_base_url: str = "https://api.mercadopago.com"
    # Optional — where Mercado Pago should POST the webhook notification, and
    # where Checkout Pro should redirect the payer after success/pending/
    # failure. Left empty in every environment that hasn't set them; Mercado
    # Pago falls back to whatever is configured in the application dashboard
    # for notification_url, and simply omits back_urls/auto_return.
    mercadopago_notification_url: str = ""
    mercadopago_success_url: str = ""
    mercadopago_pending_url: str = ""
    mercadopago_failure_url: str = ""

    # Stripe adapter (Payments Foundation etapa — second FiatPaymentProvider,
    # see billing/providers/stripe_adapter.py). Same "never registered unless
    # explicitly enabled AND fully configured" contract as Mercado Pago/
    # Binance Pay/NOWPayments above (see stripe_adapter.build_stripe_adapter()).
    # Uses Stripe Checkout (hosted Checkout Session) — no card data ever
    # reaches TRINITY Core (PCI stays with Stripe).
    stripe_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_base_url: str = "https://api.stripe.com"
    # success_url is a REQUIRED Stripe Checkout Session parameter (unlike
    # Mercado Pago's optional back_urls) — a real create_intent() call fails
    # at Stripe's own API if this is empty. Left empty by default; only
    # filled in for a real, deliberate manual test with real credentials.
    stripe_success_url: str = ""
    stripe_cancel_url: str = ""
    # Tolerance (seconds) for the Stripe-Signature header's timestamp —
    # bounds how long a captured (payload, signature) pair could be
    # replayed. Matches Stripe's own library default (5 minutes); the
    # SECOND layer of defense (nonce tracking, DB-backed, multi-process)
    # is automatic via the existing webhook_nonce mechanism.
    stripe_webhook_tolerance_seconds: int = 300

    # Payment Router — deterministic provider priority when auto-selecting
    # among multiple ELIGIBLE providers (see src/billing/payment_router.py).
    # Plain, explicit, ORDERED list — not scattered if/else. A provider not
    # in this list still works (registered = selectable), it just sorts
    # LAST, deterministically — so a brand-new adapter (Asaas, Adyen, ...)
    # is usable the moment it's registered, with zero code change, and
    # only needs an entry here to be preferred over an existing one.
    payment_provider_priority: list[str] = ["mercado_pago", "stripe", "nowpayments", "binance_pay"]

    # Subscription Engine — dunning/grace-period TECHNICAL defaults, NOT a
    # business decision (see src/services/subscription_service.py's own
    # module docstring). Override before any real launch with whatever
    # commercial policy is actually decided; nothing in this codebase
    # currently reads these to change entitlement automatically.
    #
    # How many CONSECUTIVE explicitly-rejected renewal cycles a
    # Subscription tolerates in 'past_due' before moving to
    # 'grace_period'. An ambiguous (ProviderUnavailable) outcome never
    # counts toward this — see SubscriptionService's own docstring.
    subscription_max_past_due_cycles: int = 1
    # How many days a Subscription stays in 'grace_period' before
    # SubscriptionService.expire_subscription() may transition it to
    # 'expired' (mirrors PaymentOrder's own expiry-sweep pattern).
    subscription_grace_period_days: int = 3

    # Security Hardening — POST /auth/login has no other protection against
    # credential stuffing / brute force (password hashing alone only slows
    # a single guess, not a high-volume attempt loop). Two independent
    # buckets, same dual-check pattern as request_control.py's
    # check_rate_limits(): per-IP (catches one attacker guessing across many
    # accounts) AND per-email (catches distributed attempts against ONE
    # account from many IPs/proxies) — either one tripping is enough to
    # reject. Deliberately its own bucket, not request_control's shared
    # chat/agent limiter (login has no authenticated user_id yet).
    auth_login_ip_rate_limit: int = 10
    auth_login_ip_rate_window_s: float = 60.0
    auth_login_email_rate_limit: int = 5
    auth_login_email_rate_window_s: float = 60.0

    # FREE anti-abuse (economic audit, 2026-09-13). POST /auth/register had no
    # limit at all, so one script could mint FREE accounts as fast as the
    # database accepted them, each with its own weekly allowance. 10 an hour
    # per IP leaves room for a shared office or campus address and stops a
    # loop. The plan-wide budget below is the real bound: it holds however
    # many accounts get through.
    auth_register_ip_rate_limit: int = 10
    auth_register_ip_rate_window_s: float = 3600.0

    # One provider-spend budget per UTC day for EVERY free account together.
    # FREE earns nothing, so its worst case must be a number the business
    # chooses, not a function of how many accounts someone can create. When
    # it is spent, FREE requests are refused with a reset time until 00:00
    # UTC; paid plans are never affected. $5/day bounds FREE at ~$155 a month
    # in the worst case. None disables the budget (not recommended).
    free_plan_global_daily_budget_usd: float | None = 5.0

    # Persistent sessions (src/services/session_token_service.py). A client
    # that declares itself on login/register (web, desktop, mobile) gets a
    # short access token and a rotating refresh token; a client that does not
    # keeps the legacy jwt_expiration_minutes token until it adopts refresh.
    access_token_minutes: int = 30
    # Inactivity limit: every refresh restarts it, so a login used at least
    # once in this many days never expires.
    refresh_token_days: int = 90
    # Two refreshes of the same token within this window (two tabs waking up,
    # a network retry) both succeed; later, it is a replay and the login is
    # revoked.
    refresh_reuse_grace_seconds: int = 30
    # Web keeps the refresh token in an httpOnly cookie scoped to /auth, never
    # in JavaScript-readable storage. SameSite=Lax needs the API and the web
    # app on the same site (api.<domain> and <domain>); set "none" only if
    # they cannot be.
    refresh_cookie_name: str = "trindatron_refresh"
    refresh_cookie_secure: bool = True
    refresh_cookie_samesite: str = "lax"
    refresh_cookie_domain: str = ""
    auth_refresh_ip_rate_limit: int = 60
    auth_refresh_ip_rate_window_s: float = 60.0
    # Sign in with Google (OpenID Connect). Web uses a "Web application"
    # client and a server-side code exchange; desktop uses a separate
    # "Desktop app" client with a loopback redirect and PKCE, which is what
    # Google supports for installed apps. Empty client ids leave Google
    # sign-in off and nothing else depends on them.
    google_client_id: str = ""
    google_client_secret: str = ""
    # The callback registered with Google, e.g.
    # https://api.<domain>/auth/google/callback. Empty derives it from the
    # running API's own URL, which is only right in development.
    google_redirect_uri: str = ""
    google_desktop_client_id: str = ""
    google_desktop_client_secret: str = ""
    # Where the web app lives, for the redirect back after Google sign-in.
    # Empty uses the first entry of allowed_origins.
    web_app_url: str = ""

    # Account security (src/services/account_security_service.py).
    # Transactional email goes through Resend (https://resend.com). An empty
    # key leaves email off: links are not sent, and every flow still answers
    # the same way, so turning it on later changes nothing else. email_from
    # must be on a domain verified in Resend.
    resend_api_key: str = ""
    email_from: str = "TRINDATRON <nao-responda@trindatron.com>"
    password_reset_minutes: int = 30
    email_verify_hours: int = 48
    # Wrong passwords in a row, each within the window of the previous one,
    # that lock password sign-in for auth_lockout_minutes. Google sign-in and
    # password reset still work while locked — the owner is never locked out
    # of their own account, only a guesser is.
    auth_lockout_threshold: int = 10
    auth_lockout_window_minutes: int = 15
    auth_lockout_minutes: int = 15
    # "Forgot password" and "resend confirmation" send email, so they are
    # limited tightly: per network (429) and per address (silently not sent,
    # so the answer never reveals whether the address has an account).
    auth_email_ip_rate_limit: int = 10
    auth_email_ip_rate_window_s: float = 3600.0
    auth_email_address_rate_limit: int = 3
    auth_email_address_rate_window_s: float = 3600.0

    # Commercial Checkout V1 — plan-purchase orders (src/billing/pricing.py,
    # POST /billing/checkout). Own rate-limit bucket, deliberately separate
    # from request_control's chat/agent buckets above (see
    # billing.py's own module docstring) — a user's chat activity must
    # never eat into their checkout attempt budget, or vice versa.
    billing_order_rate_limit: int = 10   # POST /billing/orders + /billing/checkout, per user
    billing_order_rate_window_s: float = 60.0
    # How often the background sweep transitions overdue 'pending' orders
    # to 'expired' (payment_order_service.run_payment_order_expiry_loop()).
    payment_order_expiry_poll_interval_seconds: float = 60.0
    # Default checkout window — how long a plan-purchase PaymentOrder stays
    # confirmable before the expiry sweep marks it 'expired'.
    checkout_order_ttl_seconds: int = 1800  # 30 minutes

    # POST /billing/orders/{id}/confirm for orders whose provider has no
    # adapter registered. Off: such an order has no payment evidence at all
    # (the economic audit of 2026-09-13 found that any account could check
    # out MAX with provider "none" and confirm it, or buy unlimited credits
    # the same way). Admins can still confirm their own manual orders. Turn
    # on only for a local deployment with no payment provider configured.
    billing_client_confirm_enabled: bool = False

    # Production Payment Activation hardening — webhook replay protection
    # (src/services/webhook_replay_service.py) and webhook-endpoint rate
    # limiting. Retention is deliberately decoupled from any one
    # provider's own clock-skew setting (e.g. binance_pay_webhook_max_clock_skew_seconds)
    # — a nonce only needs to outlive the LONGEST clock-skew window in use,
    # and a generous fixed default costs nothing (a handful of small rows).
    webhook_nonce_retention_seconds: float = 3600.0  # 1 hour
    webhook_nonce_cleanup_interval_seconds: float = 900.0  # 15 minutes

    # Beta hardening (2026-09-10) — TCU reservation orphan reclaim (see
    # entitlement_service.py's reclaim_expired_reservations()). A
    # 'pending' UsageRecord reservation can only legitimately stay
    # pending for as long as one request runs — bounded by
    # agent_scheduler_execution_timeout_seconds (300s default) for the
    # longest case (an agent execution). TTL is set to 6x that hard
    # cutoff so a genuinely still-running request is never reclaimed out
    # from under itself; the sweep interval is more frequent than
    # webhook_nonce's own (a stuck user waiting on quota is more urgent
    # than a stale webhook nonce).
    tcu_reservation_ttl_seconds: float = 1800.0  # 30 minutes
    tcu_reservation_reclaim_interval_seconds: float = 300.0  # 5 minutes
    # Generous — providers legitimately retry a webhook (Binance Pay: up
    # to 6 times per its own docs) and this must never block a real
    # payment notification; it exists purely to bound raw volumetric
    # abuse of a public, unauthenticated endpoint. Keyed by client IP
    # (no authenticated user exists on this route) — see billing.py's
    # own _check_webhook_rate_limit().
    billing_webhook_rate_limit: int = 120
    billing_webhook_rate_window_s: float = 60.0

    @field_validator("billing_order_rate_limit", "billing_webhook_rate_limit")
    @classmethod
    def _validate_billing_rate_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError("billing rate limit settings must be >= 1")
        return v

    # Shell sandbox resource limits — security flags are hardcoded in DockerSandbox.
    agent_sandbox_image: str = "trinity-sandbox:latest"
    agent_sandbox_memory_limit: str = "256m"
    agent_sandbox_cpu_limit: str = "0.5"
    agent_sandbox_pids_limit: int = 64

    # Desktop sandbox resource limits — security flags are hardcoded in DesktopSandbox.
    agent_desktop_sandbox_image: str = "trinity-desktop-sandbox:latest"
    agent_desktop_sandbox_memory_limit: str = "512m"
    agent_desktop_sandbox_cpu_limit: str = "1.0"
    agent_desktop_sandbox_pids_limit: int = 128

    # Browser (Playwright/Chromium) resource governance — see
    # trinity_agent.browser.governor.BrowserResourceGovernor. One Chromium
    # process per concurrently-active BrowserSession; each is ~150-300MB, so
    # the default is conservative for a modest production instance.
    # _BROWSER_MAX_SESSIONS_HARD_CAP is an absolute ceiling independent of
    # this setting's own value — a misconfigured env var (typo, extra zero)
    # can never push Chromium concurrency past it.
    _BROWSER_MAX_SESSIONS_HARD_CAP: ClassVar[int] = 32

    agent_browser_max_concurrent_sessions: int = 4
    agent_browser_capacity_wait_timeout_s: float = 30.0
    agent_browser_shutdown_timeout_s: float = 15.0

    @field_validator("agent_browser_max_concurrent_sessions")
    @classmethod
    def _validate_browser_max_sessions(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "AGENT_BROWSER_MAX_CONCURRENT_SESSIONS must be >= 1 — 0 or "
                "negative would make every browser tool call hang (no "
                "timeout) or fail immediately (with a timeout), never "
                "succeed."
            )
        if v > cls._BROWSER_MAX_SESSIONS_HARD_CAP:
            logger.warning(
                "AGENT_BROWSER_MAX_CONCURRENT_SESSIONS=%d exceeds the hard "
                "cap of %d; clamping to %d to prevent unbounded Chromium "
                "process growth.",
                v, cls._BROWSER_MAX_SESSIONS_HARD_CAP, cls._BROWSER_MAX_SESSIONS_HARD_CAP,
            )
            return cls._BROWSER_MAX_SESSIONS_HARD_CAP
        return v

    @field_validator("agent_browser_capacity_wait_timeout_s")
    @classmethod
    def _validate_browser_wait_timeout(cls, v: float) -> float:
        if v < 0:
            raise ValueError("AGENT_BROWSER_CAPACITY_WAIT_TIMEOUT_S must be >= 0")
        return v

    @field_validator("agent_browser_shutdown_timeout_s")
    @classmethod
    def _validate_browser_shutdown_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                "AGENT_BROWSER_SHUTDOWN_TIMEOUT_S must be > 0 — a value of 0 "
                "would mean shutdown never even attempts to close resources."
            )
        return v

    # Request Control (Etapa 5B — TRINITY SCALE ENGINE) — see
    # src/services/request_control.py. Protects the process from traffic
    # spikes/abuse BEFORE any expensive work (session lookup, TCU
    # reservation, provider/agent calls). IN-MEMORY / SINGLE PROCESS: these
    # limits are enforced per-process; with multiple Core replicas the true
    # aggregate rate/concurrency across all of them would exceed these
    # numbers — externalizing the backing state (Redis) is a future step,
    # not implemented here (see request_control.py's module docstring).
    _REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP: ClassVar[int] = 1000

    request_control_user_rate_limit: int = 30          # requests per window, per authenticated user
    request_control_user_rate_window_s: float = 60.0
    request_control_ip_rate_limit: int = 60             # requests per window, per client IP
    request_control_ip_rate_window_s: float = 60.0
    request_control_user_max_concurrent: int = 3        # in-flight chat/agent streams per user
    request_control_global_max_concurrent: int = 50     # in-flight chat/agent streams, whole process

    @field_validator(
        "request_control_user_rate_limit",
        "request_control_ip_rate_limit",
        "request_control_user_max_concurrent",
    )
    @classmethod
    def _validate_request_control_positive_int(cls, v: int, info) -> int:
        if v < 1:
            raise ValueError(f"{info.field_name.upper()} must be >= 1")
        return v

    @field_validator("request_control_global_max_concurrent")
    @classmethod
    def _validate_request_control_global_concurrent(cls, v: int) -> int:
        if v < 1:
            raise ValueError("REQUEST_CONTROL_GLOBAL_MAX_CONCURRENT must be >= 1")
        if v > cls._REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP:
            logger.warning(
                "REQUEST_CONTROL_GLOBAL_MAX_CONCURRENT=%d exceeds the hard "
                "cap of %d; clamping to %d.",
                v, cls._REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP, cls._REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP,
            )
            return cls._REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP
        return v

    @field_validator(
        "request_control_user_rate_window_s",
        "request_control_ip_rate_window_s",
    )
    @classmethod
    def _validate_request_control_window(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(
                f"{info.field_name.upper()} must be > 0 — a zero-length "
                "window would divide by nothing meaningful and admit "
                "every request or none, never a real rate limit."
            )
        return v

    # Agent Scheduler (Etapa 5D) — bounds concurrent Agent/ReAct executions
    # specifically (a subset of all requests Request Control already bounds
    # more broadly — see agent_scheduler.py's module docstring). Global
    # default is deliberately well below request_control_global_max_concurrent
    # since a single Agent execution is far more resource-intensive (full
    # tool registry, possible Chromium process, multi-iteration LLM loop)
    # than an average chat/agent request. Same hard-cap-with-warning pattern
    # as REQUEST_CONTROL_GLOBAL_MAX_HARD_CAP above — a misconfigured env var
    # can never push Agent concurrency past a sane ceiling.
    _AGENT_SCHEDULER_GLOBAL_MAX_HARD_CAP: ClassVar[int] = 200
    _AGENT_SCHEDULER_TIMEOUT_HARD_CAP_S: ClassVar[float] = 3600.0

    agent_scheduler_global_max_concurrent: int = 10
    agent_scheduler_user_max_concurrent: int = 2
    agent_scheduler_execution_timeout_seconds: float = 300.0

    @field_validator("agent_scheduler_user_max_concurrent")
    @classmethod
    def _validate_agent_scheduler_user_max_concurrent(cls, v: int) -> int:
        if v < 1:
            raise ValueError("AGENT_SCHEDULER_USER_MAX_CONCURRENT must be >= 1")
        return v

    @field_validator("agent_scheduler_global_max_concurrent")
    @classmethod
    def _validate_agent_scheduler_global_max_concurrent(cls, v: int) -> int:
        if v < 1:
            raise ValueError("AGENT_SCHEDULER_GLOBAL_MAX_CONCURRENT must be >= 1")
        if v > cls._AGENT_SCHEDULER_GLOBAL_MAX_HARD_CAP:
            logger.warning(
                "AGENT_SCHEDULER_GLOBAL_MAX_CONCURRENT=%d exceeds the hard "
                "cap of %d; clamping to %d.",
                v, cls._AGENT_SCHEDULER_GLOBAL_MAX_HARD_CAP, cls._AGENT_SCHEDULER_GLOBAL_MAX_HARD_CAP,
            )
            return cls._AGENT_SCHEDULER_GLOBAL_MAX_HARD_CAP
        return v

    @field_validator("agent_scheduler_execution_timeout_seconds")
    @classmethod
    def _validate_agent_scheduler_execution_timeout(cls, v: float) -> float:
        if v < 10:
            raise ValueError(
                "AGENT_SCHEDULER_EXECUTION_TIMEOUT_SECONDS must be >= 10 — "
                "anything shorter would abort a normal multi-iteration "
                "Agent/ReAct loop before it has a realistic chance to finish."
            )
        if v > cls._AGENT_SCHEDULER_TIMEOUT_HARD_CAP_S:
            logger.warning(
                "AGENT_SCHEDULER_EXECUTION_TIMEOUT_SECONDS=%.0f exceeds the "
                "hard cap of %.0f; clamping to %.0f.",
                v, cls._AGENT_SCHEDULER_TIMEOUT_HARD_CAP_S, cls._AGENT_SCHEDULER_TIMEOUT_HARD_CAP_S,
            )
            return cls._AGENT_SCHEDULER_TIMEOUT_HARD_CAP_S
        return v

    @field_validator("agent_repeated_action_failure_limit")
    @classmethod
    def _validate_agent_repeated_action_failure_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "AGENT_REPEATED_ACTION_FAILURE_LIMIT must be >= 1 — 0 or "
                "negative would either never trip or trip before the first "
                "genuine attempt, defeating its own purpose."
            )
        return v

    @field_validator("agent_tool_failure_streak_limit")
    @classmethod
    def _validate_agent_tool_failure_streak_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "AGENT_TOOL_FAILURE_STREAK_LIMIT must be >= 1 — 0 or "
                "negative would either never trip or trip before the first "
                "genuine attempt, defeating its own purpose."
            )
        return v

    @field_validator("idempotency_stale_after_seconds")
    @classmethod
    def _validate_idempotency_stale_after_seconds(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("IDEMPOTENCY_STALE_AFTER_SECONDS must be > 0")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        if self.environment != "production":
            return self

        errors: list[str] = []

        # PostgreSQL required in production — SQLite cannot handle concurrent writes.
        # Canonical scheme: postgresql+psycopg:// (psycopg3, the installed driver).
        # postgresql:// and postgres:// are tolerated for cloud PaaS compatibility
        # (Heroku, Railway, Render) but postgresql+psycopg:// is the recommended form.
        # NOT accepted: postgresql+psycopg2:// — psycopg2 is not installed; accepting
        #   it would pass validation but crash at engine creation.
        _pg_schemes = ("postgresql+psycopg://", "postgresql://", "postgres://")
        if not any(self.database_url.startswith(s) for s in _pg_schemes):
            errors.append(
                "DATABASE_URL must use PostgreSQL in production. "
                "SQLite does not support concurrent writes. "
                "Recommended: postgresql+psycopg://user:password@host:5432/trinity_db"
            )

        if self.jwt_secret_key in INSECURE_JWT_DEFAULTS:
            errors.append(
                "JWT_SECRET_KEY is using an insecure placeholder. "
                "Generate a secure key with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        elif len(self.jwt_secret_key) < 32:
            errors.append(
                "JWT_SECRET_KEY must be at least 32 characters in production."
            )

        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY must be set in production.")

        if self.debug:
            errors.append(
                "DEBUG must be false in production (exposes /docs and SQL logs)."
            )

        for origin in self.allowed_origins:
            if "localhost" in origin or "127.0.0.1" in origin:
                errors.append(
                    f"ALLOWED_ORIGINS must not include localhost in production: {origin}"
                )
                break

        # A wildcard combined with the app's allow_credentials=True
        # (src/main.py's CORSMiddleware) makes Starlette echo back the
        # REQUESTING origin instead of a literal "*" — i.e. "any origin,
        # with credentials", not blocked by the browser's own CORS spec
        # rejection of literal wildcard+credentials. Refuse to boot rather
        # than silently accept this in production.
        if "*" in self.allowed_origins:
            errors.append("ALLOWED_ORIGINS must not contain '*' in production (see CORSMiddleware allow_credentials=True).")

        if self.testing:
            errors.append(
                "TESTING must not be true in production (it points the database at an "
                "in-memory engine and disables every background reconciliation loop)."
            )

        if errors:
            bullet_list = "\n  - ".join(errors)
            raise ValueError(
                f"TRINITY CORE refused to start in production due to insecure configuration:\n"
                f"  - {bullet_list}"
            )

        return self


settings = Settings()
