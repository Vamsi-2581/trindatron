from __future__ import annotations

import asyncio
import json
import time
import logging
from typing import AsyncGenerator

from sqlalchemy.orm import Session as DBSession
from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    QualityNotEntitledError,
    RoutingRequest,
    RouterStreamChunk,
    TaskType,
)

from src.capability import Capability, classify, capability_status_event, get_task_type
from src.config import settings
from src.models.message import Message
from src.models.session import Session
from src.services.continuity_entitlement_service import true_up_continuity
from src.services.entitlement_service import cancel_reservation, finalize_usage, get_reservation_budget_tcu
from src.services.idempotency_service import mark_completed as mark_idempotency_completed
from src.services.idempotency_service import mark_failed as mark_idempotency_failed
from src.services.request_control import RequestControlToken
from src.services.runtime_settings import get_disabled_provider_keys
from src.prompting import ToolPolicy, assemble
from src.prompting.context_builders import build_relevant_memory, build_task_context
from src.prompting.history import conversation_context
from src.prompting.identity_guard import (
    StreamRedactor,
    safe_error_payload,
)
from src.services import preferences_service
from src.services.product_answers import product_answer

logger = logging.getLogger(__name__)

_router = ModelRouter()

def _prepare_chat_context(
    db: DBSession,
    session: Session,
    user_content: str,
    task_type: str = "",
    capability: str = "",
    client_locale: str = "",
) -> tuple[Message, list[dict]]:
    """Synchronous DB work for stream_chat()'s pre-LLM setup: persist the
    user message, load history, and build the system prompt — including any
    lazy-loaded session.project/session.agent_definition/skill relationships,
    which are real (implicit) queries, not just attribute access.

    Called via asyncio.to_thread() so this blocking work runs off the event
    loop instead of stalling every other in-flight request for its duration
    (see Etapa 5A). Same `db` Session as the caller — never touched from more
    than one thread at a time, since the caller awaits this call to finish
    before doing anything else with `db`.
    """
    user_msg = Message(session_id=session.id, role="user", content=user_content)
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    history = (
        db.query(Message)
        .filter(Message.session_id == session.id)
        .order_by(Message.created_at)
        .all()
    )

    # Single source of truth for behaviour — see src/prompting/. Chat has no
    # tools, and that is stated explicitly rather than left implicit: a model
    # told it has none answers directly, while one told nothing sometimes
    # narrates calls it cannot make.
    assembled = assemble(
        task_context=build_task_context(
            db, session, task_type=task_type, capability=capability, client_locale=client_locale,
        ),
        tool_policy=ToolPolicy.none(),
        memory=build_relevant_memory(db, session.user_id, user_content),
    )
    messages = [assembled.as_message()]
    # Availability comes from the <capabilities> block above, never from what an earlier reply said about it.
    messages += conversation_context(history)
    return user_msg, messages


def _persist_chat_result(
    db: DBSession,
    session: Session,
    user_content: str,
    full_content: str,
    final_chunk: RouterStreamChunk,
    reservation_id: str | None,
    duration_ms: int,
    truncated: bool = False,
) -> Message:
    """Synchronous DB work for stream_chat()'s post-LLM persistence: save the
    assistant message, maybe set the session title, and finalize/record
    usage. Same to_thread rationale as _prepare_chat_context above — and the
    exact same reserve-before/finalize-after/cancel-on-failure sequencing as
    before this change, just relocated to run off the event loop.
    """
    assistant_msg = Message(
        session_id=session.id,
        role="assistant",
        content=full_content,
        model_used=final_chunk.model,
        truncated=truncated or None,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    if session.title == "New conversation" and user_content:
        session.title = (user_content[:60] + "…") if len(user_content) > 60 else user_content
        db.commit()

    if reservation_id:
        finalize_usage(
            db,
            reservation_id,
            message_id=assistant_msg.id,
            model=final_chunk.model,
            input_tokens=final_chunk.input_tokens,
            output_tokens=final_chunk.output_tokens,
            cached_tokens=final_chunk.cached_tokens,
            cost_usd=final_chunk.cost_usd,
            tcu=final_chunk.tcu,
            duration_ms=duration_ms,
        )
    else:
        # Backward-compat path: called without a reservation (e.g. direct service tests).
        from src.services.usage_service import record_usage
        record_usage(
            db=db,
            user_id=session.user_id,
            session_id=session.id,
            message_id=assistant_msg.id,
            model=final_chunk.model,
            input_tokens=final_chunk.input_tokens,
            output_tokens=final_chunk.output_tokens,
            cached_tokens=final_chunk.cached_tokens,
            cost_usd=final_chunk.cost_usd,
            tcu=final_chunk.tcu,
            duration_ms=duration_ms,
        )
    return assistant_msg


def _hit_output_cap(plan: str, output_tokens: int) -> bool:
    """Whether a reply ended because it reached the plan's output limit, not because it was finished.

    Measured on the dev database: every reply cut off mid-sentence or mid-table stopped at exactly the Free plan's
    1024 output tokens, and nothing told the person. A reasoning model's hidden reasoning counts toward the same
    limit, so the visible answer can end far sooner than 1024 tokens of text. Chat sends no max_tokens of its own, so
    the plan's cap is the one the provider applied; a cap the router lowers further to fit a per-request budget is
    not seen here (the cheap models a capped plan uses stay far below that budget).
    """
    from trinity_router.limits import effective_max_output_tokens, get_plan_limits

    try:
        cap = effective_max_output_tokens(get_plan_limits(plan), None)
    except Exception:
        return False
    return output_tokens >= cap


def _persist_guard_reply(db: DBSession, session: Session, user_content: str, reply: str) -> Message:
    """TRINDATRON's own answer to an architecture probe: persisted like any
    reply, with no model and no usage (see src/prompting/identity_guard.py)."""
    assistant_msg = Message(session_id=session.id, role="assistant", content=reply)
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)
    if session.title == "New conversation" and user_content:
        session.title = (user_content[:60] + "…") if len(user_content) > 60 else user_content
        db.commit()
    return assistant_msg


async def stream_chat(
    db: DBSession,
    session: Session,
    user_content: str,
    model: str | None = None,
    user_plan: str | None = None,
    reservation_id: str | None = None,
    capability: Capability | None = None,
    request_control_token: RequestControlToken | None = None,
    forced_provider_key: str | None = None,
    continuity_execution_id: str | None = None,
    continuity_estimated_tcu: float = 0.0,
    idempotency_claim_id: str | None = None,
    client_locale: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    forced_provider_key/continuity_execution_id/continuity_estimated_tcu
    (Etapa 5F, execution wiring): set together, ONLY by sessions.py's
    Continuity fallback path, never for a NORMAL request (all three stay
    at their defaults there, and this function's normal-path behavior is
    otherwise byte-for-byte unchanged — see this module's docstring... the
    reservation_id=None case this already handled before Etapa 5F (the
    `else: record_usage(...)` branch in _persist_chat_result) is exactly
    what makes a Continuity execution's message persistence/usage logging
    work correctly with NO further change: Continuity never has a
    reservation_id (check_and_reserve() is never called for it), so it was
    already routed through that pre-existing fallback.

    forced_provider_key is threaded straight into RoutingRequest — see
    trinity_router.RoutingRequest.forced_provider_key's own docstring for
    why this pins execution to exactly one economic model with no
    failover to a different (possibly premium) candidate.

    continuity_execution_id/continuity_estimated_tcu drive the financial
    true-up in this function's own `finally` (see continuity_actual_tcu
    below): the estimate was already debited BEFORE this function was
    even called (continuity_entitlement_service.check_and_debit_continuity,
    called from sessions.py); this function's job is only to reconcile
    that estimate against the real, final_chunk.tcu cost once known — full
    refund on any non-success exit (error, cancellation, disconnect,
    timeout), partial refund (or, rarely, an additional charge) on
    success. See true_up_continuity()'s own docstring — no new refund
    semantics are invented here, this just calls it at the right moment.

    idempotency_claim_id (Etapa 5I): set ONLY by sessions.py when the
    client sent an Idempotency-Key header and claim_idempotency_key()
    returned a fresh claim (never a replay — a replay short-circuits in
    sessions.py before stream_chat() is ever called). Marked "completed"
    in this function's own `finally` at the exact same point the TCU
    reservation is finalized; marked "failed" on every other exit path.
    """
    # model parameter kept for backward compatibility; router selects model server-side.
    # capability is forwarded from sessions.py when available — avoids double-classification.
    resolved_plan = (user_plan or settings.default_user_plan).lower()
    # Connection-retention fix: captured once, before the read-only-txn
    # commit below, which (SQLAlchemy's default expire_on_commit=True)
    # marks every loaded attribute of `session` for reload on next access.
    # Every reference between that commit and the next legitimate
    # persistence step uses these plain values instead of re-touching
    # `session`, which would otherwise silently reissue a SELECT (and
    # re-acquire a pool connection) at exactly the moment the code is
    # about to call the provider.
    _session_id = session.id
    _session_user_id = session.user_id

    # Track whether the reservation was finalized so the finally block knows
    # whether to cancel it (handles GeneratorExit from client disconnect too).
    _cancel_on_exit = reservation_id is not None
    # Continuity true-up: defaults to a FULL refund of the estimate (0.0
    # actual usage) — updated to the real final_chunk.tcu only once success
    # is confirmed, below. This means any exit path that does NOT reach
    # that point (error, GeneratorExit, CancelledError, an exception in
    # persistence itself) refunds the whole estimate, mirroring exactly
    # how cancel_reservation() zeroes out TCU on the premium path — no new
    # policy, the same "no partial credit for a failed attempt" rule.
    continuity_actual_tcu = 0.0

    try:
        if capability is None:
            capability = classify(user_content)
        task_type = get_task_type(capability)
        yield capability_status_event(capability)

        user_msg, messages = await asyncio.to_thread(
            _prepare_chat_context, db, session, user_content,
            task_type.value, capability.value, client_locale or "",
        )

        # Questions about what TRINDATRON runs on, or about what it can do right
        # now, are answered by TRINDATRON itself before any model sees them —
        # the second from the capability matrix (services/product_answers.py).
        # Nothing is spent, so the reservation is released rather than finalized.
        # The same language the model would be told to answer in — chosen in Settings, else the device, else
        # the text (src/language.py). Without it these replies were the last place that only read the text.
        answer_language = await asyncio.to_thread(
            preferences_service.answer_language, db, _session_user_id, client_locale,
        )
        reply = await product_answer(db, _session_user_id, user_content, answer_language or None)
        if reply is not None:
            assistant_msg = await asyncio.to_thread(_persist_guard_reply, db, session, user_content, reply)
            _cancel_on_exit = False
            if reservation_id:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
            await asyncio.to_thread(
                mark_idempotency_completed, db, idempotency_claim_id,
                session_id=_session_id, message_id=assistant_msg.id,
            )
            yield f"data: {json.dumps({'type': 'text', 'content': reply})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'session_id': _session_id, 'message_id': assistant_msg.id, 'user_message_id': user_msg.id, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'duration_ms': 0}})}\n\n"
            yield "data: [DONE]\n\n"
            return
        # Etapa 5H — read once per call, same snapshot-at-start-of-request
        # convention as resolved_plan above; a mid-stream setting change
        # takes effect on the NEXT request, never interrupts this one.
        disabled_provider_keys = await asyncio.to_thread(get_disabled_provider_keys, db)
        # Connection-retention fix: _prepare_chat_context's own reads (history,
        # lazy-loaded project/agent/skills, memory context) and the
        # get_disabled_provider_keys() read above all ran AFTER
        # _prepare_chat_context's own internal commit, so this Session has an
        # open, uncommitted read-only transaction — and therefore a checked-out
        # pool connection — that would otherwise sit idle for the entire
        # provider call below. Nothing was written since that commit, so this
        # is a plain, safe close of a read-only transaction (equivalent to a
        # rollback here) — it returns the connection to the pool BEFORE the
        # provider is ever awaited. Every DB call in this codebase already
        # runs via to_thread(); db.commit() is a synchronous, potentially
        # blocking network round-trip and must follow the same convention.
        await asyncio.to_thread(db.commit)

        start_time = time.time()
        final_chunk = None
        full_content = ""
        # A model naming its vendor as itself is rewritten before the user or
        # the history sees it, even when the phrase spans several chunks.
        redactor = StreamRedactor()

        # See the note on max_cost_usd_override below.
        _call_budget_usd: float | None = None
        if reservation_id:
            _call_budget_usd = (
                await asyncio.to_thread(get_reservation_budget_tcu, db, reservation_id)
            ) / 100.0
            # Connection-retention fix, second gap: get_reservation_budget_tcu()
            # is a bare read (no trailing commit/rollback) — SQLAlchemy's
            # autobegin re-opens a transaction, and therefore re-checks-out a
            # pool connection, the instant it ran, undoing the commit above.
            # Same "safe close of a read-only transaction" as that one: nothing
            # was written since, so this just releases the connection again
            # before the provider call this value exists to bound.
            await asyncio.to_thread(db.commit)

        try:
            async for chunk in _router.stream(RoutingRequest(
                messages=messages,
                task_type=task_type,
                user_plan=resolved_plan,
                session_id=_session_id,
                capability=capability.value,
                forced_provider_key=forced_provider_key,
                disabled_provider_keys=disabled_provider_keys,
                # This call's ceiling, read from the reservation that
                # authorised it. The router may narrow its plan cap with this
                # but never widen it, so a call can never be authorised for
                # more than the window it was reserved against — which is what
                # makes check_and_reserve's ADMIT rule safe: admitting on
                # remaining balance only works if the call is then held to it.
                max_cost_usd_override=_call_budget_usd,
            )):
                if not chunk.is_final:
                    safe_text = redactor.feed(chunk.text)
                    if safe_text:
                        full_content += safe_text
                        yield f"data: {json.dumps({'type': 'text', 'content': safe_text})}\n\n"
                else:
                    final_chunk = chunk

        except QualityNotEntitledError as exc:
            # Entitlement/Quality Gate — mirrors agent_service.py's
            # identical branch (same contract, same rationale): the task
            # genuinely requires a quality tier this plan is not entitled
            # to, rejected before any candidate/provider was touched.
            yield f"data: {json.dumps({'type': 'error', 'error': 'quality_not_entitled', 'message': 'This request requires a capability level your current plan does not include. Upgrade your plan to access it.', 'required_quality_class': exc.required_quality_class, 'plan_max_quality_class': exc.plan_max_quality_class, 'plan': exc.plan})}\n\n"
            return
        except BudgetExceededError:
            yield f"data: {json.dumps(safe_error_payload('request_too_large'))}\n\n"
            return
        except ProviderUnavailableError:
            yield f"data: {json.dumps(safe_error_payload('temporarily_unavailable'))}\n\n"
            return
        except Exception:
            logger.exception("Unexpected error during streaming | session=%s", _session_id)
            yield f"data: {json.dumps(safe_error_payload('internal'))}\n\n"
            return

        tail = redactor.flush()
        if tail:
            full_content += tail
            yield f"data: {json.dumps({'type': 'text', 'content': tail})}\n\n"

        if final_chunk is None:
            return

        duration_ms = int((time.time() - start_time) * 1000)

        truncated = _hit_output_cap(resolved_plan, final_chunk.output_tokens)
        assistant_msg = await asyncio.to_thread(
            _persist_chat_result,
            db, session, user_content, full_content, final_chunk, reservation_id, duration_ms, truncated,
        )

        # Reservation finalized — do NOT cancel in finally.
        _cancel_on_exit = False
        # Continuity true-up: reconcile against the REAL cost now known —
        # never assume estimated == actual (Etapa 5F correction). No-op
        # (continuity_actual_tcu stays 0.0) when this wasn't a Continuity
        # execution at all (continuity_execution_id is None in `finally`).
        continuity_actual_tcu = final_chunk.tcu
        # Etapa 5I — same "only at the confirmed-success point" timing as
        # the reservation finalize above. No-op when idempotency_claim_id
        # is None (no Idempotency-Key header was sent).
        await asyncio.to_thread(
            mark_idempotency_completed, db, idempotency_claim_id,
            session_id=session.id, message_id=assistant_msg.id,
        )

        logger.info(
            "chat completed | model=%s plan=%s tokens_in=%d tokens_out=%d "
            "cost_usd=%.6f tcu=%.4f duration_ms=%d",
            final_chunk.model, resolved_plan,
            final_chunk.input_tokens, final_chunk.output_tokens,
            final_chunk.cost_usd, final_chunk.tcu, duration_ms,
        )

        yield f"data: {json.dumps({'type': 'done', 'session_id': session.id, 'message_id': assistant_msg.id, 'user_message_id': user_msg.id, 'truncated': truncated, 'usage': {'input_tokens': final_chunk.input_tokens, 'output_tokens': final_chunk.output_tokens, 'cached_tokens': final_chunk.cached_tokens, 'duration_ms': duration_ms}})}\n\n"
        yield "data: [DONE]\n\n"

    finally:
        # Unconditional on every exit path — success, error, and
        # GeneratorExit (SSE client disconnect) all hit this finally. The
        # concurrency slot's lifetime is the whole stream, not the route
        # handler call that returned this generator — see
        # request_control.py's RequestControlToken docstring.
        if request_control_token is not None:
            request_control_token.release()
        if _cancel_on_exit and reservation_id:
            await asyncio.to_thread(cancel_reservation, db, reservation_id)
        if _cancel_on_exit:
            # Etapa 5I — every exit path that did NOT reach the confirmed-
            # success point above (error, GeneratorExit, CancelledError)
            # marks the claim "failed", exactly mirroring cancel_reservation's
            # own "_cancel_on_exit" condition. A retry with the same
            # Idempotency-Key is then correctly treated as a brand-new
            # attempt (see idempotency_service.py's module docstring).
            await asyncio.to_thread(
                mark_idempotency_failed, db, idempotency_claim_id, reservation_id=reservation_id,
            )
        if continuity_execution_id is not None:
            await asyncio.to_thread(
                true_up_continuity,
                db,
                user_id=_session_user_id,
                estimated_tcu=continuity_estimated_tcu,
                actual_tcu=continuity_actual_tcu,
                execution_id=continuity_execution_id,
            )
