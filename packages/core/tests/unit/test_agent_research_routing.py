"""After the agent reads the live web, the next call routes as research — held to a floor on MAX/TEAM.

Production, 2026-09-28 (MAX): the agent searched, then gpt-4o-mini ("low") wrote "the latest" from a year-old page;
the turn had been classified as chat, so neither the free-pool precision rule nor any quality floor applied.
"""
from __future__ import annotations

import pytest

from src.prompting.tool_policy import WEB_RESEARCH_TOOLS
from src.services.agent_service import RESEARCH_QUALITY_FLOOR, research_routing


def test_before_any_web_tool_the_request_routes_as_classified():
    assert research_routing("chat", "max", researched=False) == ("chat", None)


@pytest.mark.parametrize("plan", ["max", "team", "MAX"])
def test_after_a_web_tool_max_and_team_route_as_research_with_a_standard_floor(plan):
    assert research_routing("chat", plan, researched=True) == ("research", "standard")


@pytest.mark.parametrize("plan", ["free", "start", "pro"])
def test_other_plans_route_as_research_but_keep_their_own_floor(plan):
    """Research still keeps the free pool out on every plan; only the plans listed pay for a higher floor."""
    assert research_routing("agent", plan, researched=True) == ("research", None)


def test_the_web_tools_are_the_ones_the_agent_reads_the_web_with():
    assert WEB_RESEARCH_TOOLS == {"web_search", "browser_navigate", "browser_get_text"}
    assert set(RESEARCH_QUALITY_FLOOR) == {"max", "team"}


# ── The real agent loop ────────────────────────────────────────────────────

def test_the_agent_loop_routes_the_call_after_a_web_search_as_research(client, auth_headers, registered_user, db):
    """MAX turn: call 1 routes as classified; after its web_search ran, call 2 goes as research with the floor."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from trinity_router import RoutingResult

    from src.models.user import User

    db.query(User).filter(User.id == registered_user["user_id"]).one().plan = "max"
    db.commit()
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    def result(content=None, tool_calls=None):
        return RoutingResult(content=content, tool_calls=tool_calls, provider="openai", model="gpt-test",
                             input_tokens=10, output_tokens=5, cached_tokens=0, cost_usd=0.001, tcu=0.1,
                             failover_used=False, failover_reason=None)

    search = SimpleNamespace(id="c1", function=SimpleNamespace(
        name="web_search", arguments=json.dumps({"query": "último carro elétrico Brasil 2026"})))
    router = AsyncMock(side_effect=[result(tool_calls=[search]), result(content="Resposta. Sources: …")])
    with patch("src.services.agent_service._router.complete", new=router), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "1. Notícia\n   https://x"))):
        response = client.post(f"/sessions/{session_id}/agent", headers=auth_headers,
                               json={"content": "qual foi o último carro elétrico lançado no Brasil?"})

    assert response.status_code == 200
    first, second = (call.args[0] for call in router.await_args_list)
    assert first.capability != "research" and first.min_quality_class is None
    assert second.capability == "research" and second.min_quality_class == "standard"
    assert "against <today>" in second.messages[0]["content"]  # the reading rules rode along
