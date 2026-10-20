"""PresentationExecutionProvider — job_type="presentation" step executor.

Implements the same ExecutionProvider ABC DockerExecutionProvider and
ModelRouterExecutionProvider do, for the same reason models/execution.py
already gives: job_type selects a provider, and the orchestrator never
branches on it. execution_worker's step loop is untouched by this file.

One step is one deck. There is no useful mid-deck resume — the engine
generates a presentation as a single synchronous call and has no notion of
"continue from slide 7" — so a retry regenerates, which is precisely why
retries here are tightly restricted (see _classify below).

What makes this different from the other providers
--------------------------------------------------
Every other job reserves the flat per-request figure because it cannot know
better. A deck can: slide count and image strategy determine the cost before
anything runs (src/presentation/cost.py). So the sequence is estimate, then
check the estimate against what the user can actually afford, then reserve
the ESTIMATE — not the flat 4/8/30 TCU, which would under-reserve a 17 TCU
job by a factor of four and recreate the overshoot ADMIT+CLAMP removed.

And uniquely, a refusal here is cheap while a failure is expensive: a deck
that cannot be afforded must be declined BEFORE the engine is woken, because
by the time the engine has run, the money is spent whether or not the result
can be delivered.

Accounting
----------
check_and_reserve -> generate -> finalize_usage, the same sequence the rest
of the codebase uses, with one addition: the engine's real token usage is
read back from presentation_llm_calls (written by the gateway the engine
calls — see api/routes/presenton_gateway.py) so finalize records measured
cost rather than the estimate. When no rows are found the estimate stands,
which errs high and never bills less than reality.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.execution.provider import (
    EnvironmentInfo,
    ExecutionProvider,
    ExecutionProviderUnavailable,
    StepOutcome,
)
from src.models.execution import ExecutionArtifact, ExecutionJob
from src.models.presentation_llm_call import PresentationLlmCall
from src.presentation.cost import USD_PER_TCU, estimate
from src.presentation.image_policy import decide as decide_images
from src.presentation.provider import (
    ExportFormat,
    ImageStrategy,
    PresentationError,
    PresentationQuotaError,
    PresentationRequest,
)
from src.services.entitlement_service import (
    cancel_reservation,
    check_and_reserve,
    finalize_usage,
    remaining_budget_tcu,
)
from src.services.presentation_service import build_provider
from src.storage import build_object_store
from src.storage.object_store import ObjectStore, ObjectStoreError

logger = logging.getLogger("trinity.execution.presentation")

# Absolute ceiling on deck size, independent of budget. Presenton's own
# practical limit and a guard against a payload asking for a thousand slides
# and tying the engine up for an hour.
MAX_SLIDES = 60
# The product floor, matching api/routes/presentations.MIN_SLIDES. Deliberately
# not 1: when a shrinking budget cannot fit three slides, the honest answer is
# "not enough capacity", not a one-slide file called a presentation. Both
# entry points must agree on this or the API would promise a minimum the
# worker does not keep.
MIN_SLIDES = 3


class PresentationExecutionProvider(ExecutionProvider):
    def __init__(
        self,
        workspace_root: str,
        *,
        user_id: str,
        plan: str,
        user_timezone: str | None,
        session_id: str | None,
        session_factory: Callable[[], DBSession],
        options: dict | None = None,
        engine=None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._user_id = user_id
        self._plan = plan
        self._user_timezone = user_timezone
        self._session_id = session_id
        self._session_factory = session_factory
        self._options = options or {}
        # Injected in tests; built from settings in production. Constructing
        # them here rather than at call time keeps execute_step readable and
        # lets a test substitute either half independently.
        self._engine = engine or build_provider()
        self._store = object_store or build_object_store()
        self._job_id: str | None = None
        # Read by execution_worker via getattr, same contract
        # ModelRouterExecutionProvider documents.
        self.last_reservation_id: str | None = None

    @property
    def provider_name(self) -> str:
        return "presentation"

    async def is_available(self) -> bool:
        """Engine reachable AND somewhere to put the result.

        Both halves are required: a deck that generates successfully and then
        has nowhere to be stored has spent real money for nothing, so storage
        is checked before the job is allowed to start rather than discovered
        at upload time.
        """
        engine_ok = (await self._engine.availability()).available
        if not engine_ok:
            return False
        return await self._store.is_available()

    async def prepare_environment(self, *, job_id: str, user_id: str) -> Path:
        self._job_id = job_id
        workspace = self._workspace_root / "executions" / user_id / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _point_job_at_reservation(db: DBSession, job_id: str, reservation_id: str) -> None:
        """Written eagerly, before the long call — same reasoning
        ModelRouterExecutionProvider gives: a crash mid-generation must leave
        a reservation the lease-reclaim sweep can find and cancel."""
        db.query(ExecutionJob).filter(ExecutionJob.id == job_id).update(
            {"reservation_id": reservation_id}, synchronize_session=False,
        )
        db.commit()

    @staticmethod
    def _measured_cost_usd(db: DBSession, since: datetime, until: datetime) -> tuple[float, int]:
        """Real spend recorded by the gateway during this generation.

        Temporal attribution — see models/presentation_llm_call.py for why
        that is exact here and what would break it.
        """
        row = (
            db.query(
                func.coalesce(func.sum(PresentationLlmCall.cost_usd), 0.0),
                func.count(PresentationLlmCall.id),
            )
            .filter(
                PresentationLlmCall.created_at >= since,
                PresentationLlmCall.created_at <= until,
            )
            .first()
        )
        if row is None:
            return 0.0, 0
        return float(row[0] or 0.0), int(row[1] or 0)

    def _requested_slides(self) -> int:
        raw = self._options.get("n_slides", 10)
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 10
        return max(MIN_SLIDES, min(MAX_SLIDES, n))

    def _export_format(self) -> ExportFormat:
        raw = str(self._options.get("export_as", "pptx")).lower()
        try:
            return ExportFormat(raw)
        except ValueError:
            return ExportFormat.PPTX

    def _requested_strategy(self) -> ImageStrategy | None:
        raw = self._options.get("image_strategy")
        if not raw:
            return None
        try:
            return ImageStrategy(str(raw).lower())
        except ValueError:
            return None

    async def _store_artifacts(self, db: DBSession, result, *, job_id: str) -> list[dict]:
        """Upload each produced file and record its metadata.

        The bytes never touch the workspace directory. The worker cleans that
        up after every job, and a deck that exists only there would be deleted
        minutes after being generated — which is the entire argument for
        object storage. The row here is the durable half; the object is the
        other.
        """
        stored: list[dict] = []
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.artifact_retention_days)
        for artifact in result.artifacts:
            # User id in the key is what makes ownership checkable from the
            # key alone, and job id keeps two decks from colliding on a name
            # the engine always reports as "presentation.pptx".
            key = f"presentations/{self._user_id}/{job_id}/{artifact.filename}"
            obj = await self._store.put(
                key=key, data=artifact.data, content_type=artifact.media_type
            )
            db.add(ExecutionArtifact(
                job_id=job_id,
                filename=artifact.filename,
                relative_path=artifact.filename,
                content_type=artifact.media_type,
                size_bytes=obj.size_bytes,
                storage_backend=obj.backend,
                storage_key=obj.key,
                expires_at=expires_at,
            ))
            stored.append({
                "filename": artifact.filename,
                "media_type": artifact.media_type,
                "size_bytes": obj.size_bytes,
            })
        db.commit()
        return stored

    # ── contract ─────────────────────────────────────────────────────────

    async def execute_step(
        self, *, workspace: Path, command: str, timeout_seconds: int,
    ) -> StepOutcome:
        """`command` is the deck brief. One call = one deck."""
        start = time.monotonic()
        brief = (command or "").strip()
        if not brief:
            return StepOutcome(
                success=False, output="No presentation brief was provided.",
                duration_ms=0, recoverable=False,
            )

        # ── 1. Is there anything to run? ─────────────────────────────────
        # Wake first: the engine is scaled to zero, so "unavailable" on the
        # first probe is the normal cold-start state, not a fault.
        ready = await self._engine.wait_until_ready()
        if not ready.available:
            # Raised, not returned: ExecutionProviderUnavailable is what
            # _classify_recoverable() reads as "retry this later", which is
            # right for an engine that is down rather than a deck that is
            # impossible.
            raise ExecutionProviderUnavailable(
                f"presentation engine unavailable: {ready.reason}"
            )

        db = self._session_factory()
        try:
            # ── 2. What would this deck cost? ────────────────────────────
            decision = decide_images(
                plan=self._plan, content=brief, requested=self._requested_strategy()
            )
            requested_slides = self._requested_slides()
            web_search = bool(self._options.get("web_search", False))

            est = estimate(
                n_slides=requested_slides,
                image_strategy=decision.strategy,
                web_search=web_search,
            )

            # ── 3. Can it be afforded? Decided BEFORE waking any spend. ──
            remaining = await asyncio.to_thread(
                remaining_budget_tcu, db, self._user_id, self._plan, self._user_timezone
            )
            slides = requested_slides
            if remaining is not None and est.tcu > remaining:
                # Shrink to fit rather than refuse outright — a 6-slide deck
                # the user can afford is a better answer than nothing, and
                # the alternative is spending their whole window on a deck
                # that gets cut off halfway.
                from src.presentation.cost import max_slides_within

                slides = max_slides_within(
                    remaining, image_strategy=decision.strategy, web_search=web_search
                )
                if slides < MIN_SLIDES:
                    logger.info(
                        "presentation: refused on budget | user=%s plan=%s "
                        "requested_slides=%d estimated_tcu=%.2f remaining_tcu=%.2f",
                        self._user_id, self._plan, requested_slides, est.tcu, remaining,
                    )
                    return StepOutcome(
                        success=False,
                        output=(
                            "There is not enough capacity left in your current usage "
                            "window to build a presentation. It will be available again "
                            "when the window resets."
                        ),
                        duration_ms=int((time.monotonic() - start) * 1000),
                        recoverable=False,
                    )
                est = estimate(
                    n_slides=slides, image_strategy=decision.strategy, web_search=web_search
                )

            # ── 4. Reserve the estimate, not the flat figure ─────────────
            try:
                reservation_id = await asyncio.to_thread(
                    check_and_reserve,
                    db,
                    user_id=self._user_id,
                    session_id=self._session_id,
                    plan=self._plan,
                    user_timezone=self._user_timezone,
                    estimated_tcu=est.tcu,
                )
            except HTTPException as exc:
                message = exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
                return StepOutcome(
                    success=False, output=f"Entitlement refused: {message}",
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )

            await asyncio.to_thread(
                self._point_job_at_reservation, db, self._job_id, reservation_id
            )

            # ── 5. Generate ──────────────────────────────────────────────
            window_start = datetime.now(timezone.utc)
            request = PresentationRequest(
                content=brief,
                n_slides=slides,
                language=str(self._options.get("language", "English")),
                export_as=self._export_format(),
                image_strategy=decision.strategy,
                web_search=web_search,
                instructions=self._options.get("instructions") or None,
            )
            try:
                result = await asyncio.wait_for(
                    self._engine.generate(request), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False,
                    output=f"Presentation generation timed out after {timeout_seconds}s",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    recoverable=True,
                )
            except PresentationQuotaError as exc:
                # Measured: the model provider's free tier caps at 20 requests
                # per DAY and one deck needs more than that. A retry spends
                # what little remains and lengthens the outage, so this is
                # permanent for this attempt by design, not by oversight.
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                logger.warning("presentation: quota refusal | user=%s", self._user_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )
            except PresentationError as exc:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )
            except Exception as exc:  # noqa: BLE001 — never leave a reservation dangling
                logger.exception("presentation: unexpected engine failure")
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
                return StepOutcome(
                    success=False, output=f"Unexpected presentation error: {exc}",
                    duration_ms=int((time.monotonic() - start) * 1000), recoverable=False,
                )

            window_end = datetime.now(timezone.utc)

            # ── 6. Store the result ──────────────────────────────────────
            try:
                stored = await self._store_artifacts(db, result, job_id=self._job_id)
            except ObjectStoreError as exc:
                # The deck exists and the money is already spent, so the
                # reservation is FINALIZED rather than cancelled — cancelling
                # would hand back budget that was genuinely consumed. The step
                # still fails, because an artifact nobody can download is not
                # a delivered presentation.
                measured_usd, _ = await asyncio.to_thread(
                    self._measured_cost_usd, db, window_start, window_end
                )
                spent_usd = measured_usd or est.total_usd
                await asyncio.to_thread(
                    finalize_usage, db, reservation_id,
                    message_id=None, model=settings.presentation_model,
                    input_tokens=0, output_tokens=0, cached_tokens=0,
                    cost_usd=spent_usd, tcu=spent_usd / USD_PER_TCU,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                logger.error("presentation: generated but not stored | %s", exc)
                return StepOutcome(
                    success=False,
                    output="The presentation was created but could not be saved.",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    recoverable=False,
                )

            # ── 7. Finalize against measured cost ────────────────────────
            measured_usd, call_count = await asyncio.to_thread(
                self._measured_cost_usd, db, window_start, window_end
            )
            if call_count == 0:
                # No gateway rows: either the engine is pointed straight at a
                # provider, or recording failed. Fall back to the estimate,
                # which is deliberately conservative and so never under-bills.
                logger.info(
                    "presentation: no gateway usage rows; finalizing on estimate | job=%s",
                    self._job_id,
                )
            final_usd = measured_usd if call_count else est.total_usd
            final_tcu = final_usd / USD_PER_TCU
            duration_ms = int((time.monotonic() - start) * 1000)

            await asyncio.to_thread(
                finalize_usage, db, reservation_id,
                message_id=None, model=settings.presentation_model,
                input_tokens=0, output_tokens=0, cached_tokens=0,
                cost_usd=final_usd, tcu=final_tcu, duration_ms=duration_ms,
            )
            self.last_reservation_id = reservation_id

            logger.info(
                "presentation: delivered | user=%s job=%s slides=%d images=%s "
                "calls=%d estimated_tcu=%.2f actual_tcu=%.2f duration_s=%.1f",
                self._user_id, self._job_id, slides, decision.strategy.value,
                call_count, est.tcu, final_tcu, duration_ms / 1000,
            )

            summary = {
                "slides": slides,
                "requested_slides": requested_slides,
                "image_strategy": decision.strategy.value,
                "image_reason": decision.reason,
                "downgraded_for_plan": decision.downgraded_for_plan,
                "artifacts": stored,
            }
            return StepOutcome(
                success=True, output=str(summary), duration_ms=duration_ms, recoverable=False
            )
        finally:
            db.close()

    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        if not workspace.exists():
            return EnvironmentInfo(
                available=True, workspace_path=str(workspace), files=[],
                message="workspace not created yet",
            )
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return EnvironmentInfo(available=True, workspace_path=str(workspace), files=files)

    async def cleanup_environment(self, *, workspace: Path) -> None:
        """Safe to call unconditionally — the deck itself was never written
        here (see _store_artifacts), so this removes an empty directory."""
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
