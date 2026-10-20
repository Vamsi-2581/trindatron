"""Definitive regression tests for the connection-retention fix in
stream_chat()/stream_agent() (src/services/chat_service.py,
src/services/agent_service.py).

Root cause (see the session's own architectural audit): SQLAlchemy's ORM
Session uses "autobegin" — any bare read (no subsequent commit/rollback)
leaves a pool connection checked out. Both stream_chat() and stream_agent()
used to leave such a connection checked out across the entire provider
call (up to the FULL ReAct loop for /agent — potentially every iteration).
The fix adds one `db.commit()` right before the provider is ever awaited,
plus capturing `session.id`/`session.user_id` into plain locals BEFORE that
commit (SQLAlchemy's default `expire_on_commit=True` would otherwise
silently reissue a blocking SELECT — and re-acquire a connection — the
next time `session.id` is touched, defeating the fix).

Everything here runs against a REAL PostgreSQL instance — gated by
tests/postgres_helpers.py::postgres_available() (TEST_POSTGRES_URL), the
same disposable, test-only database already used by test_entitlement.py's
own PostgreSQL-gated tests. SQLite's SingletonThreadPool/StaticPool
wouldn't exercise the QueuePool checkout/checkin semantics this fix is
actually about. No provider is ever called for real (every _router.stream/
_router.complete call site is monkeypatched); no load test against a real
HTTP/ASGI server is performed here — Test 3 calls stream_chat() directly,
mirroring the existing direct-generator-call pattern already established
by test_agent_scheduler_streaming.py, to reproduce the original pool-
exhaustion failure at a small, fast, deterministic scale instead of
through a real network/ASGI transport (which has its own, separately
audited, timeout-enforcement gaps — see the session's own harness
diagnosis).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy.exc
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from trinity_router import RoutingResult, RouterStreamChunk

import src.models  # noqa: F401 — registers all models on Base.metadata
from src.database import Base
from src.models.agent_execution import AgentExecution
from src.models.idempotency_key import IdempotencyKey
from src.models.message import Message
from src.models.session import Session as SessionModel
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import agent_service, chat_service
from src.services.agent_execution_service import create_admitted_execution
from src.services.entitlement_service import check_and_reserve
from src.services.idempotency_service import claim_idempotency_key, make_fingerprint
from tests.postgres_helpers import get_test_postgres_url, postgres_available

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not postgres_available(),
        reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py",
    ),
]


# ── Shared helpers ───────────────────────────────────────────────────────

def _engine_and_sessionmaker(*, pool_size: int = 5, max_overflow: int = 10, pool_timeout: int = 30):
    """A fresh, isolated engine/pool against the disposable PostgreSQL test
    database — never the SQLite `db`/`client` fixtures used by the rest of
    the suite. Local to each test (never a module-level singleton) so
    tests can vary pool_size/max_overflow (Test 3) without affecting each
    other. `pool_timeout` deliberately short by default is set per-test
    where it matters (Test 3) so a genuine regression fails FAST instead of
    hanging for the production default of 30s x N.
    """
    engine = create_engine(
        get_test_postgres_url(), pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


def _make_account(Session, plan: str = "pro") -> tuple[str, str]:
    """Create one user + one session directly (bypasses bcrypt/HTTP
    registration — not what these tests measure). Returns (user_id,
    session_id); commits and closes its own short-lived setup Session."""
    db = Session()
    try:
        user = User(
            id=str(uuid.uuid4()), email=f"conn-{uuid.uuid4().hex[:12]}@test.local",
            hashed_password="unused", plan=plan,
        )
        db.add(user)
        sess = SessionModel(id=str(uuid.uuid4()), user_id=user.id, model_default="")
        db.add(sess)
        db.commit()
        return user.id, sess.id
    finally:
        db.close()


async def _drain(agen):
    events = []
    async for event in agen:
        events.append(event)
    return events


def _direct_answer(content: str = "final answer") -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0, cost_usd=0.001, tcu=0.5,
        failover_used=False, failover_reason=None,
    )


class _UnknownToolCall:
    id = "call_1"

    class function:
        name = "nonexistent_tool_for_this_test"
        arguments = "{}"


def _tool_call_result() -> RoutingResult:
    # Mirrors test_agent_scheduler_streaming.py's own _ToolCallResult shape
    # exactly — an unknown-tool call fails as a normal ToolResult (not an
    # exception), which does NOT trip either Etapa 5H circuit breaker on a
    # single occurrence, so the loop cleanly proceeds to a 2nd iteration.
    return RoutingResult(
        content=None, tool_calls=[_UnknownToolCall()], provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )


# ── TESTE 1 — /chat: connection released before the provider call ──────────

async def test_chat_releases_connection_before_provider_call(monkeypatch):
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

        observed_checkedout: list[int] = []

        async def _stream_mock(request):
            # Measured INSIDE the provider mock itself — not before/after —
            # exactly the instant production code would be talking to a
            # real provider over the network.
            observed_checkedout.append(engine.pool.checkedout())
            yield RouterStreamChunk(text="hi")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=10, output_tokens=2, cached_tokens=0, cost_usd=0.001, tcu=0.5,
            )

        monkeypatch.setattr(chat_service._router, "stream", _stream_mock)

        events = await _drain(chat_service.stream_chat(db, sess, "hello", reservation_id=reservation_id))

        assert any('"type": "done"' in e for e in events)
        assert observed_checkedout == [0], (
            f"expected the pool connection to be fully released before the provider call; "
            f"observed checkedout()={observed_checkedout} at the moment(s) the provider mock ran"
        )
        db.close()
    finally:
        engine.dispose()


# ── TESTE 2 — /agent: connection released on EVERY ReAct iteration ─────────

async def test_agent_releases_connection_on_every_react_iteration(monkeypatch):
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
        execution_id = create_admitted_execution(
            db, user_id=user_id, session_id=session_id, reservation_id=reservation_id,
            continuity_execution_id=None, idempotency_key_id=None,
        )

        observed_checkedout: list[int] = []
        call_count = {"n": 0}

        async def _complete_mock(request):
            call_count["n"] += 1
            observed_checkedout.append(engine.pool.checkedout())
            # Iteration 1: a tool call (forces a 2nd iteration). Iteration
            # 2+: a direct answer (ends the loop). Guarantees >= 2 real
            # provider calls, both measured.
            if call_count["n"] == 1:
                return _tool_call_result()
            return _direct_answer()

        monkeypatch.setattr(agent_service._router, "complete", _complete_mock)

        events = await _drain(agent_service.stream_agent(
            db, sess, "hello", reservation_id=reservation_id, agent_execution_id=execution_id,
        ))

        assert any('"type": "done"' in e for e in events)
        assert call_count["n"] >= 2, f"expected >= 2 ReAct iterations, got {call_count['n']}"
        assert observed_checkedout == [0] * call_count["n"], (
            f"expected the pool connection to be released before EVERY iteration's provider "
            f"call, not just the first; observed checkedout()={observed_checkedout}"
        )
        db.close()
    finally:
        engine.dispose()


# ── TESTE 3 — small pool + real concurrency, distinct users ─────────────────

async def test_chat_ten_concurrent_users_with_undersized_pool_no_deadlock(monkeypatch):
    """Reproduces the original failure mode (pool_size smaller than the
    number of genuinely concurrent /chat requests) at a small scale —
    pool_size=2, max_overflow=0, N=10 distinct users (5x oversubscription,
    deliberate).

    ── History of this test's detection mechanism ─────────────────────────
    v1 used "does the whole batch finish without ANY error before
    pool_timeout elapses" as its signal. That's a race against real OS/
    executor/DB scheduling, inherently non-deterministic, and empirically
    became flaky once entitlement_service.finalize_usage() stopped holding
    a connection through a trailing db.refresh() (a correct optimization
    that happens AFTER the provider call returns — outside the window this
    test is actually about — but shifts overall timing enough to expose
    the race).

    v2 sampled the ENGINE-WIDE engine.pool.checkedout() at each provider
    entry (mirroring Testes 1/2) — wrong under genuine N=10 concurrency:
    that counter is global, so it reads non-zero simply because some OTHER
    request is legitimately mid-admission/persist at that instant. A false
    positive (Testes 1/2 never had this problem — they only ever have ONE
    request in flight).

    v3 (this version) checks THIS request's OWN Session —
    `db.in_transaction()` — at the exact instant of its own provider call.
    Verified empirically: a bare SELECT with no following commit leaves
    in_transaction() True (connection held); commit() makes it False;
    another query makes it True again. Per-request, immune to what any
    OTHER concurrent request is doing.

    ── Why "zero errors, all N complete" was itself the wrong bar ─────────
    A heavily-instrumented standalone reproduction (see the session's own
    diagnosis) proved: even with BOTH fixes correctly applied and
    db.in_transaction()==False for every single request, pool_size=2 can
    legitimately not finish serving N=10 full request lifecycles (~5-6
    sequential connection checkouts each) before an individual
    sqlalchemy.exc.TimeoutError fires for one or two of them — genuine
    queueing under deliberate 5x oversubscription, which is pool_timeout
    working exactly as designed, not a bug. Raising pool_timeout does not
    fix this: it lets each stuck wave wait longer, and waves can cascade
    (observed: a run taking 34s at pool_timeout=8s, driven by three
    consecutive exactly-8s stalls) — so no fixed pool_timeout converges to
    "always zero errors" without either masking a real regression (too
    large) or flaking on totally benign contention (too small). Chasing
    that number is therefore the wrong lever, exactly as this whole
    investigation set out to avoid.

    This test therefore checks two SEPARATE, honestly-scoped properties:
      1. Connection retention (the actual 9378494 regression) — checked
         with zero dependency on timing: for every request that reaches
         the provider at all, db.in_transaction() must be False at that
         instant. Verified during development via git-stash bisection:
         reverting chat_service.py's fix makes this fail immediately and
         reliably, every single request, every run.
      2. No PERMANENT deadlock and no UNEXPECTED failure mode: every
         request either succeeds or fails with exactly
         sqlalchemy.exc.TimeoutError (the pool's own clean, designed
         backpressure) — never any other exception type, and the batch
         resolves at all within a generous bounded window (never hangs
         forever). At least one request must succeed, proving genuine
         progress happens under this deliberately extreme pool.

    pool_timeout=5 / outer wait_for=90 are bounded, reasonable values —
    NOT tuned to chase a "zero errors" outcome (property 2 above tolerates
    clean TimeoutErrors); they only bound how long a genuine hang could
    take before this test itself fails loudly instead of blocking the
    suite forever.
    """
    engine, Session = _engine_and_sessionmaker(pool_size=2, max_overflow=0, pool_timeout=5)
    try:
        N = 10
        accounts = [_make_account(Session) for _ in range(N)]
        # session_id -> that request's own db Session, so the shared mock
        # (there's only one _router.stream attribute to patch) can look up
        # and check the RIGHT Session's own in_transaction() state —
        # RoutingRequest.session_id round-trips through stream_chat()
        # unchanged, so this correlation is exact, not inferred.
        session_dbs: dict[str, object] = {}
        observed_in_transaction: list[tuple[str, bool]] = []

        async def _stream_mock(request):
            # Measured INSIDE the provider mock, exactly like Testes 1/2 —
            # but scoped to THIS request's own Session (see docstring for
            # why the engine-wide pool counter doesn't work under real
            # concurrency).
            db_for_this_request = session_dbs[request.session_id]
            observed_in_transaction.append((request.session_id, db_for_this_request.in_transaction()))
            await asyncio.sleep(1.0)  # a genuinely async wait, simulating a
            # real streaming provider call — irrelevant to detection now,
            # since the assertion below is a per-request point-in-time
            # check, never a race against a timeout.
            yield RouterStreamChunk(text="hi")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=10, output_tokens=2, cached_tokens=0, cost_usd=0.001, tcu=0.5,
            )

        monkeypatch.setattr(chat_service._router, "stream", _stream_mock)

        def _get_session_sync(db, session_id):
            return db.query(SessionModel).filter(SessionModel.id == session_id).one()

        async def one_request(user_id: str, session_id: str):
            db = Session()  # own Session per "request" — mirrors get_db()'s per-request lifecycle
            session_dbs[session_id] = db
            try:
                # Mirrors sessions.py's real route handler: every synchronous
                # DB call runs via asyncio.to_thread(), never directly on the
                # event loop — a blocking pool-checkout wait (pool_size=2,
                # max_overflow=0 here) executed directly on the loop would
                # freeze every other concurrent task, including the ones
                # that would otherwise release their connections. That
                # would be a bug in this TEST, not in the fix under test.
                sess = await asyncio.to_thread(_get_session_sync, db, session_id)
                reservation_id = await asyncio.to_thread(
                    check_and_reserve, db, user_id=user_id, session_id=session_id, plan="pro",
                )
                events = await _drain(chat_service.stream_chat(db, sess, "hello", reservation_id=reservation_id))
                return events
            finally:
                db.close()

        results = await asyncio.wait_for(
            asyncio.gather(*(one_request(u, s) for u, s in accounts), return_exceptions=True),
            timeout=90,  # bounded safety net against a genuine permanent hang —
            # not a "zero errors" race (see docstring property 2). 30 was too close to the
            # real run: ~17 s on Linux-like timing, 31-38 s on the Windows self-hosted runner
            # (PostgreSQL behind Docker Desktop's port proxy), where it failed 3 runs in 5.
        )
        assert len(results) == N

        # Property 2a — no UNEXPECTED failure mode. Under this deliberate
        # 5x oversubscription, a clean sqlalchemy.exc.TimeoutError from the
        # pool's own backpressure is an ACCEPTABLE, designed outcome — any
        # OTHER exception type would indicate a real bug (deadlock,
        # corruption, an unhandled code path) and must fail the test.
        for r in results:
            if isinstance(r, BaseException):
                assert isinstance(r, sqlalchemy.exc.TimeoutError), (
                    f"expected only clean pool-timeout failures under this deliberately "
                    f"oversubscribed pool, got an unexpected exception type: {r!r}"
                )

        succeeded = [r for r in results if not isinstance(r, BaseException)]
        # Property 2b — no PERMANENT deadlock: genuine progress happened.
        assert len(succeeded) >= 1, "expected at least some requests to succeed — a total lockup would mean a real deadlock"
        for events in succeeded:
            assert any('"type": "done"' in e for e in events)

        # Property 1 — THE deterministic connection-retention regression
        # signal, a per-request check (db.in_transaction()), immune to
        # what any OTHER concurrent request in this same batch is doing,
        # and to whether this specific request went on to succeed or hit a
        # clean pool timeout during a LATER phase. See the docstring for
        # why this is structurally guaranteed to catch the original
        # 9378494 bug, independent of scheduling/timeout/completion order.
        assert len(observed_in_transaction) >= 1, "no request ever reached the provider — test setup is broken"
        still_in_transaction = [sid for sid, in_txn in observed_in_transaction if in_txn]
        assert not still_in_transaction, (
            f"expected every request's OWN connection to be released before ITS OWN "
            f"provider call, even under N={N} concurrency against pool_size=2; "
            f"sessions still holding a connection at their own provider entry: "
            f"{still_in_transaction}"
        )

        # No DUPLICATION of effect, for ANY of the N sessions regardless of
        # whether that specific request succeeded or hit a clean pool
        # timeout: at most 1 user message and at most 1 assistant message —
        # a lone "user" message with no assistant reply (1 message total)
        # is a legitimate, pre-existing outcome (same as any interruption
        # between _prepare_chat_context's own early commit of the user
        # message and the later assistant-message persist — see
        # test_chat_provider_failure_still_cancels_reservation_correctly
        # and this session's own idempotency-HTTP findings for the
        # identical, already-accepted pattern) — never MORE than one of
        # each, which is what would indicate genuine duplication.
        verify_db = Session()
        try:
            for user_id, session_id in accounts:
                messages = verify_db.query(Message).filter(Message.session_id == session_id).all()
                assert len(messages) <= 2, f"session {session_id}: expected at most 2 messages, got {len(messages)}"
                assert sum(1 for m in messages if m.role == "user") <= 1, f"session {session_id}: duplicated user message"
                assert sum(1 for m in messages if m.role == "assistant") <= 1, f"session {session_id}: duplicated assistant message"
                completed = (
                    verify_db.query(UsageRecord)
                    .filter(UsageRecord.session_id == session_id, UsageRecord.status == "completed")
                    .all()
                )
                assert len(completed) <= 1, f"session {session_id}: expected at most 1 completed UsageRecord, got {len(completed)}"
        finally:
            verify_db.close()
    finally:
        engine.dispose()


# ── TESTE 4 — business-invariant regression ─────────────────────────────────

async def test_chat_tcu_and_message_persistence_unaffected_by_fix(monkeypatch):
    """TCU reservation -> finalize, and Message persistence, produce
    exactly the same end state as before the fix — the extra commit is a
    read-only checkpoint, never a business-logic change."""
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

        async def _stream_mock(request):
            yield RouterStreamChunk(text="Hello there!")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=42, output_tokens=7, cached_tokens=3, cost_usd=0.00123, tcu=1.23,
            )

        monkeypatch.setattr(chat_service._router, "stream", _stream_mock)
        await _drain(chat_service.stream_chat(db, sess, "hi", reservation_id=reservation_id))
        db.close()

        verify_db = Session()
        try:
            record = verify_db.query(UsageRecord).filter(UsageRecord.id == reservation_id).one()
            assert record.status == "completed"
            assert float(record.tcu) == pytest.approx(1.23)
            assert float(record.cost_usd) == pytest.approx(0.00123)
            assert record.input_tokens == 42
            assert record.output_tokens == 7

            messages = (
                verify_db.query(Message)
                .filter(Message.session_id == session_id)
                .order_by(Message.created_at)
                .all()
            )
            assert [m.role for m in messages] == ["user", "assistant"]
            assert messages[0].content == "hi"
            assert messages[1].content == "Hello there!"
        finally:
            verify_db.close()
    finally:
        engine.dispose()


async def test_chat_provider_failure_still_cancels_reservation_correctly(monkeypatch):
    """The failure/cancel path (finally: cancel_reservation when
    _cancel_on_exit) must still fully refund the reservation — the new
    commit must not interfere with this pre-existing invariant."""
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

        async def _failing_stream(request):
            raise ConnectionError("simulated provider outage")
            yield  # pragma: no cover — makes this an async generator function

        monkeypatch.setattr(chat_service._router, "stream", _failing_stream)
        events = await _drain(chat_service.stream_chat(db, sess, "hi", reservation_id=reservation_id))
        assert any('"type": "error"' in e for e in events)
        db.close()

        verify_db = Session()
        try:
            record = verify_db.query(UsageRecord).filter(UsageRecord.id == reservation_id).one()
            assert record.status == "failed"
            assert float(record.tcu) == 0.0
            assert float(record.cost_usd) == 0.0
        finally:
            verify_db.close()
    finally:
        engine.dispose()


async def test_agent_execution_and_tcu_states_unaffected_by_fix(monkeypatch):
    """AgentExecution's persisted lifecycle (Etapa 5I) and TCU finalize end
    in exactly the same state as before the fix, across a real multi-
    iteration ReAct loop."""
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
        execution_id = create_admitted_execution(
            db, user_id=user_id, session_id=session_id, reservation_id=reservation_id,
            continuity_execution_id=None, idempotency_key_id=None,
        )

        call_count = {"n": 0}

        async def _complete_mock(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _tool_call_result()
            return _direct_answer("done via tool")

        monkeypatch.setattr(agent_service._router, "complete", _complete_mock)
        await _drain(agent_service.stream_agent(
            db, sess, "hi", reservation_id=reservation_id, agent_execution_id=execution_id,
        ))
        db.close()

        verify_db = Session()
        try:
            execution = verify_db.query(AgentExecution).filter(AgentExecution.id == execution_id).one()
            assert execution.status == "completed"
            assert execution.iterations_done == call_count["n"]
            assert execution.message_id is not None

            record = verify_db.query(UsageRecord).filter(UsageRecord.id == reservation_id).one()
            assert record.status == "completed"
        finally:
            verify_db.close()
    finally:
        engine.dispose()


async def test_chat_idempotency_completion_unaffected_by_fix(monkeypatch):
    """The Etapa 5I idempotency claim still ends 'completed', pointing at
    the real session/message ids, exactly as before the fix."""
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

        fingerprint = make_fingerprint(endpoint="chat", session_id=session_id, content="hi")
        claim_id = claim_idempotency_key(
            db, user_id=user_id, key=f"idem-{uuid.uuid4()}", endpoint="chat", fingerprint=fingerprint,
        )

        async def _stream_mock(request):
            yield RouterStreamChunk(text="ok")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.0, tcu=0.0,
            )

        monkeypatch.setattr(chat_service._router, "stream", _stream_mock)
        await _drain(chat_service.stream_chat(
            db, sess, "hi", reservation_id=reservation_id, idempotency_claim_id=claim_id,
        ))
        db.close()

        verify_db = Session()
        try:
            row = verify_db.query(IdempotencyKey).filter(IdempotencyKey.id == claim_id).one()
            assert row.status == "completed"
            assert row.response_session_id == session_id
            assert row.response_message_id is not None
        finally:
            verify_db.close()
    finally:
        engine.dispose()


async def test_chat_continuity_true_up_still_invoked_with_correct_args(monkeypatch):
    """Continuity's true-up call (Ledger/CreditBalance reconciliation,
    already covered by its own dedicated suite) is still invoked at the
    same point, with the same arguments, after the fix — this only proves
    the CALL SITE wiring survived, not true_up_continuity()'s own internal
    correctness (unchanged, still covered by test_continuity_execution.py)."""
    engine, Session = _engine_and_sessionmaker()
    try:
        user_id, session_id = _make_account(Session)
        db = Session()
        sess = db.query(SessionModel).filter(SessionModel.id == session_id).one()

        async def _stream_mock(request):
            yield RouterStreamChunk(text="ok")
            yield RouterStreamChunk(
                is_final=True, provider="anthropic", model="claude-sonnet-test",
                input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.002, tcu=2.0,
            )

        monkeypatch.setattr(chat_service._router, "stream", _stream_mock)

        calls = []

        def _fake_true_up(db_arg, *, user_id, estimated_tcu, actual_tcu, execution_id):
            calls.append((user_id, estimated_tcu, actual_tcu, execution_id))

        monkeypatch.setattr("src.services.chat_service.true_up_continuity", _fake_true_up)

        await _drain(chat_service.stream_chat(
            db, sess, "hi",
            continuity_execution_id="continuity-exec-1", continuity_estimated_tcu=5.0,
        ))
        db.close()

        assert calls == [(user_id, 5.0, 2.0, "continuity-exec-1")]
    finally:
        engine.dispose()
