import asyncio
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.config import settings
from src.database import get_db
from src.entitlements import get_plan_entitlement
from src.models.user import User
from src.schemas.agent import AgentRequest
from src.schemas.approval import ApprovalRequest
from src.schemas.message import ChatRequest, SessionDetailResponse
from src.schemas.session import CreateSessionRequest, SessionListResponse, SessionResponse
from src.capability import Capability, classify, infer_execution_mode, route_capability
from src.capability.multimodal import COMING_SOON_MESSAGES
from src.models.message import Message
from src.services.agent_scheduler import AgentSchedulerRejectedError, agent_scheduler, with_execution_timeout
from src.services.agent_service import stream_agent
from src.services.approval_gate import resolve_gate
from src.services.chat_service import stream_chat
from src.services.continuity_entitlement_service import (
    ContinuityBudgetExceededError,
    ContinuityNotEnabledError,
)
from src.services.continuity_mode import (
    ContinuityActivation,
    ContinuityNotApplicable,
    ContinuityRejected,
    activate_continuity_or_raise,
)
from src.services.credit_service import InsufficientCreditError
from src.services.agent_execution_service import create_admitted_execution
from src.services import execution_service
from src.services.entitlement_service import cancel_reservation, check_and_reserve
from src.services.entitlement_service import remaining_budget_tcu as entitlement_remaining_budget_tcu
from src.services.idempotency_service import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    claim_idempotency_key,
    make_fingerprint,
    mark_completed as mark_idempotency_completed,
    mark_failed as mark_idempotency_failed,
)
from src.services.demand_engine import record_demand, record_unmet_demand
from src.services.load_classification import load_classifier
from src.observability.metrics import core_metrics
from src.services.request_control import RequestControlToken, RequestRejectedError, request_control
from src.services.runtime_settings import is_agent_enabled, is_capability_disabled, is_continuity_enabled
from trinity_router import TaskType
from src.services.session_service import (
    ForeignResourceReference,
    create_session,
    delete_session,
    get_session,
    get_sessions,
)
from src.services.usage_service import get_session_usage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP for the IP dimension of rate limiting.

    Security Hardening (INFRA-01, HIGH — final gate revision): an earlier
    version of this function parsed X-Forwarded-For, first trusting the
    FIRST entry, then (after finding that was attacker-controlled)
    trusting the LAST entry instead. Neither position is safe to depend
    on: Railway's own support channel gives CONTRADICTORY answers about
    whether X-Forwarded-For is stripped-and-reset at the edge or appended
    to a client-supplied value, and there is no stable, officially
    documented spec to verify the trusted-hop count against (checked
    directly — station.railway.com's own community threads on this
    contain a Railway employee response and a community response that
    disagree with each other). Rather than guess a hop position again,
    this now prefers X-Real-IP — which Railway documents as "a single
    source of truth for the connecting IP" set by their edge, not a
    hop-counted chain a client can prepend fake entries into — with a
    safe fallback to request.client.host (the actual TCP peer; not
    attacker-forgeable, though it is Railway's own internal proxy address,
    not the real client, whenever a proxy genuinely sits in front — true
    only for local/dev runs with no proxy at all).

    Residual trust assumption (documented, not hidden): this still
    depends on X-Real-IP genuinely being set/overwritten by Railway's edge
    and never passed through verbatim from a client-supplied value — this
    could not be independently verified against a live Railway deployment
    in this environment. The per-email login-rate-limit bucket (keyed on
    the target account, not on any client-supplied network value) remains
    the actual backstop against credential stuffing regardless of this
    header's trustworthiness — see auth.py's _check_login_rate_limit().
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return request.client.host if request.client else None


def _reject_to_http_exception(exc: RequestRejectedError) -> HTTPException:
    """Translate an internal RequestRejectedError into the generic, safe
    429 the client sees — never exposes the internal reason, limit values,
    or any architectural detail (Etapa 5B requirement)."""
    headers = None
    if exc.retry_after_seconds is not None:
        headers = {"Retry-After": str(max(1, int(exc.retry_after_seconds + 0.999)))}
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please try again shortly.",
        headers=headers,
    )


def _scheduler_reject_to_http_exception(exc: AgentSchedulerRejectedError) -> HTTPException:
    """Translate an internal AgentSchedulerRejectedError into a generic,
    safe 429 — same rationale as _reject_to_http_exception (Etapa 5B):
    never exposes the internal reason or limit values."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many concurrent Agent executions. Please try again shortly.",
    )


_kill_switch_rejections_total = core_metrics.counter(
    "kill_switch_rejections_total",
    "Etapa 5H operational kill-switch rejections, labeled by component/reason.",
)


def _reject_if_agent_disabled(db: Session) -> None:
    """Etapa 5H — global Agent kill switch. MUST be called from inside
    each handler's own try/except (never before it) so the caller's
    already-acquired control_token still gets released on the 503 this
    raises — see each call site. Checked BEFORE Agent Scheduler
    acquisition/TCU reservation/provider calls, so a disabled Agent never
    consumes any of those (Request Control's rate-limit/concurrency
    protection still applies unconditionally beforehand, unaffected by
    this switch — see ORDEM DE CONTROLE in the Etapa 5H spec)."""
    if not is_agent_enabled(db):
        logger.warning("kill_switch: agent execution is disabled")
        _kill_switch_rejections_total.inc(component="agent", reason="agent_disabled")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent execution is temporarily disabled. Please try again later.",
        )


def _reject_if_agent_mode_not_entitled(
    user_plan: str, capability=None, task_type=None,
) -> None:
    """Entitlement gate (2026-09-07 — FREE policy closure): Agent mode is a
    per-plan FEATURE flag (PlanEntitlement.supports_agent_mode), not an
    operational kill switch like _reject_if_agent_disabled above — it was
    already declared (False for "free") but never actually checked before
    dispatching to stream_agent()/agent_scheduler.try_acquire(), so a FREE
    user could reach the full ReAct loop, call a real provider, and run
    real tools despite the entitlement layer saying they shouldn't be able
    to. Checked FIRST in both /agent and /message's AGENT branch — before
    session lookup, Agent Scheduler slot acquisition, and TCU reservation
    — so a plan without Agent mode never creates an agent, never calls a
    provider, never executes a tool, and never reserves/spends TCU for
    one. Reuses the SAME structured entitlement-error shape
    (`detail={"error": ..., "message": ...}`) entitlement_service.py's
    daily/weekly/monthly TCU checks already use — no new error mechanism
    invented — with HTTP 403 (a permanent plan restriction, not a
    transient outage like the 503 kill switches above use)."""
    if not get_plan_entitlement(user_plan).supports_agent_mode:
        logger.info("entitlement: agent mode not included in plan | plan=%s", user_plan)
        _kill_switch_rejections_total.inc(component="agent_entitlement", reason=f"plan_no_agent_mode:{user_plan}")
        # Demand Engine — the clearest monetization signal there is: a real
        # request the plan turned away. Aggregate label only, no content.
        record_unmet_demand(
            capability=capability, task_type=task_type,
            reason="agent_mode_not_entitled", user_plan=user_plan,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "agent_mode_not_entitled",
                "message": "Agent mode is not included in your current plan. Upgrade your plan to access it.",
                "plan": user_plan,
            },
        )


def _reject_if_capability_disabled(db: Session, capability: Capability) -> None:
    """Etapa 5H — per-capability kill switch, applied UNIFORMLY in /chat,
    /agent, and /message right after `capability = classify(...)` (before
    session lookup, scheduler, TCU, or provider) — even though only
    /message currently branches its EXECUTOR on capability, gating all
    three endpoints identically closes the loophole where a capability an
    operator disabled could still reach a provider through /chat or
    /agent's classification-for-observability-only path. Same
    "caller's own try/except releases control_token" contract as
    _reject_if_agent_disabled above."""
    if is_capability_disabled(db, capability.value):
        logger.warning("kill_switch: capability disabled | capability=%s", capability.value)
        _kill_switch_rejections_total.inc(component="capability", reason=f"capability_disabled:{capability.value}")
        record_unmet_demand(
            capability=capability, task_type=None,
            reason="capability_disabled", user_plan="",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This capability is temporarily disabled. Please try again later.",
        )


def _log_load_class(
    endpoint: str, capability, task_type: TaskType, user_plan: str = "",
) -> None:
    """Etapa 5C — observability only. Computes and logs the LoadClass for
    this request; nothing downstream reads it. Not wired into Request
    Control's limits, TCU, or any behavioral branch — see
    load_classification.py's module docstring for why. Purely additive,
    never raises (LoadClassifier.classify() cannot fail).

    Also the single point where all three request endpoints (/chat,
    /agent, /message) have already converged on a classified capability,
    so it is where the Demand Engine records that demand — one aggregate
    counter increment, no content, no identifiers (see
    services/demand_engine.py). Recorded for the request as ASKED, before
    any downstream gate can refuse it, so refusals stay a strict subset."""
    load_class = load_classifier.classify(capability, forced_task_type=task_type)
    logger.info(
        "load_class computed | endpoint=%s capability=%s task_type=%s load_class=%s",
        endpoint, capability.value, task_type.value, load_class.value,
    )
    record_demand(
        endpoint=endpoint, capability=capability, task_type=task_type, user_plan=user_plan,
    )


_CONTINUITY_DECLINE_EXCEPTIONS = (
    ContinuityNotApplicable,
    ContinuityRejected,
    ContinuityNotEnabledError,
    ContinuityBudgetExceededError,
    InsufficientCreditError,
)

# Etapa 5G — the ONE place every possible Continuity activation outcome
# converges (activated, or one of the five decline reasons above), so this
# is where a single unified event/metric is recorded rather than scattering
# partial "outcome" metrics across continuity_mode.py/
# continuity_entitlement_service.py (which record their own narrower,
# already-existing counters for the financial/budget-specific outcomes —
# see those modules). Purely observational: never changes which branch
# raises or what the client receives.
_continuity_activations_total = core_metrics.counter(
    "continuity_activations_total", "Continuity Mode activation attempts, labeled by plan/outcome.",
)

_CONTINUITY_OUTCOME_BY_EXCEPTION: dict[type[Exception], str] = {
    ContinuityNotApplicable: "not_applicable",
    ContinuityNotEnabledError: "not_enabled",
    ContinuityBudgetExceededError: "budget_exceeded",
    InsufficientCreditError: "insufficient_balance",
}


def _continuity_outcome_label(exc: Exception) -> str:
    """`ContinuityRejected.reason` (MULTIMODAL_CAPABILITY | NO_ELIGIBLE_MODEL)
    already distinguishes WHY Continuity itself declined — reused verbatim
    here instead of collapsing both into one generic "rejected" label."""
    if isinstance(exc, ContinuityRejected):
        return f"rejected_{exc.reason.value}"
    for exc_type, label in _CONTINUITY_OUTCOME_BY_EXCEPTION.items():
        if isinstance(exc, exc_type):
            return label
    return "declined_unknown"  # pragma: no cover — exhaustive over _CONTINUITY_DECLINE_EXCEPTIONS


async def _try_continuity_or_reraise(
    db: Session,
    current_user: User,
    exc: HTTPException,
    capability: Capability,
    *,
    requires_tools: bool = False,
) -> ContinuityActivation:
    """Called ONLY from the `except HTTPException` handler around
    check_and_reserve() — Etapa 5F. Attempts Continuity Mode as a
    controlled degraded path; RE-RAISES THE ORIGINAL EXCEPTION UNCHANGED
    (never masks it) whenever Continuity does not apply, is not enabled
    for the plan, has no budget left, or the user's credit balance can't
    cover it — so the client gets exactly the same 429 they would today
    with no Continuity Mode at all. This is the ONE place a quota
    rejection is ever given a second chance; every other rejection
    (Request Control, Agent Scheduler, provider failures — Etapa 5E) never
    reaches this function at all.

    estimated_tcu starts from check_and_reserve()'s own conservative
    estimate (entitlement.max_tcu_per_request) — the same "worst case, one
    model call" ceiling already used for the premium reservation — but is
    then clamped to entitlement.continuity_daily_tcu_limit. This clamp is
    load-bearing, not cosmetic: max_tcu_per_request is calibrated for a
    premium-tier model call and, for pro/max/team, is LARGER than those
    same plans' own Continuity daily budget (50/200/200 vs. 30/75/150 —
    see entitlements.py's official values). Without the clamp, the very
    first Continuity attempt of the day on those plans would ask
    check_and_debit_continuity() to reserve more than the entire day's
    Continuity allowance and be rejected by ContinuityBudgetExceededError
    every time — Continuity would be permanently unreachable on 3 of 4
    paid plans. The clamp invents no new business number — it only bounds
    an existing estimate by an existing, already-approved ceiling — and it
    stays safe/conservative because the exact same "reserve conservative
    up front, true_up_continuity() back down to the real cost afterward"
    pattern already governs the premium max_tcu_per_request/daily_tcu_limit
    relationship, applied here to Continuity's own pair of numbers instead.

    requires_tools=True MUST be passed by /agent and /message's AGENT
    branch: stream_agent()'s ReAct loop always calls RoutingRequest with
    a non-empty `tools` list (Continuity only removes the three browser
    tools — read_file/write_file/execute_shell/web_search stay available,
    see agent_service.py's docstring), so the economic model
    select_continuity_model() picks must actually support tool-calling —
    otherwise a forced_provider_key pinned to a non-tool-capable model
    would silently break the ReAct loop (no failover is possible once
    pinned). /chat never sets tools, so its default False is correct.
    """
    # Etapa 5H — global Continuity kill switch. Checked FIRST, before any
    # other Continuity code runs (cheaper than even the eligibility/model
    # checks activate_continuity_or_raise() itself orders first) — when
    # disabled, this is indistinguishable from Continuity "not applying"
    # at all: `exc` (the ORIGINAL 429) is re-raised unchanged, no financial
    # code runs, no provider is ever considered. Never wired as a
    # TCU-bypass: disabling Continuity only ever makes the pre-existing
    # 429 behavior (Etapa 5B/5C, unaffected by Etapa 5F) the final answer
    # again, exactly as it was before Continuity Mode existed.
    if not await asyncio.to_thread(is_continuity_enabled, db):
        logger.warning("kill_switch: continuity mode is disabled")
        _kill_switch_rejections_total.inc(component="continuity", reason="continuity_disabled")
        _continuity_activations_total.inc(plan=current_user.plan, outcome="continuity_disabled")
        raise exc from None

    try:
        entitlement = get_plan_entitlement(current_user.plan)
        estimated_tcu = min(entitlement.max_tcu_per_request, entitlement.continuity_daily_tcu_limit)
        activation = await asyncio.to_thread(
            activate_continuity_or_raise,
            db,
            user_id=current_user.id,
            plan=current_user.plan,
            capability=capability,
            quota_rejection_detail=exc.detail,
            estimated_tcu=estimated_tcu,
            execution_id=str(uuid.uuid4()),
            requires_tools=requires_tools,
        )
    except _CONTINUITY_DECLINE_EXCEPTIONS as decline_exc:
        _continuity_activations_total.inc(
            plan=current_user.plan, outcome=_continuity_outcome_label(decline_exc),
        )
        raise exc from None
    _continuity_activations_total.inc(plan=current_user.plan, outcome="activated")
    return activation


async def _enter_request_control(request: Request, user_id: str) -> RequestControlToken:
    """Rate-limit check + concurrency slot acquisition, in that order (see
    RequestControl's own docstring for why). Raises HTTPException(429)
    directly — callers don't need their own try/except for this step."""
    try:
        await request_control.check_rate_limits(user_id=user_id, client_ip=_client_ip(request))
        return request_control.acquire_concurrency(user_id)
    except RequestRejectedError as exc:
        raise _reject_to_http_exception(exc) from exc


async def _claim_idempotency_or_raise(
    db: Session, request: Request, current_user: User, *, endpoint: str, session_id: str, content: str,
) -> tuple[str | None, JSONResponse | None]:
    """Etapa 5I — the ONE idempotency entry point every handler calls as
    the FIRST statement inside its own try block (before session lookup,
    kill switches, TCU) — see idempotency_service.py's module docstring
    for the full contract.

    Returns (claim_id, None) — proceed normally, thread claim_id into
    stream_chat()/stream_agent() — when either no Idempotency-Key header
    was sent (claim_id is None, zero behavior change) or a FRESH claim was
    made. Returns (None, JSONResponse) when the SAME key already
    represents a COMPLETED operation — the caller MUST return that
    response immediately and run nothing.

    Raises HTTPException(409) directly for an in-progress conflict or an
    incompatible reuse — the caller's own `except Exception:` block (which
    every handler already has, releasing control_token/scheduler_token)
    handles cleanup exactly like any other pre-existing 429/404 path.
    """
    key = request.headers.get("idempotency-key")
    if not key:
        return None, None

    fingerprint = make_fingerprint(endpoint=endpoint, session_id=session_id, content=content)
    try:
        result = await asyncio.to_thread(
            claim_idempotency_key,
            db, user_id=current_user.id, key=key, endpoint=endpoint, fingerprint=fingerprint,
        )
    except IdempotencyInProgressError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A request with this Idempotency-Key is already being processed.",
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Idempotency-Key was already used for a different operation.",
        ) from None

    if isinstance(result, IdempotencyReplay):
        return None, JSONResponse(status_code=status.HTTP_200_OK, content={
            "idempotent_replay": True,
            "session_id": result.session_id,
            "message_id": result.message_id,
            "status": "completed",
        })
    return result, None


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        session = create_session(
            db,
            user_id=current_user.id,
            title=body.title,
            model=body.model,
            project_id=body.project_id,
            agent_definition_id=body.agent_definition_id,
            is_temporary=body.is_temporary,
        )
    except ForeignResourceReference:
        # Same "404, never reveal existence for another user" convention
        # already used by every other cross-user ownership check in this
        # codebase (see PaymentOrderService._get_or_raise() and friends).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project or agent definition not found")
    return session


@router.get("", response_model=SessionListResponse)
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sessions = get_sessions(db, user_id=current_user.id)
    return SessionListResponse(sessions=sessions, total=len(sessions))


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = get_session(db, session_id=session_id, user_id=current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not delete_session(db, session_id=session_id, user_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


@router.delete("/{session_id}/messages/from/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_messages_from(
    session_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a message and all subsequent messages in the session (edit-and-resend support)."""
    session = get_session(db, session_id=session_id, user_id=current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    msg = db.query(Message).filter_by(id=message_id, session_id=session.id).first()
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    db.query(Message).filter(
        Message.session_id == session.id,
        Message.created_at >= msg.created_at,
    ).delete(synchronize_session=False)
    db.commit()


@router.post("/{session_id}/chat")
async def chat(
    request: Request,
    session_id: str,
    body: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Request Control (Etapa 5B) runs first — before any DB work, before TCU
    # reservation, before the provider is ever considered. Rejected requests
    # never reach check_and_reserve, so they never consume TCU.
    control_token = await _enter_request_control(request, current_user.id)

    # Etapa 5C — /chat always executes stream_chat() (no tools, ever),
    # independent of what capability body.content resembles; task_type is
    # therefore forced to CHAT rather than derived. Observability only.
    capability = classify(body.content)
    _log_load_class("/chat", capability, TaskType.CHAT, current_user.plan)

    continuity: ContinuityActivation | None = None
    idempotency_claim_id: str | None = None
    try:
        # Etapa 5I — first persistent effect of this request, before
        # anything else (including the Etapa 5H checks right below). A
        # request with no Idempotency-Key header is completely unaffected
        # (idempotency_claim_id stays None, idempotency_replay is always
        # None too). See _claim_idempotency_or_raise's own docstring.
        idempotency_claim_id, idempotency_replay = await _claim_idempotency_or_raise(
            db, request, current_user, endpoint="chat", session_id=session_id, content=body.content,
        )
        if idempotency_replay is not None:
            control_token.release()
            return idempotency_replay

        # Etapa 5H — cheapest possible check, before session lookup/TCU/
        # provider. See _reject_if_capability_disabled's own docstring.
        await asyncio.to_thread(_reject_if_capability_disabled, db, capability)

        session = await asyncio.to_thread(get_session, db, session_id=session_id, user_id=current_user.id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        # ENFORCEMENT: check quota and reserve BEFORE returning StreamingResponse.
        # If limit exceeded, HTTPException(429) is raised here — HTTP 200 is NOT sent.
        # Provider is never called when quota is exceeded.
        try:
            reservation_id = await asyncio.to_thread(
                check_and_reserve,
                db,
                user_id=current_user.id,
                session_id=session.id,
                plan=current_user.plan,
                user_timezone=current_user.timezone,
            )
        except HTTPException as exc:
            # Etapa 5F — the ONE point a quota rejection gets a second,
            # controlled chance: Continuity Mode. Re-raises `exc` unchanged
            # (falls through to the normal 429) whenever Continuity doesn't
            # apply/isn't enabled/has no budget/can't be financed — this
            # now includes the Etapa 5H Continuity kill switch, checked
            # first inside _try_continuity_or_reraise.
            continuity = await _try_continuity_or_reraise(db, current_user, exc, capability)
            reservation_id = None
    except Exception:
        # Neither 404 nor a later TCU rejection should leave the
        # concurrency slot held — nothing past this point will ever call
        # stream_chat() (whose own `finally` is the normal release path).
        # Etapa 5I — a claimed-but-never-started key must also be marked
        # "failed" here so a retry is treated as a fresh attempt, not
        # stuck "in_progress" until the stale-reconciliation window passes.
        if idempotency_claim_id is not None:
            await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
        control_token.release()
        raise

    if continuity is not None:
        return StreamingResponse(
            stream_chat(
                db, session, body.content,
                user_plan=current_user.plan,
                request_control_token=control_token,
                capability=capability,
                forced_provider_key=continuity.provider_key,
                continuity_execution_id=continuity.execution_id,
                continuity_estimated_tcu=continuity.estimated_tcu,
                idempotency_claim_id=idempotency_claim_id,
                client_locale=body.locale,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return StreamingResponse(
        stream_chat(
            db, session, body.content, body.model,
            user_plan=current_user.plan,
            reservation_id=reservation_id,
            request_control_token=control_token,
            idempotency_claim_id=idempotency_claim_id,
            client_locale=body.locale,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/agent")
async def agent(
    request: Request,
    session_id: str,
    body: AgentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Request Control (Etapa 5B) runs first — same rationale as /chat.
    control_token = await _enter_request_control(request, current_user.id)

    # Etapa 5C — /agent always executes stream_agent()'s full ReAct loop,
    # independent of what capability body.content resembles; task_type is
    # therefore forced to AGENT rather than derived. Observability only.
    capability = classify(body.content)
    _log_load_class("/agent", capability, TaskType.AGENT, current_user.plan)

    # Etapa 5D — Agent Scheduler runs BEFORE the TCU reservation:
    # Request Control -> Agent Scheduler -> TCU -> Agent execution.
    # Deliberately in that order so a scheduler rejection never touches
    # TCU at all — there is nothing to cancel, mirroring Request Control's
    # own rejection-never-reserves guarantee from Etapa 5B. Etapa 5F does
    # NOT change this order: Continuity is only ever considered AFTER a
    # TCU (quota) rejection, so it can never let a request bypass either
    # Request Control or the Agent Scheduler — both already ran by then.
    scheduler_token = None
    continuity: ContinuityActivation | None = None
    idempotency_claim_id: str | None = None
    try:
        # Etapa 5I — first persistent effect, before everything else
        # (same placement/rationale as /chat).
        idempotency_claim_id, idempotency_replay = await _claim_idempotency_or_raise(
            db, request, current_user, endpoint="agent", session_id=session_id, content=body.content,
        )
        if idempotency_replay is not None:
            control_token.release()
            return idempotency_replay

        # Etapa 5H — capability + global Agent kill switches, both before
        # session lookup/Scheduler/TCU/provider. Order between the two
        # doesn't matter (either alone is sufficient to reject); capability
        # first only because it's the uniform check shared with /chat.
        await asyncio.to_thread(_reject_if_capability_disabled, db, capability)
        await asyncio.to_thread(_reject_if_agent_disabled, db)
        # Entitlement gate (2026-09-07) — Agent mode as a PLAN FEATURE
        # (distinct from the operational kill switch above): a plan
        # without supports_agent_mode never reaches session lookup,
        # Agent Scheduler, or TCU reservation below.
        _reject_if_agent_mode_not_entitled(current_user.plan, capability, TaskType.AGENT)

        session = await asyncio.to_thread(get_session, db, session_id=session_id, user_id=current_user.id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        try:
            # Plan concurrency (max_agents) checked BEFORE the scheduler's
            # own global/per-user gate — same exception, same translator.
            agent_scheduler.check_plan_concurrency(
                current_user.id, get_plan_entitlement(current_user.plan).max_agents,
            )
            scheduler_token = agent_scheduler.try_acquire(current_user.id)
        except AgentSchedulerRejectedError as exc:
            raise _scheduler_reject_to_http_exception(exc) from exc

        # ENFORCEMENT: identical to /chat. Provider is never called if quota exceeded.
        # AGENT OVERSHOOT NOTE: reservation = max_tcu_per_request (one model call).
        # An agent session runs up to agent_max_iterations calls; real TCU may exceed
        # reservation by up to max_tcu_per_request × (max_iterations - 1). This is a
        # known MVP limitation documented in the Fase 3 risk register.
        try:
            reservation_id = await asyncio.to_thread(
                check_and_reserve,
                db,
                user_id=current_user.id,
                session_id=session.id,
                plan=current_user.plan,
                user_timezone=current_user.timezone,
            )
        except HTTPException as exc:
            # Etapa 5F — same controlled second chance as /chat. /agent
            # remains HEAVY regardless: Agent Scheduler slot was already
            # acquired above and is passed through unchanged below, so
            # Continuity can never bypass it.
            continuity = await _try_continuity_or_reraise(
                db, current_user, exc, capability, requires_tools=True,
            )
            reservation_id = None
    except Exception:
        # Neither 404, a scheduler rejection, nor a later TCU rejection
        # should leave either slot held — nothing past this point will
        # ever call stream_agent() (whose own `finally` is the normal
        # release path for both tokens).
        if idempotency_claim_id is not None:
            await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
        if scheduler_token is not None:
            scheduler_token.release()
        control_token.release()
        raise

    # Etapa 5I — admission is fully confirmed at this point (scheduler
    # slot held, TCU reserved or Continuity activated); this is exactly
    # where "admitted" starts, before stream_agent() is ever constructed.
    agent_execution_id = await asyncio.to_thread(
        create_admitted_execution,
        db, user_id=current_user.id, session_id=session.id,
        reservation_id=reservation_id,
        continuity_execution_id=continuity.execution_id if continuity else None,
        idempotency_key_id=idempotency_claim_id,
    )

    if continuity is not None:
        return StreamingResponse(
            with_execution_timeout(
                stream_agent(
                    db, session, body.content,
                    user_plan=current_user.plan,
                    request_control_token=control_token,
                    agent_scheduler_token=scheduler_token,
                    capability=capability,
                    forced_provider_key=continuity.provider_key,
                    continuity_execution_id=continuity.execution_id,
                    continuity_estimated_tcu=continuity.estimated_tcu,
                    idempotency_claim_id=idempotency_claim_id,
                    client_locale=body.locale,
                    agent_execution_id=agent_execution_id,
                ),
                timeout_s=settings.agent_scheduler_execution_timeout_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return StreamingResponse(
        with_execution_timeout(
            stream_agent(
                db, session, body.content,
                user_plan=current_user.plan,
                reservation_id=reservation_id,
                user_timezone=current_user.timezone,
                request_control_token=control_token,
                agent_scheduler_token=scheduler_token,
                idempotency_claim_id=idempotency_claim_id,
                client_locale=body.locale,
                agent_execution_id=agent_execution_id,
            ),
            timeout_s=settings.agent_scheduler_execution_timeout_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/agent/approve", status_code=status.HTTP_200_OK)
async def approve_agent_action(
    session_id: str,
    body: ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resolves a CONFIRM-level tool call's approval gate (write_file/
    execute_shell — see agent_service.py's _permission_gateway). Mirrors
    /workspaces/work/{workspace_id}/approve exactly, session-scoped
    instead of workspace-scoped — same shared approval_gate module,
    same double ownership check (session lookup here, then
    resolve_gate's own owner_id match)."""
    session = await asyncio.to_thread(get_session, db, session_id=session_id, user_id=current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    found = resolve_gate(body.action_id, approved=True, owner_id=session_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval gate not found or already resolved",
        )
    return {"status": "approved"}


@router.post("/{session_id}/agent/reject", status_code=status.HTTP_200_OK)
async def reject_agent_action(
    session_id: str,
    body: ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = await asyncio.to_thread(get_session, db, session_id=session_id, user_id=current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    found = resolve_gate(body.action_id, approved=False, owner_id=session_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval gate not found or already resolved",
        )
    return {"status": "rejected"}


def _persist_coming_soon_message_pair(
    db: Session,
    session_id: str,
    user_content: str,
    coming_soon: str,
    execution_job_id: str | None = None,
) -> tuple[str | None, str]:
    """Synchronous DB work for the /message coming-soon branch — called via
    asyncio.to_thread() so it runs off the event loop (Etapa 5A). Preserves
    the original best-effort behavior: on any DB failure, still returns a
    usable (None, "coming-soon") pair rather than raising."""
    try:
        user_msg = Message(session_id=session_id, role="user", content=user_content)
        db.add(user_msg)
        assistant_msg = Message(session_id=session_id, role="assistant", content=coming_soon,
                                execution_job_id=execution_job_id)
        db.add(assistant_msg)
        db.commit()
        return user_msg.id, assistant_msg.id
    except Exception:
        return None, "coming-soon"


#: What the chat says about a deck, in the person's language.
_DECK_REPLIES = {
    "pt": {
        "plan": "Criar apresentações está disponível nos planos pagos.",
        "unavailable": "Não consigo criar apresentações agora — o serviço está temporariamente indisponível. "
                       "Tente de novo em instantes.",
        "capacity": "Não há capacidade suficiente na sua janela de uso atual para criar uma apresentação. "
                    "Ela volta a ficar disponível quando a janela renovar.",
        "failed": "Não consegui começar essa apresentação. Tente de novo.",
        "started": "Estou criando sua apresentação de {slides} slides. Isso leva alguns minutos — "
                   "o arquivo aparece aqui para baixar quando ficar pronto.",
    },
    "en": {
        "plan": "Building presentations is available on the paid plans.",
        "unavailable": "I can't build presentations right now — the service is temporarily unavailable. "
                       "Please try again shortly.",
        "capacity": "There isn't enough capacity left in your current usage window to build a presentation. "
                    "It will be available again when the window resets.",
        "failed": "I couldn't start building that presentation. Please try again.",
        "started": "I'm building your {slides}-slide presentation now. It takes a few minutes — "
                   "I'll have the file ready for you to download when it's done.",
    },
    "es": {
        "plan": "Crear presentaciones está disponible en los planes de pago.",
        "unavailable": "No puedo crear presentaciones ahora — el servicio no está disponible temporalmente. "
                       "Inténtalo de nuevo en unos momentos.",
        "capacity": "No queda capacidad suficiente en tu ventana de uso actual para crear una presentación. "
                    "Volverá a estar disponible cuando la ventana se renueve.",
        "failed": "No pude empezar esa presentación. Inténtalo de nuevo.",
        "started": "Estoy creando tu presentación de {slides} diapositivas. Tarda unos minutos — "
                   "el archivo aparecerá aquí para descargar cuando esté listo.",
    },
}

#: The language the deck itself is written in, as the engine takes it.
_DECK_LANGUAGES = {"pt": "Portuguese (Brazil)", "en": "English", "es": "Spanish"}

DEFAULT_SLIDES = 10

_NUMBER_WORDS = {
    "tres": 3, "três": 3, "three": 3, "quatro": 4, "cuatro": 4, "four": 4, "cinco": 5, "five": 5,
    "seis": 6, "six": 6, "sete": 7, "siete": 7, "seven": 7, "oito": 8, "ocho": 8, "eight": 8,
    "nove": 9, "nueve": 9, "nine": 9, "dez": 10, "diez": 10, "ten": 10, "doze": 12, "doce": 12, "twelve": 12,
    "quinze": 15, "quince": 15, "fifteen": 15, "vinte": 20, "veinte": 20, "twenty": 20,
}
_SLIDE_COUNT = re.compile(
    r"\b(\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")[\s-]+"
    r"(?:slides?|l[âa]minas?|p[áa]ginas?|telas?|diapositivas?|pages?)\b",
    re.IGNORECASE,
)


def slides_asked(brief: str) -> int | None:
    """The slide count the request names ("5 slides", "cinco lâminas", "12 diapositivas"), or None.

    Only a number directly followed by a slide word counts: "os 5 maiores carros" names no slide count, so the
    deck keeps the default rather than a guess.
    """
    match = _SLIDE_COUNT.search(brief)
    if not match:
        return None
    word = match.group(1).lower()
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word)


async def _start_presentation_job(db, current_user, session, brief: str, language: str | None = None):
    """Queue a deck for this chat turn, or explain why not.

    Returns (reply_text, job_id | None). Never raises: this runs inside a
    chat turn, so every outcome has to be a sentence the user can read. The
    refusals mirror POST /presentations exactly — the two entry points must
    not disagree about who may create a deck.

    The reply and the deck are in the person's language (chat_media.language_for:
    their Settings choice, else their device, else the words they wrote). The
    slide count is the one the request names, held to what the plan and the
    remaining capacity allow; without one it is DEFAULT_SLIDES.
    """
    from src.services import chat_media, presentation_service
    from src.api.routes.presentations import MAX_SLIDES, MIN_SLIDES

    lang = chat_media.language_for(brief, language)
    words = _DECK_REPLIES.get(lang, _DECK_REPLIES["pt"])

    if not presentation_service.plan_allows_presentations(current_user.plan):
        return (words["plan"], None)

    engine = await presentation_service.availability()
    if not engine.available:
        logger.info("presentation: chat request refused | reason=%s", engine.reason)
        return (words["unavailable"], None)

    n_slides = max(MIN_SLIDES, min(slides_asked(brief) or DEFAULT_SLIDES, MAX_SLIDES))
    remaining = await asyncio.to_thread(
        entitlement_remaining_budget_tcu, db, current_user.id, current_user.plan,
        current_user.timezone,
    )
    if remaining is not None:
        affordable = presentation_service.largest_affordable_deck(
            plan=current_user.plan, remaining_tcu=remaining
        )
        if affordable < MIN_SLIDES:
            return (words["capacity"], None)
        n_slides = min(n_slides, affordable)

    try:
        job = await asyncio.to_thread(
            execution_service.create_job,
            db,
            user_id=current_user.id,
            plan=current_user.plan,
            user_timezone=current_user.timezone,
            session_id=session.id,
            job_type="presentation",
            payload={
                "steps": [{"command": brief}],
                "presentation": {"n_slides": n_slides, "language": _DECK_LANGUAGES.get(lang, "Portuguese (Brazil)"),
                                 "export_as": "pptx", "web_search": False},
            },
            priority="normal",
            timeout_seconds=1800,
            max_retries=1,
        )
    except Exception:
        logger.exception("presentation: could not queue a deck from chat")
        return (words["failed"], None)

    return (words["started"].format(slides=n_slides), job.id)


@router.post("/{session_id}/message")
async def message(
    request: Request,
    session_id: str,
    body: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Unified message endpoint — Capability Router decides the execution pipeline.

    Classifies the user's content into a Capability, then dispatches to:
    - stream_chat()  when TaskType is CHAT (conversational, no tools)
    - stream_agent() when TaskType is AGENT (ReAct loop with tool calls)

    Execution Mode etapa (2026-09-07): task_type is no longer a fixed
    function of capability alone (that conflated WHAT a request is about
    with HOW it needs to be executed — see pipelines.py's module
    docstring). infer_execution_mode() checks the request's own execution
    intent for CHAT/CODE/RESEARCH (FILES/WORK/AUTOMATION/AGENT stay
    unconditionally AGENT, since their own trigger phrases already imply
    real action).

    The /chat and /agent endpoints are kept for backward compatibility with the
    Electron desktop client and direct API consumers.
    """
    # Request Control (Etapa 5B) runs first — same rationale as /chat and /agent.
    control_token = await _enter_request_control(request, current_user.id)

    capability = classify(body.content)
    # A "sim" to the offer TRINDATRON just made (a video in premium, e.g. one with sound) is that request,
    # confirmed: it is created now instead of reaching a model that would only talk about it.
    from src.services import chat_media as _chat_media

    offered_request = _chat_media.accepted_offer(db, current_user.id, session_id, body.content)
    if offered_request is not None:
        capability = Capability.VIDEO
    capability_route = route_capability(capability)
    # A question about the present escalates to the tool loop (pipelines.py), but only where tools
    # exist: on a plan without agent mode the turn stays a conversation that admits its knowledge may
    # be old, rather than becoming a 403 for asking what is new.
    task_type = infer_execution_mode(
        capability=capability,
        messages=[{"role": "user", "content": body.content}],
        agent_available=bool(get_plan_entitlement(current_user.plan).supports_agent_mode),
    )

    # Etapa 5C — observability only.
    _log_load_class("/message", capability, task_type, current_user.plan)

    # Etapa 5D — Agent Scheduler is acquired ONLY when this request will
    # actually dispatch to stream_agent() (task_type == AGENT) — gated by
    # executor, never by LoadClass. A FILES capability classifies MEDIUM
    # in Etapa 5C but still runs the identical HEAVY ReAct loop as any
    # other AGENT-task-type capability, so it must still go through the
    # scheduler (see agent_scheduler.py's module docstring). Runs before
    # the TCU reservation, same rationale as /agent.
    scheduler_token = None
    continuity: ContinuityActivation | None = None
    idempotency_claim_id: str | None = None
    try:
        # Etapa 5I — first persistent effect, before everything else
        # (same placement/rationale as /chat and /agent).
        idempotency_claim_id, idempotency_replay = await _claim_idempotency_or_raise(
            db, request, current_user, endpoint="message", session_id=session_id, content=body.content,
        )
        if idempotency_replay is not None:
            control_token.release()
            return idempotency_replay

        # Etapa 5H — capability kill switch applies regardless of which
        # executor task_type resolves to (uniform with /chat and /agent).
        # The Agent kill switch is checked ONLY when this request will
        # actually reach stream_agent() — a CHAT-routed /message request
        # never touches Agent at all, so agent_enabled=False must not
        # reject it.
        await asyncio.to_thread(_reject_if_capability_disabled, db, capability)

        session = await asyncio.to_thread(get_session, db, session_id=session_id, user_id=current_user.id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if task_type == TaskType.AGENT:
            await asyncio.to_thread(_reject_if_agent_disabled, db)
            # Entitlement gate (2026-09-07) — same as /agent's identical
            # call site: a plan without supports_agent_mode never reaches
            # Agent Scheduler/TCU/stream_agent() via /message either.
            _reject_if_agent_mode_not_entitled(current_user.plan, capability, task_type)
            try:
                agent_scheduler.check_plan_concurrency(
                    current_user.id, get_plan_entitlement(current_user.plan).max_agents,
                )
                scheduler_token = agent_scheduler.try_acquire(current_user.id)
            except AgentSchedulerRejectedError as exc:
                raise _scheduler_reject_to_http_exception(exc) from exc

        try:
            reservation_id = await asyncio.to_thread(
                check_and_reserve,
                db,
                user_id=current_user.id,
                session_id=session.id,
                plan=current_user.plan,
                user_timezone=current_user.timezone,
            )
        except HTTPException as exc:
            # Etapa 5F — same controlled second chance as /chat and
            # /agent. A multimodal capability (the only case
            # capability_route.implemented is False) is always rejected
            # by activate_continuity_or_raise's TEXT-ONLY gate before any
            # financial call, so `continuity is not None` below and
            # `not capability_route.implemented` below are mutually
            # exclusive by construction — the coming-soon branch never
            # needs to account for a Continuity-activated reservation.
            continuity = await _try_continuity_or_reraise(
                db, current_user, exc, capability, requires_tools=(task_type == TaskType.AGENT),
            )
            reservation_id = None
    except Exception:
        if idempotency_claim_id is not None:
            await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
        if scheduler_token is not None:
            scheduler_token.release()
        control_token.release()
        raise

    _sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    # ── Image / video — a real media job, never a model describing one ──
    # The request goes through the same path as POST /media/images|videos
    # (plan, daily count, engine, storage, per-request ceiling, reservation).
    # The chat turn's reservation is released: the job reserves its own, sized
    # to the engine that will run it, so one creation is never charged twice.
    if capability in (Capability.IMAGE, Capability.VIDEO):
        from src.services import chat_media

        if reservation_id:
            await asyncio.to_thread(cancel_reservation, db, reservation_id)
        media_kind = "image" if capability is Capability.IMAGE else "video"
        from src.services import preferences_service

        media_language = await asyncio.to_thread(
            preferences_service.answer_language, db, current_user.id, body.locale,
        )
        media_reply, media_job = await chat_media.start_from_chat(
            db, current_user, offered_request or body.content, media_kind, session_id=session.id,
            language=media_language or None, confirmed=offered_request is not None,
        )
        media_artifact = chat_media.artifact_event(db, media_job) if media_job is not None else None
        media_job_id = media_job.id if media_job is not None else None

        async def _media_stream():
            import json
            try:
                user_msg_id, asst_msg_id = await asyncio.to_thread(
                    chat_media.persist_turn, db, session.id, body.content, media_reply, media_job_id,
                )
                if idempotency_claim_id is not None:
                    if asst_msg_id is not None:
                        await asyncio.to_thread(
                            mark_idempotency_completed, db, idempotency_claim_id,
                            session_id=session.id, message_id=asst_msg_id,
                        )
                    else:
                        await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
                # The reply travels as `answer` on done (like every non-model reply), never also as text.
                if media_artifact is not None:
                    yield f"data: {json.dumps({'type': 'artifact', 'artifact': media_artifact})}\n\n"
                payload = {
                    "type": "done", "session_id": session.id, "message_id": asst_msg_id or "",
                    "user_message_id": user_msg_id, "answer": media_reply, "iterations": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "duration_ms": 0},
                }
                if media_job_id:
                    payload["media_job_id"] = media_job_id
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                control_token.release()

        return StreamingResponse(_media_stream(), media_type="text/event-stream", headers=_sse_headers)

    # ── Unimplemented capability — return coming-soon without hitting the LLM ──
    if not capability_route.implemented:
        # task_type is CHAT for every UNIMPLEMENTED_CAPABILITIES member
        # (IMAGE/VIDEO/AUDIO — see pipelines.py's CAPABILITY_TASK_TYPE), so
        # scheduler_token is always None here; nothing to release beyond
        # control_token below.
        await asyncio.to_thread(cancel_reservation, db, reservation_id)
        coming_soon = COMING_SOON_MESSAGES.get(
            capability, "Esta capacidade estará disponível em breve."
        )

        async def _coming_soon_stream():
            import json
            try:
                user_msg_id, asst_msg_id = await asyncio.to_thread(
                    _persist_coming_soon_message_pair, db, session.id, body.content, coming_soon,
                )
                # Etapa 5I — mirrors stream_agent()'s own coming-soon branch:
                # mark completed only on a REAL persisted effect; the
                # "coming-soon" fallback string means persistence itself
                # failed, so the claim is left retryable instead.
                if idempotency_claim_id is not None:
                    if asst_msg_id != "coming-soon":
                        await asyncio.to_thread(
                            mark_idempotency_completed, db, idempotency_claim_id,
                            session_id=session.id, message_id=asst_msg_id,
                        )
                    else:
                        await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.id, 'message_id': asst_msg_id, 'user_message_id': user_msg_id, 'answer': coming_soon, 'iterations': 0, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'tcu': 0.0, 'duration_ms': 0}})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                control_token.release()

        return StreamingResponse(
            _coming_soon_stream(),
            media_type="text/event-stream",
            headers=_sse_headers,
        )

    # ── Presentation — hand off to a durable job, do not answer in text ──
    # PRESENTATION classifies as TaskType.AGENT, so without this branch a
    # deck request would fall through to stream_agent() and be answered with
    # a description of a presentation instead of a presentation. The work is
    # minutes long and produces a file, which is a job, not a chat turn.
    #
    # The chat reservation is cancelled rather than carried: the job makes its
    # own, sized to the estimated deck (see execution/presentation_provider.py).
    # Keeping both would charge the user twice for one deck.
    if capability is Capability.PRESENTATION:
        await asyncio.to_thread(cancel_reservation, db, reservation_id)
        if scheduler_token is not None:
            scheduler_token.release()

        from src.services import preferences_service

        deck_language = await asyncio.to_thread(
            preferences_service.answer_language, db, current_user.id, body.locale,
        )
        reply, job_id = await _start_presentation_job(db, current_user, session, body.content,
                                                      language=deck_language or None)

        async def _presentation_stream():
            import json
            try:
                # Linked to the job, so the card (progress, then the file) survives reopening the chat.
                user_msg_id, asst_msg_id = await asyncio.to_thread(
                    _persist_coming_soon_message_pair, db, session.id, body.content, reply, job_id,
                )
                if idempotency_claim_id is not None:
                    if asst_msg_id != "coming-soon":
                        await asyncio.to_thread(
                            mark_idempotency_completed, db, idempotency_claim_id,
                            session_id=session.id, message_id=asst_msg_id,
                        )
                    else:
                        await asyncio.to_thread(mark_idempotency_failed, db, idempotency_claim_id)
                payload = {
                    "type": "done", "session_id": session.id, "message_id": asst_msg_id,
                    "user_message_id": user_msg_id, "answer": reply, "iterations": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                              "tcu": 0.0, "duration_ms": 0},
                }
                # The client polls this to follow the deck and collect the file.
                if job_id:
                    payload["job_id"] = job_id
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                control_token.release()

        return StreamingResponse(
            _presentation_stream(), media_type="text/event-stream", headers=_sse_headers,
        )

    if task_type == TaskType.AGENT:
        # Etapa 5I — same admission point as /agent: Agent Scheduler +
        # TCU reservation/Continuity have already succeeded by here.
        agent_execution_id = await asyncio.to_thread(
            create_admitted_execution,
            db, user_id=current_user.id, session_id=session.id,
            reservation_id=reservation_id,
            continuity_execution_id=continuity.execution_id if continuity else None,
            idempotency_key_id=idempotency_claim_id,
        )
        return StreamingResponse(
            with_execution_timeout(
                stream_agent(
                    db, session, body.content,
                    user_plan=current_user.plan,
                    reservation_id=reservation_id,
                    user_timezone=current_user.timezone,
                    capability=capability,
                    request_control_token=control_token,
                    agent_scheduler_token=scheduler_token,
                    forced_provider_key=continuity.provider_key if continuity else None,
                    continuity_execution_id=continuity.execution_id if continuity else None,
                    continuity_estimated_tcu=continuity.estimated_tcu if continuity else 0.0,
                    idempotency_claim_id=idempotency_claim_id,
                    client_locale=body.locale,
                    agent_execution_id=agent_execution_id,
                ),
                timeout_s=settings.agent_scheduler_execution_timeout_seconds,
            ),
            media_type="text/event-stream",
            headers=_sse_headers,
        )

    return StreamingResponse(
        stream_chat(
            db, session, body.content, body.model,
            user_plan=current_user.plan,
            reservation_id=reservation_id,
            capability=capability,
            request_control_token=control_token,
            forced_provider_key=continuity.provider_key if continuity else None,
            continuity_execution_id=continuity.execution_id if continuity else None,
            continuity_estimated_tcu=continuity.estimated_tcu if continuity else 0.0,
            idempotency_claim_id=idempotency_claim_id,
            client_locale=body.locale,
        ),
        media_type="text/event-stream",
        headers=_sse_headers,
    )


@router.get("/{session_id}/usage")
def usage(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = get_session(db, session_id=session_id, user_id=current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return get_session_usage(db, session_id=session_id, user_id=current_user.id)
