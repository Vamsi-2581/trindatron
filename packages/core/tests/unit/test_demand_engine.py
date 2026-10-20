"""Demand Engine — aggregated capability demand telemetry.

Covers: what gets counted, that refusals are counted separately as unmet
demand, the snapshot shape, and the two properties the module exists to
guarantee — it records no content/identifiers, and it can never fail a
request.
"""
from __future__ import annotations

from src.capability import Capability
from src.services.demand_engine import (
    demand_snapshot,
    record_demand,
    record_unmet_demand,
)
from trinity_router import TaskType


def _count(rows: list[dict], **match) -> int:
    return sum(
        row["count"] for row in rows
        if all(row.get(k) == v for k, v in match.items())
    )


# -- What is counted -------------------------------------------------------


def test_record_demand_counts_one_request():
    record_demand(
        endpoint="/chat", capability=Capability.CHAT,
        task_type=TaskType.CHAT, user_plan="start",
    )
    rows = demand_snapshot()["demand"]
    assert _count(rows, endpoint="/chat", capability="chat", plan="start") == 1


def test_record_demand_accumulates_per_label_combination():
    for _ in range(3):
        record_demand(
            endpoint="/chat", capability=Capability.CHAT,
            task_type=TaskType.CHAT, user_plan="free",
        )
    record_demand(
        endpoint="/chat", capability=Capability.CHAT,
        task_type=TaskType.CHAT, user_plan="pro",
    )
    rows = demand_snapshot()["demand"]
    assert _count(rows, plan="free", capability="chat") == 3
    assert _count(rows, plan="pro", capability="chat") == 1


def test_enum_and_plain_string_labels_are_equivalent():
    """Call sites shouldn't have to remember `.value` for one argument."""
    record_demand(endpoint="/message", capability=Capability.CHAT, task_type=TaskType.CHAT, user_plan="max")
    record_demand(endpoint="/message", capability="chat", task_type="chat", user_plan="max")
    rows = demand_snapshot()["demand"]
    assert _count(rows, endpoint="/message", capability="chat", plan="max") == 2


def test_unmet_demand_is_a_separate_counter():
    record_demand(endpoint="/agent", capability=Capability.CHAT, task_type=TaskType.AGENT, user_plan="free")
    record_unmet_demand(
        capability=Capability.CHAT, task_type=TaskType.AGENT,
        reason="agent_mode_not_entitled", user_plan="free",
    )
    snap = demand_snapshot()
    assert _count(snap["demand"], endpoint="/agent", plan="free") == 1
    assert _count(snap["unmet"], reason="agent_mode_not_entitled", plan="free") == 1


def test_snapshot_is_sorted_by_count_descending():
    for _ in range(5):
        record_demand(endpoint="/chat", capability=Capability.CHAT, task_type=TaskType.CHAT, user_plan="start")
    record_demand(endpoint="/agent", capability=Capability.CHAT, task_type=TaskType.AGENT, user_plan="start")
    counts = [row["count"] for row in demand_snapshot()["demand"]]
    assert counts == sorted(counts, reverse=True)


def test_empty_snapshot_has_both_keys():
    snap = demand_snapshot()
    assert snap == {"demand": [], "unmet": []}


# -- Privacy ---------------------------------------------------------------


def test_recorded_labels_carry_no_identifiers_or_content():
    """The whole design rests on this: only closed-set labels are stored.
    No user id, no session id, no message text - there is no argument that
    could carry them and no field that could hold them."""
    record_demand(endpoint="/chat", capability=Capability.CHAT, task_type=TaskType.CHAT, user_plan="start")
    record_unmet_demand(
        capability=Capability.CHAT, task_type=TaskType.AGENT,
        reason="agent_mode_not_entitled", user_plan="start",
    )
    snap = demand_snapshot()
    allowed = {"capability", "task_type", "endpoint", "plan", "reason", "count"}
    for row in snap["demand"] + snap["unmet"]:
        assert set(row) <= allowed, f"unexpected label recorded: {set(row) - allowed}"


# -- Never fails a request -------------------------------------------------


def test_record_demand_never_raises_on_bad_input():
    class Exploding:
        @property
        def value(self):
            raise RuntimeError("boom")

    record_demand(endpoint="/chat", capability=Exploding(), task_type=None, user_plan="start")
    record_unmet_demand(capability=None, task_type=None, reason="x", user_plan=None)


def test_record_demand_never_raises_when_the_registry_fails(monkeypatch):
    import src.services.demand_engine as de

    class Broken:
        def inc(self, *a, **kw):
            raise RuntimeError("registry down")

    monkeypatch.setattr(de, "_demand_capability_total", Broken())
    record_demand(endpoint="/chat", capability=Capability.CHAT, task_type=TaskType.CHAT, user_plan="start")
