"""Etapa 5I — real-concurrency proof for idempotency claims (test 2/13 of
the required scenarios: "duas requests simultâneas + mesma chave -> um
único efeito", verified with genuine OS threads against a real PostgreSQL
instance).

Mirrors test_entitlement.py::test_concurrent_reservation_only_one_succeeds
exactly: gated by tests/postgres_helpers.py::postgres_available()
(TEST_POSTGRES_URL), its own engine/session, entirely independent of the
SQLite `db`/`client` fixtures used by the rest of the suite. SQLite
serializes all writes (single-writer) so this race cannot be reproduced
there — IdempotencyKey's UNIQUE(user_id, key) constraint is what makes the
race safe, and only a real concurrent-transaction DB proves it.
"""
from __future__ import annotations

import threading
import uuid as uuid_module

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.models  # noqa: F401 — registers all models on Base.metadata
from src.database import Base
from src.models.user import User
from src.services.idempotency_service import (
    IdempotencyInProgressError,
    claim_idempotency_key,
    make_fingerprint,
)
from tests.postgres_helpers import get_test_postgres_url, postgres_available


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_concurrent_same_key_claims_only_one_succeeds():
    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    try:
        user = User(
            id=str(uuid_module.uuid4()),
            email=f"pgidempotency_{uuid_module.uuid4().hex[:8]}@test.com",
            hashed_password="x",
            plan="start",
        )
        setup_db.add(user)
        setup_db.commit()
        user_id = user.id
    finally:
        setup_db.close()

    key = f"race-{uuid_module.uuid4()}"
    fingerprint = make_fingerprint(endpoint="chat", session_id="s1", content="hello")

    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)  # maximize the chance both threads race together
            claim = claim_idempotency_key(session, user_id=user_id, key=key, endpoint="chat", fingerprint=fingerprint)
            results[label] = ("claimed", claim)
        except IdempotencyInProgressError:
            results[label] = ("rejected", None)
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    outcomes = [results["t1"][0], results["t2"][0]]
    assert outcomes.count("claimed") == 1, f"expected exactly one winner, got {results}"
    assert outcomes.count("rejected") == 1, f"expected exactly one deterministic rejection, got {results}"

    verify_db = Session()
    try:
        from src.models.idempotency_key import IdempotencyKey
        rows = verify_db.query(IdempotencyKey).filter(IdempotencyKey.key == key).all()
        assert len(rows) == 1  # the UNIQUE(user_id, key) constraint — never two rows for one race
    finally:
        verify_db.close()
        engine.dispose()
