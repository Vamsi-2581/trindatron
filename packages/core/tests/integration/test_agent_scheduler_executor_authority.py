"""Etapa 5D correction, items 4 and 5.

Item 4 — "executor real > capability": proves the Agent Scheduler's
admission gate is driven by which executor will ACTUALLY run
(stream_agent()'s ReAct loop vs stream_chat()'s plain completion), never
by LoadClass. A FILES capability classifies as LoadClass.MEDIUM (Etapa
5C), but /message still dispatches it to stream_agent() (task_type ==
TaskType.AGENT) — so it must still go through the Scheduler's full
concurrency gate, exactly like Capability.AGENT itself. Also confirms,
by direct inspection, that agent_scheduler.py contains no reference to
LoadClass/LoadClassifier at all — the rule is enforced structurally, not
just behaviorally.

Item 5 — "Scheduler -> TCU order": proves both the accept path (Scheduler
accepts -> TCU reserves -> Agent executes) and the reject path (Scheduler
rejects -> TCU never reserved -> provider never called -> no ReAct loop ->
no orphaned execution) end to end, plus that the existing TCU reserve/
finalize/cancel semantics are untouched for a request the Scheduler DID
admit (a provider failure after acceptance still correctly cancels the
reservation — unchanged from pre-5D behavior).
"""
from __future__ import annotations

import inspect

import pytest
from unittest.mock import AsyncMock, patch

from trinity_router import ProviderUnavailableError, RoutingResult

from src.services import agent_scheduler as agent_scheduler_module
from src.services.agent_scheduler import agent_scheduler
from src.services.load_classification import LoadClass, load_classifier
from src.capability import Capability


def _routing_result() -> RoutingResult:
    return RoutingResult(
        content="done", tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=3, output_tokens=3, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )


# ── Item 4: structural check — agent_scheduler.py never touches LoadClass ──

def test_agent_scheduler_module_has_no_load_classification_dependency():
    """Checks actual CODE (imports + module namespace), not prose — the
    module docstring legitimately explains the LoadClass/executor
    distinction in English, which would false-positive a plain text
    search. `try_acquire()`'s only parameter is `user_id: str`; it has no
    way to even see a capability or LoadClass value."""
    module_names = set(agent_scheduler_module.__dict__)
    assert not module_names & {"LoadClass", "LoadClassifier", "load_classifier", "load_classification"}, (
        "agent_scheduler.py must never import LoadClass/LoadClassifier — "
        "admission is gated by executor (task_type), never by content "
        "classification. See agent_scheduler.py's module docstring."
    )

    source_lines = [
        line for line in inspect.getsource(agent_scheduler_module).splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert not any("load_classification" in line for line in source_lines), (
        f"agent_scheduler.py must not import load_classification: {source_lines}"
    )

    sig = inspect.signature(agent_scheduler_module.InMemoryAgentScheduler.try_acquire)
    assert list(sig.parameters) == ["self", "user_id"], (
        "try_acquire() must be driven only by user_id (executor decision is "
        "made by the CALLER — sessions.py's task_type check — not by "
        "anything Capability/LoadClass-shaped passed into the scheduler itself)"
    )


# ── Item 4: behavioral check — FILES (MEDIUM) still goes through the Scheduler ─

def test_files_capability_is_medium_but_still_gated_by_scheduler_like_heavy(client, auth_headers, monkeypatch):
    """Sanity-check the premise first: FILES really does classify as
    MEDIUM today (Etapa 5C) — if this ever changes, the second half of
    this test (proving MEDIUM does NOT get a free pass past the
    Scheduler) is what actually matters and must still hold regardless."""
    assert load_classifier.classify(Capability.FILES) == LoadClass.MEDIUM

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)  # scheduler fully exhausted

    called = {"provider": 0}

    async def _spy_complete(request):
        called["provider"] += 1
        return _routing_result()

    with patch("src.services.agent_service._router.complete", new=_spy_complete):
        # "read the file" classifies as Capability.FILES -> TaskType.AGENT
        # (see src/capability/pipelines.py's CAPABILITY_TASK_TYPE) — still
        # dispatches to stream_agent(), so it must still be rejected here.
        resp = client.post(
            f"/sessions/{session_id}/message", json={"content": "please read the file config.json"}, headers=auth_headers,
        )

    assert resp.status_code == 429, (
        "a FILES-capability request (MEDIUM per LoadClassifier) must still "
        "be rejected by an exhausted Agent Scheduler — MEDIUM must never "
        "bypass the HEAVY-equivalent concurrency gate for a real Agent/ReAct execution"
    )
    assert called["provider"] == 0


def test_files_capability_succeeds_through_scheduler_when_capacity_available(client, auth_headers, monkeypatch):
    """Companion positive case: with real capacity, the same FILES request
    is admitted and genuinely executes stream_agent() (the provider IS
    called) — confirming the gate is real admission control, not an
    accidental always-reject."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    called = {"provider": 0}

    async def _spy_complete(request):
        called["provider"] += 1
        return _routing_result()

    with patch("src.services.agent_service._router.complete", new=_spy_complete):
        resp = client.post(
            f"/sessions/{session_id}/message", json={"content": "please read the file config.json"}, headers=auth_headers,
        )

    assert resp.status_code == 200
    assert called["provider"] >= 1
    assert agent_scheduler.global_active_count == 0


# ── Item 5: Scheduler accepts -> TCU reserves -> Agent executes ────────────

def test_scheduler_accepts_then_tcu_reserves_then_agent_executes(client, auth_headers, monkeypatch, db):
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=_routing_result())):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 200
    # TCU was genuinely reserved-and-finalized for this accepted execution.
    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
    assert agent_scheduler.global_active_count == 0


# ── Item 5: Scheduler rejects -> nothing downstream ever runs ──────────────

def test_scheduler_rejects_then_nothing_downstream_runs(client, auth_headers, monkeypatch, db):
    from src.models.usage import UsageRecord
    from src.models.message import Message

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr(agent_scheduler._global, "_limit", 0)

    called = {"provider": 0, "reserve": 0}

    async def _spy_complete(request):
        called["provider"] += 1
        return _routing_result()

    def _spy_reserve(*args, **kwargs):
        called["reserve"] += 1
        raise AssertionError("check_and_reserve must not be called")

    with patch("src.services.agent_service._router.complete", new=_spy_complete), \
         patch("src.api.routes.sessions.check_and_reserve", new=_spy_reserve):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 429
    assert called["provider"] == 0
    assert called["reserve"] == 0
    assert db.query(UsageRecord).filter(UsageRecord.session_id == session_id).count() == 0
    assert db.query(Message).filter(Message.session_id == session_id).count() == 0, "no orphaned execution"


# ── Item 5: existing TCU reserve/finalize/cancel semantics untouched ───────

def test_provider_failure_after_scheduler_acceptance_still_cancels_reservation(client, auth_headers, db):
    """The Scheduler sitting in front of the TCU reservation must not
    change what happens to that reservation once execution is genuinely
    underway — a provider failure mid-execution must still cancel
    (not finalize) the reservation, exactly as before Etapa 5D."""
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    async def _failing_complete(request):
        raise ProviderUnavailableError("provider down")

    with patch("src.services.agent_service._router.complete", new=_failing_complete):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 200  # SSE stream itself completes (carries an error event)
    assert "unavailable" in resp.text.lower() or "error" in resp.text.lower()
    # cancel_reservation() marks the pending row status='failed' (it is
    # never deleted) — exactly the pre-5D semantics, unchanged.
    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
    assert records[0].status == "failed"
    assert agent_scheduler.global_active_count == 0
