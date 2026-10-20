import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import INSECURE_JWT_DEFAULTS, settings
from src.middleware.logging import RequestLoggingMiddleware, setup_logging
from src.api.routes import health, auth, sessions, users, projects, workspaces, skills, agent_definitions, memories, billing, voice, admin, execution, agent_instances, presentations, presenton_gateway

logger = logging.getLogger(__name__)

# Inject API keys from pydantic settings into os.environ so litellm can find them.
# pydantic_settings loads .env into Python objects but does NOT set os.environ.
if settings.anthropic_api_key:
    os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
if settings.openai_api_key:
    os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
if settings.gemini_api_key:
    os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
if settings.groq_api_key:
    os.environ.setdefault("GROQ_API_KEY", settings.groq_api_key)


def _log_startup_warnings() -> None:
    """Warn about insecure settings in non-production environments."""
    if settings.is_production:
        return  # Production validation is enforced by Settings.model_validator.

    if settings.jwt_secret_key in INSECURE_JWT_DEFAULTS:
        logger.warning(
            "[SECURITY] JWT_SECRET_KEY is using an insecure placeholder. "
            "Set a strong secret before deploying to production."
        )
    if settings.debug:
        logger.warning(
            "[SECURITY] DEBUG=true — /docs and /redoc are publicly accessible."
        )
    if not settings.anthropic_api_key:
        logger.warning(
            "[CONFIG] ANTHROPIC_API_KEY is not set — chat and agent requests will fail."
        )


def _migration_head(versions_dir: "Path | None" = None) -> str | None:
    """The revision the migrations end at: the one nothing else supersedes.

    Read from the files rather than through Alembic's own API, because this
    package ships a directory named `alembic/`, which shadows the installed
    library whenever the Core's root is on the import path.
    """
    import re
    from pathlib import Path

    directory = versions_dir or Path(__file__).resolve().parent.parent / "alembic" / "versions"
    revisions: set[str] = set()
    superseded: set[str] = set()
    for path in directory.glob("*.py"):
        source = path.read_text(encoding="utf-8", errors="replace")
        revision = re.search(r"^revision\s*(?::\s*str\s*)?=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE)
        if revision:
            revisions.add(revision.group(1))
        # A merge revision names several parents: down_revision = ("0031_…", "0033_…").
        for statement in re.finditer(r"^down_revision[^=\n]*=\s*(.+)$", source, re.MULTILINE):
            superseded.update(re.findall(r"['\"]([^'\"]+)['\"]", statement.group(1)))
    heads = revisions - superseded
    # Anything but exactly one head (a branch, or a merge written across several lines) is
    # reported as unknown rather than guessed: a wrong "you must migrate" is worse than silence.
    return heads.pop() if len(heads) == 1 else None


def _database_revision() -> str | None:
    """What the database says it was migrated to, or None when it carries no stamp."""
    from sqlalchemy import inspect, text

    from src.database import engine

    with engine.connect() as connection:
        if "alembic_version" not in inspect(connection).get_table_names():
            return None
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _log_schema_drift() -> None:
    """Say, once at boot, when the database is behind the migrations this build ships.

    A missing column does not look like a missing column from outside. When
    usage_records lacked a column this build writes, every reservation failed,
    so every conversation answered "temporarily unavailable" — which reads as a
    capability being down rather than as a migration that was never run. One
    line at startup turns hours of that into a one-line answer. Best-effort:
    never blocks boot, and a database created by create_all (tests) has no
    stamp to compare.
    """
    try:
        head = _migration_head()
        current = _database_revision()
        if head and current and head != current:
            logger.error(
                "[SCHEMA] The database is at %s but this build expects %s. Run 'alembic upgrade head': "
                "until then requests that record usage or read preferences fail, and clients show them "
                "as temporarily unavailable.",
                current, head,
            )
    except Exception:
        logger.debug("[SCHEMA] Could not compare the database revision to the migration head.", exc_info=True)


def _run_startup_cleanup() -> None:
    """Run session retention cleanup once on startup (best-effort)."""
    try:
        from src.database import SessionLocal
        from src.services.session_service import cleanup_expired_sessions
        db = SessionLocal()
        try:
            removed = cleanup_expired_sessions(db)
            if removed:
                logger.info("[RETENTION] Startup cleanup: permanently removed %d expired session(s).", removed)
        finally:
            db.close()
    except Exception:
        logger.exception("[RETENTION] Startup cleanup failed — skipping.")


def _run_5i_reconciliation() -> None:
    """Etapa 5I — reconcile any IdempotencyKey/AgentExecution row left
    "in_progress"/"admitted"/"running" by a PREVIOUS process instance
    (crash, kill, or a deploy that didn't reach a graceful shutdown path).
    Best-effort, same shape as _run_startup_cleanup(): opens its own
    SessionLocal(), never lets a failure here block app startup."""
    try:
        from src.database import SessionLocal
        from src.services.idempotency_service import reconcile_stale_idempotency_keys_at_startup
        from src.services.agent_execution_service import reconcile_stale_executions_at_startup
        db = SessionLocal()
        try:
            reconciled_keys = reconcile_stale_idempotency_keys_at_startup(db)
            reconciled_executions = reconcile_stale_executions_at_startup(db)
            if reconciled_keys:
                logger.warning(
                    "[5I] Startup reconciliation: %d stale idempotency key(s) from a previous process.",
                    reconciled_keys,
                )
            if reconciled_executions:
                logger.warning(
                    "[5I] Startup reconciliation: %d stale agent execution(s) from a previous process.",
                    reconciled_executions,
                )
        finally:
            db.close()
    except Exception:
        logger.exception("[5I] Startup reconciliation failed — skipping.")


def _resume_media_provider_jobs() -> None:
    """Follow media jobs a hosted provider accepted before this process started,
    instead of failing them or submitting them again (media_service.resume_job).
    Best-effort: never blocks startup."""
    try:
        from src.services.media_service import resume_provider_jobs
        resumed = resume_provider_jobs()
        if resumed:
            logger.warning("[MEDIA] Startup: following %d provider job(s) from a previous process.", resumed)
    except Exception:
        logger.exception("[MEDIA] Startup resume of provider jobs failed — skipping.")


def _run_execution_reconciliation() -> None:
    """Durable Execution — reclaim any ExecutionJob whose lease is missing
    or expired (see execution_worker.py's module docstring for why this,
    not an unconditional "mark everything live as failed", is the correct
    startup check now: a "running" job with a still-valid lease may
    genuinely be owned by another live process/replica). Same best-effort
    shape as _run_5i_reconciliation() above. The SAME function also runs
    periodically thereafter — see the recovery loop started in lifespan()
    below — this call just ensures a fresh boot catches anything already
    stale before the first periodic sweep would."""
    try:
        from src.database import SessionLocal
        from src.services.execution_worker import reclaim_expired_leases
        db = SessionLocal()
        try:
            reconciled = reclaim_expired_leases(db)
            if reconciled:
                logger.warning(
                    "[EXECUTION] Startup reconciliation: %d expired execution lease(s) reclaimed.",
                    reconciled,
                )
        finally:
            db.close()
    except Exception:
        logger.exception("[EXECUTION] Startup reconciliation failed — skipping.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(debug=settings.debug)
    _log_startup_warnings()
    _log_schema_drift()
    # CP8: these five are best-effort reconciliation against whatever a PREVIOUS
    # process left behind — meaningless for a TestClient boot, which starts from
    # tables this same test just created. Skipping them under settings.testing is
    # not "test-only mock logic": it is the same testing flag documented on
    # Settings.testing (config.py) drawing the one line the lifespan needs between
    # "a real process resuming real work" and "a test booting the ASGI app". Nothing
    # about their production behavior changes when settings.testing is false.
    if not settings.testing:
        _run_startup_cleanup()
        _run_5i_reconciliation()
        _run_execution_reconciliation()
        _resume_media_provider_jobs()
    # Initialize the provider registry. Empty unless a provider's own opt-in
    # config is fully satisfied — see each build_*_adapter()'s own guard.
    # Absence of any given provider must never prevent boot: every builder
    # returns None (not an exception) when its config is incomplete.
    from src.billing.providers.registry import ProviderRegistry
    from src.billing.providers.binance_pay_adapter import build_binance_pay_adapter
    from src.billing.providers.nowpayments_adapter import build_nowpayments_adapter
    from src.billing.providers.mercadopago_adapter import build_mercadopago_adapter
    from src.billing.providers.stripe_adapter import build_stripe_adapter
    from src.api.deps import set_provider_registry
    _billing_registry = ProviderRegistry()
    _binance_pay_adapter = build_binance_pay_adapter(settings)
    if _binance_pay_adapter is not None:
        _billing_registry.register(_binance_pay_adapter)
        logger.info("billing: binance_pay provider registered")
    _nowpayments_adapter = build_nowpayments_adapter(settings)
    if _nowpayments_adapter is not None:
        _billing_registry.register(_nowpayments_adapter)
        logger.info("billing: nowpayments provider registered")
    _mercadopago_adapter = build_mercadopago_adapter(settings)
    if _mercadopago_adapter is not None:
        _billing_registry.register(_mercadopago_adapter)
        logger.info("billing: mercado_pago provider registered")
    _stripe_adapter = build_stripe_adapter(settings)
    if _stripe_adapter is not None:
        _billing_registry.register(_stripe_adapter)
        logger.info("billing: stripe provider registered")
    set_provider_registry(_billing_registry)

    # Initialize the voice provider registry. Both adapters are always
    # registered (adapter/provider architecture, no vendor lock-in) — each
    # raises a clear "missing config" error at call time if its required
    # key is unset, rather than failing silently or inventing a key at
    # startup. settings.voice_provider (TTS) / voice_provider_stt (STT)
    # pick which registered adapter actually serves each route.
    from src.voice.adapters.elevenlabs import ElevenLabsVoiceProvider
    from src.voice.adapters.gemini import GeminiVoiceProvider
    from src.voice.registry import VoiceProviderRegistry
    from src.api.deps import set_voice_registry
    voice_registry = VoiceProviderRegistry()
    voice_registry.register(
        ElevenLabsVoiceProvider(
            api_key=settings.elevenlabs_api_key,
            tts_voice_id=settings.elevenlabs_tts_voice_id,
            tts_model_id=settings.elevenlabs_tts_model_id,
            stt_model_id=settings.elevenlabs_stt_model_id,
        )
    )
    voice_registry.register(
        GeminiVoiceProvider(
            api_key=settings.gemini_api_key,
            tts_voice_name=settings.gemini_tts_voice_name,
            tts_model_id=settings.gemini_tts_model_id,
        )
    )
    # Transcription fallback (and the cheaper transcriber when chosen): open-weight Whisper, hosted.
    from src.voice.adapters.groq import GroqVoiceProvider
    voice_registry.register(GroqVoiceProvider(api_key=settings.groq_api_key))
    # stt.local (experimental): registered only when STT_LOCAL_ENABLED and a loopback server are set. Never part
    # of the automatic STT fallback, and not run until voice/budget.py has a cost bound for it.
    from src.voice.adapters.whisper_local import build_whisper_local_provider
    _whisper_local = build_whisper_local_provider()
    if _whisper_local is not None:
        voice_registry.register(_whisper_local)
        logger.info("voice: stt.local registered (experimental, not in the fallback chain)")
    set_voice_registry(voice_registry)
    if settings.voice_provider == "gemini" and not settings.gemini_api_key:
        logger.warning(
            "[CONFIG] GEMINI_API_KEY is not set — /voice/synthesize (default "
            "provider: gemini) will return 424 until it is configured."
        )
    if settings.voice_provider_stt == "elevenlabs" and not settings.elevenlabs_api_key:
        logger.warning(
            "[CONFIG] ELEVENLABS_API_KEY is not set — /voice/transcribe (default "
            "provider: elevenlabs) will return 424 until it is configured."
        )

    # CP8: none of the seven loops below (or their startup reconciliation
    # counterparts above) start under settings.testing. A TestClient boot needs a
    # minimal, deterministic lifespan, not a second live copy of every production
    # poll loop racing the test's own create_all/drop_all cycle on the same engine —
    # that race is what made tests/integration/test_admin_economics.py fail
    # non-deterministically (CP7). A test that wants one of these loops to actually
    # run drives it directly instead (see conftest.py's execution_session_override /
    # run_worker_until_terminal for the existing example).
    import asyncio
    if not settings.testing:
        # Persistent-agent poll loop (AgentSchedule -> create_job() only, never
        # executes work directly — see agent_schedule_service.py). Started
        # here, stopped gracefully below on shutdown via stop_event — same
        # lifespan-scoped background-task pattern as everything else in this
        # function, just the first ones that need an explicit stop signal
        # (they're `while` loops, not one-shot startup routines).
        from src.services.agent_schedule_service import run_schedule_poll_loop
        _schedule_stop_event = asyncio.Event()
        _schedule_task = asyncio.create_task(run_schedule_poll_loop(stop_event=_schedule_stop_event))

        # Durable Execution Worker + recovery loop (see execution_worker.py's
        # module docstring for the full queue/lease/heartbeat/recovery model).
        # Two separate loops, same reason expire_overdue_jobs() and
        # reclaim_expired_leases() are two separate functions: claiming/
        # running jobs and reclaiming abandoned leases are independent
        # concerns on independent intervals (EXECUTION_WORKER_POLL_INTERVAL_SECONDS
        # vs EXECUTION_RECOVERY_INTERVAL_SECONDS) — one starting one asyncio
        # task, not two, would couple their timing for no benefit.
        from src.services.execution_worker import run_worker_loop, run_recovery_loop
        _execution_worker_stop_event = asyncio.Event()
        _execution_worker_task = asyncio.create_task(run_worker_loop(stop_event=_execution_worker_stop_event))
        _execution_recovery_stop_event = asyncio.Event()
        _execution_recovery_task = asyncio.create_task(run_recovery_loop(stop_event=_execution_recovery_stop_event))

        # Commercial Checkout V1 — PaymentOrder expiry sweep (see
        # payment_order_service.py's own docstring). Same in-process
        # asyncio-task-in-lifespan pattern as the three loops above — not a
        # new scheduling mechanism.
        from src.services.payment_order_service import run_payment_order_expiry_loop
        _payment_order_expiry_stop_event = asyncio.Event()
        _payment_order_expiry_task = asyncio.create_task(run_payment_order_expiry_loop(stop_event=_payment_order_expiry_stop_event))

        # Production Payment Activation hardening — webhook replay-nonce
        # retention cleanup (see webhook_replay_service.py's own docstring).
        # Same pattern again — not a new scheduling mechanism.
        from src.services.webhook_replay_service import run_webhook_nonce_cleanup_loop
        _webhook_nonce_cleanup_stop_event = asyncio.Event()
        _webhook_nonce_cleanup_task = asyncio.create_task(run_webhook_nonce_cleanup_loop(stop_event=_webhook_nonce_cleanup_stop_event))

        # Beta hardening — TCU reservation orphan reclaim (see
        # entitlement_service.py's reclaim_expired_reservations()). Same
        # in-process asyncio-task-in-lifespan pattern as the loops above —
        # not a new scheduling mechanism.
        from src.services.entitlement_service import run_tcu_reservation_reclaim_loop
        _tcu_reservation_reclaim_stop_event = asyncio.Event()
        _tcu_reservation_reclaim_task = asyncio.create_task(run_tcu_reservation_reclaim_loop(stop_event=_tcu_reservation_reclaim_stop_event))

        # Generated artifacts (decks) are kept for settings.artifact_retention_days
        # and then removed from object storage together with their rows. Daily
        # sweep — retention is measured in days, so anything more frequent just
        # queries for nothing. Not a security control: a signed URL expires in
        # minutes regardless (see services/artifact_service.py).
        from src.services.artifact_service import run_artifact_retention_loop
        _artifact_retention_stop_event = asyncio.Event()
        _artifact_retention_task = asyncio.create_task(run_artifact_retention_loop(stop_event=_artifact_retention_stop_event))

    yield

    if not settings.testing:
        _schedule_stop_event.set()
        _execution_worker_stop_event.set()
        _execution_recovery_stop_event.set()
        _payment_order_expiry_stop_event.set()
        _webhook_nonce_cleanup_stop_event.set()
        _tcu_reservation_reclaim_stop_event.set()
        _artifact_retention_stop_event.set()
        # Graceful shutdown, bounded — a job still genuinely running past this
        # grace period is simply abandoned mid-attempt; its lease will expire
        # naturally and a future worker (this process's next boot, or another
        # replica) reclaims it via reclaim_expired_leases(). That IS the
        # durability guarantee working as designed, not a gap being papered
        # over — see execution_worker.py's module docstring.
        for _task, _stop_name in (
            (_schedule_task, "schedule"), (_execution_worker_task, "execution_worker"), (_execution_recovery_task, "execution_recovery"),
            (_payment_order_expiry_task, "payment_order_expiry"), (_webhook_nonce_cleanup_task, "webhook_nonce_cleanup"),
            (_tcu_reservation_reclaim_task, "tcu_reservation_reclaim"),
        ):
            try:
                await asyncio.wait_for(_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[SHUTDOWN] %s loop did not stop within grace period — cancelling.", _stop_name)
                _task.cancel()


app = FastAPI(
    title=settings.app_name,
    description="TRINDATRON — Agentic AI Platform API",
    version=settings.app_version,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Security Hardening — cheap, safe defense-in-depth for a JSON API
    handling payments. HSTS/CSP are deliberately NOT set here: this is a
    pure JSON API (no HTML rendering, no cookie-based auth — Bearer JWT
    only, so no cookie-flag/clickjacking surface either), and HSTS is the
    reverse-proxy/CDN's job on this app's deployment target (Railway)."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

app.include_router(health.router, tags=["system"])
app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(workspaces.router)
app.include_router(skills.router)
app.include_router(agent_definitions.router)
app.include_router(memories.router)
app.include_router(billing.router)
app.include_router(voice.router)
app.include_router(admin.router)
app.include_router(execution.router)
app.include_router(agent_instances.router)
app.include_router(presentations.router)
from src.api.routes import account_sessions, me, scheduled_tasks  # noqa: E402 — settings, integrations, scheduling

app.include_router(account_sessions.router)
app.include_router(me.router)
app.include_router(scheduled_tasks.router)
# Service-to-service only: the presentation engine's LLM gateway. Not a
# user-facing surface (see the module docstring) and excluded from the
# public schema at the route level.
app.include_router(presenton_gateway.router)
# Development builds report their startup timeline here; never mounted in production.
if not settings.is_production:
    from src.api.routes import diagnostics

    app.include_router(diagnostics.router)


@app.get("/", tags=["system"])
async def root():
    return {
        "message": f"{settings.app_name} is running",
        "version": settings.app_version,
        "docs": "/docs",
    }
