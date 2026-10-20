"""An answer cut off by the plan's output limit says so (F-04 of the Android audit).

On the dev database every reply that ended mid-sentence or mid-table had stopped at exactly the Free plan's 1024
output tokens, and nothing told the person. Only the model router is scripted here; the route, the plan limits, the
persistence and the history are the real ones.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from trinity_router import RouterStreamChunk
from trinity_router.limits import get_plan_limits

from src.models.message import Message
from src.models.user import User
from src.services.chat_service import _hit_output_cap


def _set_plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


def _done(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: {"):
            event = json.loads(line[6:])
            if event.get("type") == "done":
                return event
    raise AssertionError(body)


def _router(output_tokens: int):
    async def _stream(request):
        yield RouterStreamChunk(text="| Hora | Atividade |\n|---|---|\n| 22:00 – 22")
        yield RouterStreamChunk(is_final=True, provider="p", model="m", input_tokens=10, output_tokens=output_tokens,
                                cost_usd=0.0, tcu=0.0)
    return _stream


@pytest.mark.parametrize("plan", ["free", "pro", "max"])
def test_the_cap_is_the_plans_own(plan):
    cap = get_plan_limits(plan).max_output_tokens_per_request
    assert _hit_output_cap(plan, cap)
    assert not _hit_output_cap(plan, cap - 1)


def test_a_free_answer_that_hits_the_limit_is_marked_live_and_in_the_history(client, db, auth_headers, registered_user):
    _set_plan(db, registered_user["user_id"], "free")
    cap = get_plan_limits("free").max_output_tokens_per_request
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _router(cap)):
        response = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                               json={"content": "Monte uma rotina da noite em tabela", "locale": "pt-BR"})
    assert response.status_code == 200
    assert _done(response.text)["truncated"] is True

    history = client.get(f"/sessions/{session_id}", headers=auth_headers).json()["messages"]
    assert [m["truncated"] for m in history] == [False, True]


def test_a_finished_answer_is_not_marked(client, db, auth_headers, registered_user):
    _set_plan(db, registered_user["user_id"], "free")
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _router(300)):
        response = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                               json={"content": "Monte uma rotina da noite em tabela", "locale": "pt-BR"})
    assert _done(response.text)["truncated"] is False
    stored = db.query(Message).filter(Message.session_id == session_id, Message.role == "assistant").one()
    assert stored.truncated is None  # NULL means "not truncated", the meaning every existing row already has
