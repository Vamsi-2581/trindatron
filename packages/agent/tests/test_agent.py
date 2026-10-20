"""
Agent ReAct loop tests — all router calls are mocked; no real API calls are made.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from trinity_agent.agent import TrinityAgent, AgentResult
from trinity_agent.permissions.gateway import PermissionGateway, PermissionDenied
from trinity_agent.permissions.manifest import PermissionLevel
from trinity_agent.security.audit import AuditLog
from trinity_agent.tools.filesystem import ReadFile, WriteFile, ListDirectory
from trinity_agent.tools.registry import ToolRegistry
from trinity_router import RoutingResult


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _routing_result(content: str | None = None, tool_calls=None) -> RoutingResult:
    return RoutingResult(
        content=content,
        tool_calls=tool_calls,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        cost_usd=0.0001,
        tcu=0.01,
        failover_used=False,
        failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)),
    )


def _make_registry(workspace):
    reg = ToolRegistry()
    reg.register(ReadFile(workspace))
    reg.register(WriteFile(workspace))
    reg.register(ListDirectory(workspace))
    return reg


def _auto_gateway():
    manifest = {name: PermissionLevel.AUTO for name in [
        "read_file", "write_file", "list_directory",
    ]}
    return PermissionGateway(manifest=manifest)


# ─── Direct answer (no tool call) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_direct_answer(workspace, tmp_path):
    reg = _make_registry(workspace)
    gw = _auto_gateway()
    agent = TrinityAgent(reg, gw, session_id="s1")

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(return_value=_routing_result("42"))):
        result = await agent.run("What is 6 × 7?")

    assert isinstance(result, AgentResult)
    assert result.answer == "42"
    assert result.iterations == 1
    assert result.tool_calls == []
    assert result.error == ""


# ─── Single tool call then answer ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_single_tool_call(workspace, tmp_path):
    (workspace / "notes.txt").write_text("secret ingredient: love")
    reg = _make_registry(workspace)
    gw = _auto_gateway()
    agent = TrinityAgent(reg, gw)

    responses = [
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "notes.txt"})]),
        _routing_result("The secret ingredient is love."),
    ]

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(side_effect=responses)):
        result = await agent.run("What is in notes.txt?")

    assert "love" in result.answer
    assert result.iterations == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["tool"] == "read_file"
    assert result.tool_calls[0]["success"] is True


# ─── Permission denied ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_permission_denied(workspace):
    reg = _make_registry(workspace)
    forbidden_gw = PermissionGateway(manifest={"write_file": PermissionLevel.FORBIDDEN})
    agent = TrinityAgent(reg, forbidden_gw)

    responses = [
        _routing_result(tool_calls=[_make_tool_call("write_file", {"path": "x.txt", "content": "x"})]),
        _routing_result("I could not write the file."),
    ]

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(side_effect=responses)):
        result = await agent.run("Write 'x' to x.txt")

    assert result.tool_calls[0]["success"] is False


# ─── Max iterations ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_max_iterations(workspace):
    reg = _make_registry(workspace)
    gw = _auto_gateway()
    agent = TrinityAgent(reg, gw, max_iterations=2)

    # Always returns a tool call, never a direct answer
    infinite_response = _routing_result(tool_calls=[_make_tool_call("list_directory", {})])

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(return_value=infinite_response)):
        result = await agent.run("Loop forever")

    assert result.error == "max_iterations_exceeded"
    assert result.iterations == 2


# ─── Audit log integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_records_audit_entries(workspace):
    (workspace / "data.txt").write_text("audit me")
    reg = _make_registry(workspace)
    gw = _auto_gateway()
    audit = AuditLog()
    agent = TrinityAgent(reg, gw, audit_log=audit, session_id="sess-99")

    responses = [
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "data.txt"})]),
        _routing_result("File content received."),
    ]

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(side_effect=responses)):
        await agent.run("Read data.txt")

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.tool_name == "read_file"
    assert entry.session_id == "sess-99"


# ─── Multiple tool calls in one iteration ─────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_multiple_tool_calls_in_one_step(workspace):
    (workspace / "a.txt").write_text("alpha")
    (workspace / "b.txt").write_text("beta")
    reg = _make_registry(workspace)
    gw = _auto_gateway()
    agent = TrinityAgent(reg, gw)

    tc_a = _make_tool_call("read_file", {"path": "a.txt"}, call_id="call_a")
    tc_b = _make_tool_call("read_file", {"path": "b.txt"}, call_id="call_b")

    responses = [
        _routing_result(tool_calls=[tc_a, tc_b]),
        _routing_result("Got both files."),
    ]

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(side_effect=responses)):
        result = await agent.run("Read both files")

    assert len(result.tool_calls) == 2
    assert result.answer == "Got both files."


# ─── Unknown tool gracefully handled ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_unknown_tool_handled(workspace):
    reg = _make_registry(workspace)
    gw = PermissionGateway(manifest={"ghost_tool": PermissionLevel.AUTO})
    agent = TrinityAgent(reg, gw)

    responses = [
        _routing_result(tool_calls=[_make_tool_call("ghost_tool", {"x": 1})]),
        _routing_result("Could not run ghost_tool."),
    ]

    with patch("trinity_agent.agent._router.complete", new=AsyncMock(side_effect=responses)):
        result = await agent.run("Use ghost_tool")

    assert result.tool_calls[0]["success"] is False
