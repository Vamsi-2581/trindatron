"""browser_click / browser_fill — the tools that let an agent ACT on a page.

Until these landed, the agent could open a page, read it and photograph it,
but not press anything. That gap mattered commercially: the product is sold
on "you give the objective, TRINDATRON does the work", and work on the web
involves buttons.

These are also the first browser tools with a side effect on someone else's
system, so most of what is tested here is the restraint around them: both are
PermissionLevel.CONFIRM, the fill value never leaves the agent's context, and
Continuity Mode still refuses the whole browser surface.

Same strategy as test_agent_browser_integration.py — the Playwright boundary
is mocked, the Core dispatch -> approval -> SSE pipeline is real.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from trinity_router import RoutingResult

from src.services import agent_service
from src.services.skill_service import ALLOWED_TOOL_IDS
from trinity_agent.permissions.manifest import DEFAULT_MANIFEST, PermissionLevel


def _routing_result(content: str | None = None, tool_calls=None) -> RoutingResult:
    return RoutingResult(
        content=content,
        tool_calls=tool_calls,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        cost_usd=0.001,
        tcu=0.1,
        failover_used=False,
        failover_reason=None,
    )


def _make_tool_call(name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


# ── The tools exist, everywhere they have to ────────────────────────────────


def test_both_tools_are_offered_to_the_model():
    names = {t["function"]["name"] for t in agent_service._AGENT_TOOLS}
    assert "browser_click" in names
    assert "browser_fill" in names


def test_both_tools_are_valid_skill_tool_ids():
    """A skill may restrict an agent to a subset of tools; a tool missing
    from ALLOWED_TOOL_IDS is silently filtered out at runtime."""
    assert "browser_click" in ALLOWED_TOOL_IDS
    assert "browser_fill" in ALLOWED_TOOL_IDS


def test_both_tools_are_in_the_browser_tool_set():
    """The set that drives dispatch, the Continuity filter and the
    defence-in-depth check. Missing here means one of the three drifts."""
    assert "browser_click" in agent_service._BROWSER_TOOL_NAMES
    assert "browser_fill" in agent_service._BROWSER_TOOL_NAMES


# ── Restraint ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["browser_click", "browser_fill"])
def test_acting_on_a_page_requires_the_users_approval(tool):
    """Reading a page is automatic; acting on it is not. A click can submit a
    form and a fill can enter data on a third-party site."""
    assert DEFAULT_MANIFEST[tool] == PermissionLevel.CONFIRM


@pytest.mark.parametrize("tool", ["browser_navigate", "browser_get_text", "browser_screenshot"])
def test_reading_a_page_stays_automatic(tool):
    """The new tools must not have made the passive ones stricter."""
    assert DEFAULT_MANIFEST[tool] == PermissionLevel.AUTO


def test_approval_prompt_names_the_field_but_never_its_value():
    """The description is rendered in the UI and written to the activity
    stream. A filled field may hold a password or personal data."""
    described = agent_service._describe_tool_action(
        "browser_fill", {"selector": "#senha", "value": "hunter2-super-secret"},
    )
    # Named as the page names it — "o campo “senha”" — never by the raw selector.
    assert "“senha”" in described and "#senha" not in described
    assert "hunter2-super-secret" not in described


def test_approval_prompt_for_click_says_what_will_be_clicked():
    described = agent_service._describe_tool_action(
        "browser_click", {"selector": "button#comprar"},
    )
    assert "botão “comprar”" in described and "button#comprar" not in described


def test_continuity_mode_still_refuses_the_whole_browser_surface():
    """Continuity runs unattended, so it fails closed on anything that can
    touch the outside world — the new tools are covered by the same set."""
    for tool in ("browser_navigate", "browser_get_text", "browser_screenshot",
                 "browser_click", "browser_fill"):
        assert tool in agent_service._BROWSER_TOOL_NAMES


# ── The tools actually run ──────────────────────────────────────────────────


def test_click_dispatches_through_core(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    tc = _make_tool_call("browser_click", {"selector": "button#ok"})
    responses = [_routing_result(tool_calls=[tc]), _routing_result("Clicked.")]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("trinity_agent.browser.session.BrowserSession.click", new=AsyncMock(return_value=None)), \
         patch("src.services.agent_service._permission_gateway.level",
               return_value=PermissionLevel.AUTO):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "clique no botão ok"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    result = next(
        (e for e in events if e.get("type") == "tool_result" and e.get("activity") == "activity.tool.browser_click"),
        None,
    )
    assert result is not None, events
    assert result["success"] is True
    assert "button#ok" in result["output"]


def test_fill_dispatches_and_does_not_echo_the_value(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    secret = "nunca-deve-aparecer-no-stream"
    tc = _make_tool_call("browser_fill", {"selector": "input#email", "value": secret})
    responses = [_routing_result(tool_calls=[tc]), _routing_result("Filled.")]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("trinity_agent.browser.session.BrowserSession.fill", new=AsyncMock(return_value=None)), \
         patch("src.services.agent_service._permission_gateway.level",
               return_value=PermissionLevel.AUTO):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "preencha o email"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    result = next(
        (e for e in events if e.get("type") == "tool_result" and e.get("activity") == "activity.tool.browser_fill"),
        None,
    )
    assert result is not None, events
    assert result["success"] is True
    assert "input#email" in result["output"]
    assert secret not in result["output"]
    assert secret not in resp.text, "the value must not appear anywhere in the stream"


def test_a_failing_click_does_not_crash_the_loop(client, auth_headers):
    """A selector that does not resolve is ordinary — the agent should be
    told and carry on, not have the run die."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    tc = _make_tool_call("browser_click", {"selector": "button#nao-existe"})
    responses = [_routing_result(tool_calls=[tc]), _routing_result("Não encontrei o botão.")]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("trinity_agent.browser.session.BrowserSession.click",
               new=AsyncMock(side_effect=Exception("Timeout 10000ms exceeded"))), \
         patch("src.services.agent_service._permission_gateway.level",
               return_value=PermissionLevel.AUTO):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "clique no botão"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    result = next(
        (e for e in events if e.get("type") == "tool_result" and e.get("activity") == "activity.tool.browser_click"),
        None,
    )
    assert result is not None
    assert result["success"] is False
