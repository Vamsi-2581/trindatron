"""TRINITY CODE / WORK loops are bounded by the reservation, call by call.

Found in the 2026-09-13 economic audit: stream_code_task and stream_work_task
reserved ONE max_tcu_per_request estimate and then ran up to 20 model calls
with no budget re-check and no per-call cost ceiling. agent_service.py had
closed exactly that gap months earlier; these two loops never got the fix.

The consequence was structural, not cosmetic: a PRO user sitting just below
their weekly ceiling could start three concurrent tasks and spend roughly
1,400 TCU past it before finalize_usage() recorded anything — about $70 of
provider spend against a $19.90 subscription, entirely within the rules.

Pinned here, without a model or a database:
  * every call carries capability="code"/"work" and the reservation's current
    balance as its cost ceiling;
  * a refused iteration budget stops the loop and records what was spent;
  * an error after spending records that spend instead of cancelling it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services import code_service


def _tool_call(name: str, args: dict, call_id: str = "c1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _result(*, tool_calls=None, content="", tcu=1.5):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, model="gpt-4o-mini", provider="openai",
        input_tokens=1_000, output_tokens=100, cached_tokens=0,
        cost_usd=tcu / 100.0, tcu=tcu, failover_used=False, failover_reason=None,
    )


class _FakeRouter:
    def __init__(self, results):
        self._results = list(results)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def ledger(monkeypatch):
    """Record finalize/cancel calls and script the budget answers."""
    state = SimpleNamespace(finalized=[], cancelled=[], budget_answers=[], budget_tcu=5.0)

    monkeypatch.setattr(code_service, "finalize_usage",
                        lambda db, rid, **kw: state.finalized.append((rid, kw)))
    monkeypatch.setattr(code_service, "cancel_reservation",
                        lambda db, rid: state.cancelled.append(rid))
    monkeypatch.setattr(code_service, "get_reservation_budget_tcu",
                        lambda db, rid: state.budget_tcu)
    monkeypatch.setattr(code_service, "check_agent_iteration_budget",
                        lambda *a, **k: state.budget_answers.pop(0) if state.budget_answers else True)
    return state


def _workspace(tmp_path):
    return SimpleNamespace(root_path=str(tmp_path), language_hint=None, test_command=None)


async def _drain(gen) -> list[str]:
    return [event async for event in gen]


async def test_every_code_call_carries_the_capability_and_the_reservations_ceiling(tmp_path, monkeypatch, ledger):
    router = _FakeRouter([_result(content="done")])
    monkeypatch.setattr(code_service, "_router", router)

    await _drain(code_service.stream_code_task(
        MagicMock(), _workspace(tmp_path), "say done", reservation_id="r1",
        user_id="u1", user_plan="pro",
    ))

    [request] = router.requests
    assert request.capability == "code"
    assert request.max_cost_usd_override == pytest.approx(ledger.budget_tcu / 100.0)


async def test_a_later_call_gets_what_is_left_not_what_was_already_spent(tmp_path, monkeypatch, ledger):
    """The reservation's `tcu` column means two things: check_agent_iteration_budget rewrites it to
    the running total so the usage sums stay true, and get_reservation_budget_tcu reads it as the
    ceiling. Re-reading it each iteration therefore capped call N+1 at what call N had just spent, so
    the router refused every candidate and a loop that used a tool could never answer. The fake here
    reproduces that rewrite — the old one returned a constant, which is why nothing caught it."""
    reads = iter([5.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(code_service, "get_reservation_budget_tcu", lambda db, rid: next(reads))
    router = _FakeRouter([
        _result(tool_calls=[_tool_call("list_directory", {"path": "."})], tcu=2.0),
        _result(content="done", tcu=1.0),
    ])
    monkeypatch.setattr(code_service, "_router", router)

    await _drain(code_service.stream_code_task(
        MagicMock(), _workspace(tmp_path), "list then answer", reservation_id="r1",
        user_id="u1", user_plan="pro",
    ))

    first, second = router.requests
    assert first.max_cost_usd_override == pytest.approx(0.05), "the first call gets the whole grant"
    assert second.max_cost_usd_override == pytest.approx(0.03), (
        "the second call must get grant minus spend (5.0 - 2.0 TCU), not the 2.0 already spent"
    )


async def test_a_refused_iteration_budget_stops_the_loop_and_keeps_the_spend(tmp_path, monkeypatch, ledger):
    listing = _result(tool_calls=[_tool_call("list_directory", {"path": "."})], tcu=2.0)
    router = _FakeRouter([listing, listing, listing, listing])
    monkeypatch.setattr(code_service, "_router", router)
    ledger.budget_answers = [True, False]   # the second re-check refuses

    events = await _drain(code_service.stream_code_task(
        MagicMock(), _workspace(tmp_path), "list files forever", max_iterations=20,
        reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    assert len(router.requests) == 2, "the loop kept calling the model after its budget was refused"
    [(rid, recorded)] = ledger.finalized
    assert rid == "r1" and recorded["tcu"] == pytest.approx(4.0)
    assert ledger.cancelled == []
    assert any('"done"' in e for e in events)


async def test_an_error_after_spending_records_the_spend_instead_of_cancelling(tmp_path, monkeypatch, ledger):
    listing = _result(tool_calls=[_tool_call("list_directory", {"path": "."})], tcu=3.0)
    router = _FakeRouter([listing, RuntimeError("provider exploded")])
    monkeypatch.setattr(code_service, "_router", router)

    events = await _drain(code_service.stream_code_task(
        MagicMock(), _workspace(tmp_path), "list then crash",
        reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    assert any('"error"' in e for e in events)
    [(_, recorded)] = ledger.finalized
    assert recorded["tcu"] == pytest.approx(3.0), "billed calls were refunded by cancelling the reservation"
    assert ledger.cancelled == []


@pytest.fixture
def work_ledger(monkeypatch):
    """work_service imports the same helpers by name, so it is patched on its
    own module; _record_partial_usage lives in code_service and calls
    code_service.finalize_usage, which is patched there too."""
    from src.services import work_service

    state = SimpleNamespace(finalized=[], cancelled=[], budget_answers=[], budget_tcu=5.0)
    record = lambda db, rid, **kw: state.finalized.append((rid, kw))  # noqa: E731
    monkeypatch.setattr(work_service, "finalize_usage", record)
    monkeypatch.setattr(code_service, "finalize_usage", record)
    monkeypatch.setattr(work_service, "cancel_reservation",
                        lambda db, rid: state.cancelled.append(rid))
    monkeypatch.setattr(work_service, "get_reservation_budget_tcu",
                        lambda db, rid: state.budget_tcu)
    monkeypatch.setattr(work_service, "check_agent_iteration_budget",
                        lambda *a, **k: state.budget_answers.pop(0) if state.budget_answers else True)
    return work_service, state


# An unregistered tool name: ToolRegistry.call() answers success=False without
# touching the network or an approval gate, which is all the loop needs.
_INERT_TOOL = "no_such_tool"


async def test_every_work_call_carries_the_capability_and_the_reservations_ceiling(monkeypatch, work_ledger):
    work_service, state = work_ledger
    router = _FakeRouter([_result(content="done")])
    monkeypatch.setattr(work_service, "_router", router)

    await _drain(work_service.stream_work_task(
        MagicMock(), SimpleNamespace(), "say done", reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    [request] = router.requests
    assert request.capability == "work"
    assert request.max_cost_usd_override == pytest.approx(state.budget_tcu / 100.0)


async def test_a_refused_work_budget_stops_the_loop_and_keeps_the_spend(monkeypatch, work_ledger):
    work_service, state = work_ledger
    step = _result(tool_calls=[_tool_call(_INERT_TOOL, {})], tcu=2.5)
    router = _FakeRouter([step, step, step])
    monkeypatch.setattr(work_service, "_router", router)
    state.budget_answers = [False]

    await _drain(work_service.stream_work_task(
        MagicMock(), SimpleNamespace(), "loop", max_iterations=8,
        reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    assert len(router.requests) == 1
    [(_, recorded)] = state.finalized
    assert recorded["tcu"] == pytest.approx(2.5)
    assert state.cancelled == []


async def test_a_work_error_after_spending_records_the_spend(monkeypatch, work_ledger):
    work_service, state = work_ledger
    router = _FakeRouter([_result(tool_calls=[_tool_call(_INERT_TOOL, {})], tcu=1.25),
                          RuntimeError("provider exploded")])
    monkeypatch.setattr(work_service, "_router", router)

    await _drain(work_service.stream_work_task(
        MagicMock(), SimpleNamespace(), "step then crash",
        reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    [(_, recorded)] = state.finalized
    assert recorded["tcu"] == pytest.approx(1.25)
    assert state.cancelled == []


async def test_an_error_before_any_spend_still_cancels(tmp_path, monkeypatch, ledger):
    router = _FakeRouter([RuntimeError("provider exploded")])
    monkeypatch.setattr(code_service, "_router", router)

    await _drain(code_service.stream_code_task(
        MagicMock(), _workspace(tmp_path), "crash at once",
        reservation_id="r1", user_id="u1", user_plan="pro",
    ))

    assert ledger.finalized == []
    assert ledger.cancelled == ["r1"]
