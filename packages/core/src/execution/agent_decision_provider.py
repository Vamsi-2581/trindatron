"""
AgentDecisionExecutionProvider — job_type="agent_decide" step executor.

Persistent Agent Runtime — Tier 2 (LLM DECIDE). Implements the SAME
ExecutionProvider ABC every other provider does (see provider.py's
docstring and model_router_provider.py's own module docstring for why
this is the correct extension point rather than a branch in
execution_worker's step loop). A DECIDE ExecutionJob is a REAL, durable,
billed job — claimed, leased, retried, and recovered by the EXISTING
Durable Execution Worker exactly like a "research" job. Nothing in
execution_worker.py changes.

Billing (reuses model_router_provider.py's EXACT sequence, not a parallel
implementation)
--------------------------------------------------------------------------
check_and_reserve() -> Model Router call -> finalize_usage() on a real
response, or cancel_reservation() on any router-level failure
(timeout/unavailable/rejected/unexpected). Exactly one UsageRecord is
created and finalized per successful call, one created-then-cancelled per
router-level failure — the identical guarantee model_router_provider.py
already gives, because this is the SAME sequence, copied deliberately (a
shared base class would have saved a few lines at the cost of coupling
two independently-evolving call shapes; see that trade-off noted here
rather than hidden).

Decision validation happens AFTER billing, not instead of it
--------------------------------------------------------------------------
A router-level failure (timeout, provider unavailable, rejected, budget
exceeded, unexpected exception) means no real completion happened —
cancel_reservation(), StepOutcome(success=False, recoverable=...),
IDENTICAL to model_router_provider.py's own classification. But once a
REAL response comes back, the user was genuinely billed for it
(finalize_usage() happens immediately) — regardless of whether that
response happens to be valid JSON matching the DECIDE contract. Malformed/
invalid model output is then a SEPARATE, later check
(agent_decision.parse_decision()) that turns a successfully-billed call
into a step FAILURE (mission requirement: "Se o modelo retornar JSON
inválido: tratar como falha de DECIDE ... não executar ACT por default").
This is deliberate: the model was actually called and actually cost real
tokens either way — the failure is "the response wasn't useful", not "the
call didn't happen", and those two must never be billed the same way.

recoverable=True for invalid JSON — a retry may genuinely produce valid
JSON from the same non-deterministic model; execution_worker's EXISTING
job.max_retries bounds the attempt count, no new retry policy is created.

Capability / provider-selection rules
----------------------------------------
Reuses Capability.RESEARCH and TaskType.CHAT — the SAME single-turn,
no-tool-loop shape "research" already uses (see model_router_provider.py's
own docstring for why this correctly falls back to the TaskType/plan
chain). Deliberately NOT Capability.AGENT/TaskType.AGENT — that maps to
the ReAct tool-use pipeline (capability_router.py), a materially
different, heavier shape this single structured-JSON completion has no
need for and should not accidentally acquire tool access through.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.capability.classifier import Capability
from src.execution.provider import (
    EnvironmentInfo,
    ExecutionProvider,
    StepOutcome,
)
from src.models.execution import ExecutionJob
from src.services.agent_decision import DECIDE_SYSTEM_PROMPT_V1, parse_decision
from src.services.entitlement_service import (
    cancel_reservation,
    check_and_reserve,
    finalize_usage,
    get_reservation_budget_tcu,
)
from src.services.runtime_settings import get_disabled_provider_keys
from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    RequestRejectedByProviderError,
    RoutingRequest,
    TaskType,
)

logger = logging.getLogger("trinity.execution.agent_decision_provider")

# Module-level singleton — same rationale as model_router_provider.py's own
# `_router`: one shared ModelRouter (and its circuit-breaker registry)
# across every DECIDE job in this process, deliberately a SEPARATE
# instance from model_router_provider._router (each provider module owns
# its own router instance, matching the existing precedent — chat/agent's
# router.py singleton is yet another separate instance again).
_router = ModelRouter()


class AgentDecisionExecutionProvider(ExecutionProvider):
    def __init__(
        self,
        workspace_root: str,
        *,
        user_id: str,
        plan: str,
        user_timezone: str | None,
        session_id: str | None,
        session_factory: Callable[[], DBSession],
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._user_id = user_id
        self._plan = plan
        self._user_timezone = user_timezone
        self._session_id = session_id
        self._session_factory = session_factory
        self._job_id: str | None = None
        # Same "reference-only pointer, copied onto ExecutionJob.reservation_id
        # by execution_worker" contract as model_router_provider.py's own
        # last_reservation_id — see that module's docstring.
        self.last_reservation_id: str | None = None

    @property
    def provider_name(self) -> str:
        return "agent_decision"

    async def is_available(self) -> bool:
        return True

    async def prepare_environment(self, *, job_id: str, user_id: str) -> Path:
        self._job_id = job_id
        workspace = self._workspace_root / "executions" / user_id / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    @staticmethod
    def _point_job_at_reservation(db: DBSession, job_id: str, reservation_id: str) -> None:
        db.query(ExecutionJob).filter(ExecutionJob.id == job_id).update(
            {"reservation_id": reservation_id}, synchronize_session=False,
        )
        db.commit()

    async def execute_step(
        self, *, workspace: Path, command: str, timeout_seconds: int,
    ) -> StepOutcome:
        """`command` is the DECIDE user-message text built by
        agent_decision.build_decide_payload() — the per-cycle dynamic
        context (agent identity, CHECK result, observed excerpt). The
        SYSTEM message (DECIDE_SYSTEM_PROMPT_V1) is applied HERE, at call
        time, never persisted in job_payload — see agent_decision.py's
        module docstring for why that separation is the actual prompt-
        injection defense."""
        start = time.monotonic()
        db = self._session_factory()
        try:
            try:
                reservation_id = await asyncio.to_thread(
                    check_and_reserve,
                    db,
                    user_id=self._user_id,
                    session_id=self._session_id,
                    plan=self._plan,
                    user_timezone=self._user_timezone,
                )
            except HTTPException as exc:
                message = exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
                return StepOutcome(
                    success=False,
                    output=f"Entitlement refused: {message}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    recoverable=False,
                )

            # Written EAGERLY — see model_router_provider.py's docstring for
            # exactly why (lets reclaim_expired_leases() find and cancel a
            # dangling 'pending' reservation even if the crash happens
            # strictly inside the router call below).
            await asyncio.to_thread(self._point_job_at_reservation, db, self._job_id, reservation_id)

            disabled_provider_keys = await asyncio.to_thread(get_disabled_provider_keys, db)
            session_id_for_audit = self._session_id or f"execution-decide-{self._job_id}"
            # Held to its reservation — see model_router_provider.py.
            call_budget_usd = (
                await asyncio.to_thread(get_reservation_budget_tcu, db, reservation_id)
            ) / 100.0

            try:
                result = await asyncio.wait_for(
                    _router.complete(RoutingRequest(
                        messages=[
                            {"role": "system", "content": DECIDE_SYSTEM_PROMPT_V1},
                            {"role": "user", "content": command},
                        ],
                        task_type=TaskType.CHAT,
                        user_plan=self._plan,
                        session_id=session_id_for_audit,
                        capability=Capability.RESEARCH.value,
                        disabled_provider_keys=disabled_provider_keys,
                        max_cost_usd_override=call_budget_usd,
                    )),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False,
                    output=f"DECIDE call timed out after {timeout_seconds}s",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    recoverable=True,
                )
            except ProviderUnavailableError as exc:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=True,
                )
            except (RequestRejectedByProviderError, BudgetExceededError) as exc:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )
            except Exception as exc:  # noqa: BLE001 — never leave a reservation dangling
                logger.exception("execution.agent_decision_provider: unexpected router failure")
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=f"Unexpected router error: {exc}",
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )

            duration_ms = int((time.monotonic() - start) * 1000)
            # A REAL response came back — bill it now, regardless of what
            # validation below finds. See module docstring.
            await asyncio.to_thread(
                finalize_usage,
                db,
                reservation_id,
                message_id=None,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_tokens=result.cached_tokens,
                cost_usd=result.cost_usd,
                tcu=result.tcu,
                duration_ms=duration_ms,
            )
            self.last_reservation_id = reservation_id

            content = result.content or ""
            if parse_decision(content) is None:
                # Billed, but unusable — a STEP failure (mission
                # requirement: never an implicit ACT). recoverable=True:
                # a retry may get valid JSON from the same non-deterministic
                # model; job.max_retries (existing policy) bounds attempts.
                logger.warning(
                    "execution.agent_decision_provider: DECIDE output failed validation | job=%s", self._job_id,
                )
                return StepOutcome(
                    success=False,
                    output=content,
                    duration_ms=duration_ms,
                    recoverable=True,
                )

            return StepOutcome(success=True, output=content, duration_ms=duration_ms, recoverable=False)
        finally:
            db.close()

    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        if not workspace.exists():
            return EnvironmentInfo(available=True, workspace_path=str(workspace), files=[], message="workspace not created yet")
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return EnvironmentInfo(available=True, workspace_path=str(workspace), files=files)

    async def cleanup_environment(self, *, workspace: Path) -> None:
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
