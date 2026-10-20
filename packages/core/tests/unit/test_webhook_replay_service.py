"""Unit tests for src/services/webhook_replay_service.py — Production
Payment Activation hardening (DB-backed, multi-process-safe webhook
replay protection).

Test matrix
-----------
R01) was_nonce_processed() is False for a never-seen (provider, nonce) pair.
R02) record_nonce() then was_nonce_processed() -> True.
R03) record_nonce() called twice for the SAME pair never raises — idempotent.
R04) Scoped by provider — the same nonce string under a DIFFERENT provider
     is NOT considered a replay.
R05) cleanup_expired_nonces() deletes only rows older than retention_seconds;
     a fresh row survives.
R06) Concurrent race: two separate sessions (simulating two processes)
     both record_nonce() for the SAME pair — neither raises, exactly one
     row exists afterward.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models.webhook_nonce import WebhookNonce
from src.services import webhook_replay_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def test_R01_unseen_nonce_not_processed(db):
    assert webhook_replay_service.was_nonce_processed(db, "binance_pay", "nonce-abc") is False


def test_R02_recorded_nonce_is_processed(db):
    webhook_replay_service.record_nonce(db, "binance_pay", "nonce-abc")
    assert webhook_replay_service.was_nonce_processed(db, "binance_pay", "nonce-abc") is True


def test_R03_record_nonce_twice_is_idempotent(db):
    webhook_replay_service.record_nonce(db, "binance_pay", "nonce-abc")
    webhook_replay_service.record_nonce(db, "binance_pay", "nonce-abc")  # must NOT raise

    assert db.query(WebhookNonce).filter(
        WebhookNonce.provider == "binance_pay", WebhookNonce.nonce == "nonce-abc",
    ).count() == 1


def test_R04_scoped_by_provider(db):
    webhook_replay_service.record_nonce(db, "binance_pay", "shared-nonce")
    assert webhook_replay_service.was_nonce_processed(db, "binance_pay", "shared-nonce") is True
    assert webhook_replay_service.was_nonce_processed(db, "other_provider", "shared-nonce") is False


def test_R05_cleanup_deletes_only_expired(db):
    now = datetime.now(timezone.utc)
    old = WebhookNonce(provider="binance_pay", nonce="old-nonce", created_at=now - timedelta(hours=2))
    fresh = WebhookNonce(provider="binance_pay", nonce="fresh-nonce", created_at=now)
    db.add_all([old, fresh])
    db.commit()

    deleted = webhook_replay_service.cleanup_expired_nonces(db, retention_seconds=3600.0, now=now)

    assert deleted == 1
    remaining = {row.nonce for row in db.query(WebhookNonce).all()}
    assert remaining == {"fresh-nonce"}


def test_R06_concurrent_race_exactly_one_row(db):
    """Two separate sessions bound to the SAME underlying engine —
    simulating two processes racing to record the identical delivery.
    Neither call may raise; exactly one row must exist afterward."""
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)
    session_a = Session()
    session_b = Session()
    try:
        webhook_replay_service.record_nonce(session_a, "binance_pay", "race-nonce")
        webhook_replay_service.record_nonce(session_b, "binance_pay", "race-nonce")  # must NOT raise

        assert db.query(WebhookNonce).filter(WebhookNonce.nonce == "race-nonce").count() == 1
    finally:
        session_a.close()
        session_b.close()
