"""
ModelRouterExecutionProvider — job_type="research" step executor.

Implements the SAME ExecutionProvider ABC DockerExecutionProvider does (see
provider.py's docstring). This is deliberate: models/execution.py's own
docstring already says job_type is "reserved for future kinds (e.g.
'research', 'browser') without a schema change — the provider decides what
job_type values it understands; the orchestrator never branches on it
itself." Adding a new ExecutionProvider implementation — instead of an
`if job.job_type == "research"` branch inside execution_worker._execute_one_attempt()'s
step loop — is exactly what that sentence calls for: that loop stays 100%
unchanged, still just calls `execute_step(workspace, command, timeout)` and
interprets the result generically for checkpoint/retry/Sentinel/cleanup.

`command` here is the research prompt text, not a shell command — the ABC's
parameter name is provider-agnostic by design (see provider.py), so no
signature change was needed to repurpose it.

Accounting (mission requirements 3-6: reuse existing entitlement system, no
duplicate accounting, no improper charging on failure, idempotent)
--------------------------------------------------------------------
Each execute_step() call is exactly one Model Router completion, wrapped in
the SAME check_and_reserve() / finalize_usage() / cancel_reservation()
sequence sessions.py's /chat and's /agent routes already use (imported
unmodified — no new ledger, no new TCU math). Sequencing:
  1. check_and_reserve() — before the call. HTTPException (quota exceeded)
     is caught and turned into a permanent (non-recoverable) StepOutcome
     failure, never a silent skip.
  2. Model Router call. ANY exception (classified or not) triggers
     cancel_reservation() before returning a failed StepOutcome — a
     reservation is never left dangling in "pending" on a failure path,
     which is exactly what "provider failure must never cause improper
     charging" requires (a lingering pending reservation would otherwise
     count against the user's quota via _sum_active_tcu() forever).
  3. finalize_usage() — only after a real RoutingResult (real token counts)
     comes back.
Exactly one UsageRecord is created and finalized per successful call, and
exactly one is created-then-cancelled per failed call — idempotent in the
sense that no call path can produce zero or two ledger entries for the same
attempt.

Capability / provider-selection rules (mission requirement 2)
---------------------------------------------------------------
Uses TaskType.CHAT (single-turn completion, no tool loop — matches
Capability.RESEARCH's existing route to ExecutorKind.TEXT in
capability_router.py) and capability=Capability.RESEARCH.value, so
selector.select_provider_chain() applies the exact same chain-selection
rules chat/agent requests get (RESEARCH has no dedicated entry in
_CAPABILITY_CHAINS, so it correctly falls back to the TaskType/plan chain —
this is the existing, intentional "unknown capability" behavior, not a gap).
disabled_provider_keys is read fresh per call via the same
runtime_settings.get_disabled_provider_keys() Etapa 5H kill switch chat/agent
already respect.
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

logger = logging.getLogger("trinity.execution.model_router_provider")

# Module-level singleton — same rationale router.py's own docstring gives
# for chat_service.py's/agent_service.py's `_router = ModelRouter()`: the
# circuit breaker registry is designed to persist across calls made through
# one shared instance. Every ModelRouterExecutionProvider instance (one per
# research job — see execution_service.build_research_provider()) shares
# this ONE router, so a provider outage observed by one research job's
# retry attempt is remembered for the next research job too — consistent
# with, not a duplicate of, chat/agent's own per-module singleton.
_router = ModelRouter()


class ModelRouterExecutionProvider(ExecutionProvider):
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
        # Reference-only pointer for execution_worker._execute_one_attempt()
        # to copy onto ExecutionJob.reservation_id after each step — see
        # that column's own docstring in models/execution.py ("no double
        # accounting"). Optional/generic: that loop reads this via
        # getattr(..., "last_reservation_id", None), so a provider that
        # never invokes the Model Router (DockerExecutionProvider) simply
        # doesn't have it and nothing changes for job_type="shell". Also
        # written EAGERLY, mid-call — see _point_job_at_reservation().
        self.last_reservation_id: str | None = None

    @property
    def provider_name(self) -> str:
        return "model_router"

    async def is_available(self) -> bool:
        # Unlike Docker (a single local daemon that either is or isn't
        # reachable), Model Router availability is inherently per-call —
        # its own chain/failover/circuit-breaker machinery (router.py)
        # already decides per-provider reachability at complete()-time.
        # There is no cheap, meaningful pre-flight check to do here.
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
        """`command` is the research prompt. One call = one Model Router
        completion = one entitlement reservation (see module docstring)."""
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

            # Written EAGERLY — immediately after the reservation exists,
            # BEFORE the (possibly slow, possibly never-returning-if-the-
            # worker-crashes) Model Router call below — not lazily after
            # execute_step() returns. This is what lets
            # execution_worker.reclaim_expired_leases() find and cancel a
            # dangling 'pending' reservation even when the crash happens
            # strictly inside this call (the exact window mission
            # requirement 5 is about): without this, job.reservation_id
            # would still be None at the moment of the crash, and the
            # orphaned reservation would never be found.
            await asyncio.to_thread(self._point_job_at_reservation, db, self._job_id, reservation_id)

            disabled_provider_keys = await asyncio.to_thread(get_disabled_provider_keys, db)
            session_id_for_audit = self._session_id or f"execution-job-{self._job_id}"
            # Held to its reservation like every chat and agent call: without
            # this a step could spend the router's whole plan cap against a
            # reservation that was clamped to a much smaller balance.
            call_budget_usd = (
                await asyncio.to_thread(get_reservation_budget_tcu, db, reservation_id)
            ) / 100.0

            try:
                result = await asyncio.wait_for(
                    _router.complete(RoutingRequest(
                        messages=[{"role": "user", "content": command}],
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
                    output=f"Research call timed out after {timeout_seconds}s",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    recoverable=True,
                )
            except ProviderUnavailableError as exc:
                # All candidates in the chain failed right now — the same
                # transient condition Docker-unreachable maps to. Worth a
                # job-level retry.
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=True,
                )
            except (RequestRejectedByProviderError, BudgetExceededError) as exc:
                # Request content rejected, or Model Router's own plan
                # ceiling — retrying the identical prompt cannot change
                # either outcome. Permanent, matching an ordinary non-zero
                # shell exit's treatment in docker_provider.py.
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )
            except Exception as exc:  # noqa: BLE001 — never leave a reservation dangling
                logger.exception("execution.model_router_provider: unexpected router failure")
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=f"Unexpected router error: {exc}",
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )

            duration_ms = int((time.monotonic() - start) * 1000)
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
            try:
                (workspace / "research_result.md").write_text(content, encoding="utf-8")
            except OSError:
                logger.warning("execution.model_router_provider: failed writing research artifact", exc_info=True)

            return StepOutcome(success=True, output=content, duration_ms=duration_ms, recoverable=False)
        finally:
            db.close()

    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        if not workspace.exists():
            return EnvironmentInfo(available=True, workspace_path=str(workspace), files=[], message="workspace not created yet")
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return EnvironmentInfo(available=True, workspace_path=str(workspace), files=files)

    async def cleanup_environment(self, *, workspace: Path) -> None:
        # Same "idempotent, safe even if never prepared" contract as
        # DockerExecutionProvider.cleanup_environment() — see that
        # docstring. execution_worker._execute_one_attempt() already
        # persists artifacts (_persist_artifacts()) BEFORE calling this,
        # so deleting the workspace here does not lose anything not
        # already captured as an ExecutionArtifact row.
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
