"""Etapa 5I — unit tests for src/services/idempotency_service.py.

Exercises claim_idempotency_key()'s branches directly (no HTTP layer):
fresh claim, replay-after-completed, reject-in-progress, reject-conflict,
reclaim-after-failed, and both reconciliation paths (on-demand stale +
startup sweep). HTTP-level wiring (the Idempotency-Key header, 409 mapping,
route-handler replay short-circuit) is covered separately in
tests/integration/test_idempotency_http.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.models.idempotency_key import IdempotencyKey
from src.models.usage import UsageRecord
from src.services.entitlement_service import check_and_reserve
from src.services.idempotency_service import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    claim_idempotency_key,
    make_fingerprint,
    mark_completed,
    mark_failed,
    reconcile_stale_idempotency_keys_at_startup,
)


@pytest.fixture
def user_id(client, registered_user, db):
    # `client`/`registered_user` registers a real user (see conftest.py) so
    # user_id is a genuine FK target, matching every other test file's
    # convention (test_entitlement.py, test_credit_service.py, etc.).
    return registered_user["user_id"]


def _fp(content: str = "hello", endpoint: str = "chat", session_id: str = "s1") -> str:
    return make_fingerprint(endpoint=endpoint, session_id=session_id, content=content)


# ── make_fingerprint — deterministic, sensitive to every input ─────────────

def test_fingerprint_is_deterministic():
    a = make_fingerprint(endpoint="chat", session_id="s1", content="hi")
    b = make_fingerprint(endpoint="chat", session_id="s1", content="hi")
    assert a == b


@pytest.mark.parametrize("kwargs", [
    {"endpoint": "agent"},
    {"session_id": "s2"},
    {"content": "bye"},
])
def test_fingerprint_changes_with_any_input(kwargs):
    base = make_fingerprint(endpoint="chat", session_id="s1", content="hi")
    varied_kwargs = {"endpoint": "chat", "session_id": "s1", "content": "hi", **kwargs}
    varied = make_fingerprint(**varied_kwargs)
    assert base != varied


# ── Test 1 (unit level) / claim on a fresh key ──────────────────────────────

def test_claim_returns_new_id_for_fresh_key(db, user_id):
    key = f"k-{uuid.uuid4()}"
    claim = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=_fp())
    assert isinstance(claim, str)
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim).first()
    assert row is not None
    assert row.status == "in_progress"
    assert row.user_id == user_id
    assert row.key == key


# ── Test 6 — different keys are fully independent ───────────────────────────

def test_different_keys_are_independent(db, user_id):
    claim_a = claim_idempotency_key(db, user_id=user_id, key=f"a-{uuid.uuid4()}", endpoint="chat", fingerprint=_fp("A"))
    claim_b = claim_idempotency_key(db, user_id=user_id, key=f"b-{uuid.uuid4()}", endpoint="chat", fingerprint=_fp("B"))
    assert claim_a != claim_b
    assert db.query(IdempotencyKey).count() == 2


# ── Test 4 — retry while genuinely in progress (fresh) is deterministic ────

def test_retry_while_fresh_in_progress_raises_deterministic_conflict(db, user_id):
    key = f"k-{uuid.uuid4()}"
    fp = _fp()
    first = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)
    assert isinstance(first, str)

    with pytest.raises(IdempotencyInProgressError):
        claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)

    # No second row was created — same claim, still in_progress.
    assert db.query(IdempotencyKey).filter(IdempotencyKey.key == key).count() == 1


# ── Test 5 — same key, incompatible payload → rejection, never silent reuse ─

def test_retry_with_different_fingerprint_raises_conflict(db, user_id):
    key = f"k-{uuid.uuid4()}"
    claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=_fp("hello"))

    with pytest.raises(IdempotencyConflictError):
        claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=_fp("goodbye"))


# ── Test 3 — retry after success replays, never re-executes ─────────────────

def test_retry_after_completed_returns_replay_not_a_new_effect(db, user_id):
    key = f"k-{uuid.uuid4()}"
    fp = _fp()
    claim = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)
    mark_completed(db, claim, session_id="sess-123", message_id="msg-456")

    result = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)

    assert isinstance(result, IdempotencyReplay)
    assert result.session_id == "sess-123"
    assert result.message_id == "msg-456"
    # Still exactly one row — a replay never inserts a second claim.
    assert db.query(IdempotencyKey).filter(IdempotencyKey.key == key).count() == 1


# ── Test 11 — retry after a failed attempt reclaims the SAME row ───────────

def test_retry_after_failed_reclaims_same_row_as_new_attempt(db, user_id):
    key = f"k-{uuid.uuid4()}"
    fp = _fp()
    claim = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)
    mark_failed(db, claim)

    retried = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)

    assert retried == claim  # same row, reclaimed — not a second economic operation
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim).first()
    assert row.status == "in_progress"
    assert db.query(IdempotencyKey).filter(IdempotencyKey.key == key).count() == 1


# ── Stale in_progress (crashed process) is reconciled on-demand ────────────

def test_stale_in_progress_with_reservation_is_reconciled_and_reclaimed(db, user_id):
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=None, plan="start")
    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "pending"

    key = f"k-{uuid.uuid4()}"
    fp = _fp()
    claim = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)
    mark_failed(db, claim, reservation_id=reservation_id)
    # Simulate the SAME claim being reused later as if it were still
    # in_progress from a crashed process (age beyond the stale threshold).
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim).first()
    row.status = "in_progress"
    row.reservation_id = reservation_id
    row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=settings.idempotency_stale_after_seconds + 60)
    db.commit()

    retried = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=fp)

    assert retried == claim
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim).first()
    assert row.status == "in_progress"  # reclaimed fresh for the new attempt
    db.refresh(reservation)
    assert reservation.status == "failed"  # REGRA FINANCEIRA CRÍTICA: refunded, not left pending forever
    assert float(reservation.tcu) == 0.0


# ── Test 10 — startup sweep reconciles every in_progress row unconditionally ─

def test_reconcile_stale_idempotency_keys_at_startup(db, user_id):
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=None, plan="start")

    orphan = IdempotencyKey(
        user_id=user_id, key=f"orphan-{uuid.uuid4()}", endpoint="chat",
        fingerprint=_fp(), status="in_progress", reservation_id=reservation_id,
    )
    db.add(orphan)
    db.commit()
    db.refresh(orphan)

    count = reconcile_stale_idempotency_keys_at_startup(db)

    assert count == 1
    db.refresh(orphan)
    assert orphan.status == "failed"
    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "failed"
    assert float(reservation.tcu) == 0.0


def test_reconcile_stale_idempotency_keys_at_startup_is_a_noop_when_none_stale(db, user_id):
    key = f"k-{uuid.uuid4()}"
    claim = claim_idempotency_key(db, user_id=user_id, key=key, endpoint="chat", fingerprint=_fp())
    mark_completed(db, claim, session_id="s", message_id="m")

    count = reconcile_stale_idempotency_keys_at_startup(db)

    assert count == 0
    row = db.query(IdempotencyKey).filter(IdempotencyKey.id == claim).first()
    assert row.status == "completed"  # untouched
