"""Request Idempotency — Etapa 5I (Request Idempotency & Execution Recovery).

Protects POST /chat, /agent, /message against duplicate effects (a second
Message, a second TCU reservation, a second provider execution) when the
SAME logical request arrives more than once — a client-side network retry,
a double-tap, a proxy retry. OPT-IN via the `Idempotency-Key` HTTP header:
a request with no header is completely unaffected by this module, byte for
byte — see sessions.py's route handlers, which only call into this module
when the header is present.

Does NOT introduce a second financial ledger. `IdempotencyKey.reservation_id`
is a reference-only pointer into the EXISTING UsageRecord machinery;
reconciling a stale claim here NEVER computes cost/TCU itself — it only
ever calls the already-existing, already-tested cancel_reservation()
(full refund, identical to every other failed-attempt path in this
codebase).

── Race safety: the SAME proven pattern as CreditService ──────────────────
src/services/credit_service.py already solved "two concurrent requests
racing to claim the same idempotency key" for financial operations —
attempt the INSERT; a concurrent winner triggers a DB-level UNIQUE
violation (IntegrityError); the loser re-reads whatever the winner
actually committed and defers to it, never trusting its own pre-insert
read. This module reuses that exact mechanism (UNIQUE(user_id, key) on
IdempotencyKey) rather than inventing a second concurrency primitive —
an in-memory check-then-act (`if key in memory: return cached`) is
explicitly NOT what this is, precisely because it would not survive a
restart or a second process.

── States and their retry semantics ────────────────────────────────────────
  in_progress  — claimed, not yet resolved. A retry with the same key
                 while this is fresh (younger than
                 settings.idempotency_stale_after_seconds) gets
                 IdempotencyInProgressError -> HTTP 409, deterministic, no
                 waiting/polling. A retry while this is STALE (older than
                 the threshold — the owning process crashed or was killed
                 mid-request) is reconciled on the spot (see
                 _reconcile_one_stale_idempotency_row) and allowed to
                 proceed as a fresh attempt.
  completed    — a real result exists. A retry returns an IdempotencyReplay
                 pointing at the already-created session/message — NEVER a
                 second effect, and never a re-run of the operation.
  failed       — nothing useful was produced (a 429/503/404 gate rejection,
                 a provider error, a genuine interruption). A retry is
                 treated as a brand-new attempt under the SAME row —
                 correct because every "failed" exit in this codebase
                 already means the associated UsageRecord/Continuity debit
                 was fully cancelled/refunded (see stream_chat()/
                 stream_agent()'s own `finally` blocks) — there is nothing
                 left over to duplicate.

── Fingerprint ──────────────────────────────────────────────────────────
A cheap, deterministic hash (sha256, NOT semantic/LLM comparison — same
"deterministic and cheap" requirement already established for the Agent
loop's own circuit breakers in Etapa 5H) of (endpoint, session_id,
content). Reusing the same Idempotency-Key for a DIFFERENT fingerprint
raises IdempotencyConflictError -> HTTP 409 — the key is never silently
reinterpreted as a different operation (same principle as
credit_service.py's DuplicateIdempotencyKey).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.idempotency_key import IdempotencyKey
from src.observability.metrics import core_metrics
from src.services.entitlement_service import cancel_reservation

logger = logging.getLogger("trinity.idempotency")

_claims_total = core_metrics.counter(
    "idempotency_claims_total", "Idempotency key claim outcomes, labeled by outcome.",
)
_reconciled_total = core_metrics.counter(
    "idempotency_reconciled_total", "Stale idempotency rows reconciled, labeled by trigger (startup|on_demand).",
)

_MAX_RESOLVE_ATTEMPTS = 5  # bounded retry under contention — mirrors CreditService._MAX_RETRIES's spirit


class IdempotencyInProgressError(Exception):
    """A request with this Idempotency-Key is currently being processed
    by another, still-live request. Maps to HTTP 409 in the route
    handler. Never waited-on/polled — the client is expected to retry
    later, same as any other 429/409 in this codebase."""

    def __init__(self, key: str) -> None:
        super().__init__(f"A request with Idempotency-Key {key!r} is already in progress.")
        self.key = key


class IdempotencyConflictError(Exception):
    """The same Idempotency-Key was already used for a DIFFERENT logical
    operation (different endpoint, session, or content). Maps to HTTP 409
    — never silently reinterpreted as the new operation."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Idempotency-Key {key!r} was already used for a different operation.")
        self.key = key


@dataclass(frozen=True)
class IdempotencyReplay:
    """A previously COMPLETED operation under this key. The route handler
    MUST return this directly (a small JSON confirmation, not a fresh SSE
    stream) instead of running the operation again — see sessions.py."""
    session_id: str | None
    message_id: str | None


def make_fingerprint(*, endpoint: str, session_id: str, content: str) -> str:
    """Deterministic, cheap identity for "is this the SAME logical
    operation" — no LLM call, no semantic/embedding comparison (same
    determinism requirement already established for Etapa 5H's Agent-loop
    circuit breakers). Any change to session_id or content under the same
    key is treated as an incompatible reuse."""
    raw = f"{endpoint}\x1f{session_id}\x1f{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_idempotency_key(
    db: DBSession, *, user_id: str, key: str, endpoint: str, fingerprint: str,
) -> str | IdempotencyReplay:
    """The ONE entry point every route handler uses when a client sends
    an Idempotency-Key header. Returns either:
      - a claim id (str) — proceed with the request; the caller MUST
        eventually call mark_completed()/mark_failed() with this id (via
        stream_chat()/stream_agent()'s own `finally` block — same
        threading pattern already used for reservation_id/
        continuity_execution_id).
      - an IdempotencyReplay — return it directly, run nothing.
    Raises IdempotencyInProgressError / IdempotencyConflictError (the
    route handler maps both to HTTP 409).
    """
    for _attempt in range(_MAX_RESOLVE_ATTEMPTS):
        existing = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            .first()
        )

        if existing is None:
            row = IdempotencyKey(
                user_id=user_id, key=key, endpoint=endpoint, fingerprint=fingerprint, status="in_progress",
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue  # lost the race — re-read on the next loop iteration
            db.refresh(row)
            _claims_total.inc(outcome="claimed")
            return row.id

        if existing.fingerprint != fingerprint:
            _claims_total.inc(outcome="conflict")
            raise IdempotencyConflictError(existing.key)

        if existing.status == "completed":
            _claims_total.inc(outcome="replayed")
            return IdempotencyReplay(session_id=existing.response_session_id, message_id=existing.response_message_id)

        if existing.status == "failed":
            if _try_reclaim(db, existing.id, from_status="failed"):
                _claims_total.inc(outcome="retried_after_failure")
                return existing.id
            continue  # someone else's retry won the race — re-read and defer to it

        # status == "in_progress" — live concurrent request, or an orphan
        # from a crashed process.
        age_s = (datetime.now(timezone.utc) - existing.updated_at).total_seconds()
        if age_s < settings.idempotency_stale_after_seconds:
            _claims_total.inc(outcome="in_progress_conflict")
            raise IdempotencyInProgressError(existing.key)

        _reconcile_one_stale_idempotency_row(db, existing)
        if _try_reclaim(db, existing.id, from_status="failed"):
            _claims_total.inc(outcome="retried_after_stale")
            return existing.id
        continue

    # Exhausted retries under pathological contention — fail closed with
    # the same 409 a live in-progress request would produce, never a
    # silent duplicate.
    raise IdempotencyInProgressError(key)


def _try_reclaim(db: DBSession, row_id: str, *, from_status: str) -> bool:
    """Atomic UPDATE ... WHERE status = :from_status — only one concurrent
    caller can ever flip it to "in_progress"; the others see 0 rows
    updated and must re-read/defer, exactly like CreditBalance's own
    optimistic-lock UPDATE ... WHERE version = :expected pattern."""
    updated = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.id == row_id, IdempotencyKey.status == from_status)
        .update({"status": "in_progress"}, synchronize_session=False)
    )
    db.commit()
    return bool(updated)


def mark_completed(db: DBSession, claim_id: str | None, *, session_id: str, message_id: str) -> None:
    """Called from stream_chat()/stream_agent()'s own `finally` block,
    exactly where reservation finalization already happens — same
    "only on genuine success" timing as `_cancel_on_exit = False`."""
    if claim_id is None:
        return
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim_id).first()
    if row is None:
        logger.warning("idempotency: mark_completed on missing claim | id=%s", claim_id)
        return
    row.status = "completed"
    row.response_session_id = session_id
    row.response_message_id = message_id
    db.commit()


def mark_failed(db: DBSession, claim_id: str | None, *, reservation_id: str | None = None) -> None:
    """Called from every non-success exit path — mirrors
    cancel_reservation()'s own "any failure path" contract. `reservation_id`
    is stored so a FUTURE stale-reconciliation (if this row itself later
    becomes orphaned before a retry consumes it — not the common case,
    since "failed" already means fully refunded, but kept for defense in
    depth) has something to double-check."""
    if claim_id is None:
        return
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim_id).first()
    if row is None:
        logger.warning("idempotency: mark_failed on missing claim | id=%s", claim_id)
        return
    row.status = "failed"
    if reservation_id is not None:
        row.reservation_id = reservation_id
    db.commit()


def _reconcile_one_stale_idempotency_row(db: DBSession, row: IdempotencyKey) -> None:
    """Financial safety net for a single stale claim: if it's still
    holding a PENDING UsageRecord reservation, cancel it via the EXISTING
    cancel_reservation() primitive (full refund — identical semantics to
    every other failed-attempt path; no new refund rule invented here)."""
    if row.reservation_id is not None:
        cancel_reservation(db, row.reservation_id)
    row.status = "failed"
    db.commit()
    _reconciled_total.inc(trigger="on_demand")
    logger.warning(
        "idempotency: reconciled stale in_progress row | id=%s key=%s age_s=%.0f",
        row.id, row.key, (datetime.now(timezone.utc) - row.updated_at).total_seconds(),
    )


def reconcile_stale_idempotency_keys_at_startup(db: DBSession) -> int:
    """Unconditional startup sweep — called once from main.py's
    lifespan(), alongside the existing _run_startup_cleanup(). ANY row
    still "in_progress" when the process boots is, by construction, from
    a PREVIOUS process instance (this one hasn't served a single request
    yet) — no age threshold needed here, unlike the on-demand check in
    claim_idempotency_key(). Returns the number of rows reconciled."""
    stale = db.query(IdempotencyKey).filter(IdempotencyKey.status == "in_progress").all()
    for row in stale:
        if row.reservation_id is not None:
            cancel_reservation(db, row.reservation_id)
        row.status = "failed"
    if stale:
        db.commit()
        _reconciled_total.inc(len(stale), trigger="startup")
    return len(stale)
