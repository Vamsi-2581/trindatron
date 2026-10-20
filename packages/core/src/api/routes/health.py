from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.api.deps import get_current_admin_user
from src.config import settings
from src.database import check_db_connection
from src.models.user import User
from src.observability.metrics import combined_snapshot

router = APIRouter()


@router.get("/health", tags=["system"])
async def health_check():
    """Liveness probe — returns ok if the process is alive."""
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/db", tags=["system"])
async def health_db():
    """Readiness probe — returns ok only if the database is reachable."""
    ok = check_db_connection()
    payload = {
        "status": "ok" if ok else "error",
        "database": "connected" if ok else "unreachable",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    status_code = 200 if ok else 503
    return JSONResponse(content=payload, status_code=status_code)


@router.get("/health/metrics", tags=["system"])
async def health_metrics(_admin: User = Depends(get_current_admin_user)):
    """Etapa 5G (Observability) — process-wide metrics snapshot: HTTP
    requests/latency/errors, Request Control/Agent Scheduler rejections,
    Model Router provider attempts/retries/failover/circuit-breaker
    transitions, TCU/cost, Continuity activations, and aggregated
    capability demand (services/demand_engine.py) — everything
    core_metrics/trinity_router.router_metrics have recorded since process
    start (or the last call, values are cumulative, not reset on read).

    ADMIN-ONLY (2026-09-10). This endpoint used to be unauthenticated on
    the argument that every value here is an aggregate with a small, fixed
    label set — which is still true, and is why nothing here can leak a
    prompt, message content, a session/user id, or a credential (that
    invariant is tested, and must stay tested). What the old rationale got
    wrong was its own stated precondition: it said to add auth "if this
    endpoint is ever exposed outside a trusted network", and the service
    runs on a public host, so it already was. Aggregates are not secrets
    but they are business-sensitive — request volume per route,
    registrations, plan mix, which providers/models are chosen and at what
    cost/TCU, and now which capabilities each plan is asking for. That is
    a description of the product's economics, readable by anyone who
    guessed the path.

    Reuses get_current_admin_user (src/api/deps.py) — the codebase's ONE
    admin gate — rather than inventing a metrics token: no new secret to
    provision, rotate, or leak, and no second auth mechanism.

    /health and /health/db stay public deliberately: they carry no
    business data, and railway.toml's healthcheckPath points at /health,
    so the platform probe is unaffected by this change.
    """
    return combined_snapshot()


# Product routers added after src/main.py was frozen — see routes/platform.py.
from src.api.routes.platform import router as _platform_router  # noqa: E402

router.include_router(_platform_router)
