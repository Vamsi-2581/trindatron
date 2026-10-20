"""Shared fixtures for model-router tests."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from trinity_router.metrics import router_metrics
from trinity_router.selector import TaskType


@pytest.fixture(autouse=True)
def _reset_router_metrics():
    """Etapa 5G: router_metrics is a process-wide singleton (same pattern
    as Core's request_control/agent_scheduler) — its counters/histograms
    would otherwise accumulate across every test in the whole pytest
    session. Purely additive: does not touch any other fixture."""
    router_metrics.reset()
    yield
    router_metrics.reset()


# ---------------------------------------------------------------------------
# Helpers to build mock litellm responses
# ---------------------------------------------------------------------------

def _make_litellm_response(
    content: str = "OK",
    tool_calls=None,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Build a minimal mock of a litellm completion response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls

    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


@pytest.fixture()
def mock_litellm_ok(monkeypatch):
    """Patch litellm.acompletion to return a successful response."""
    mock = AsyncMock(return_value=_make_litellm_response())
    monkeypatch.setattr("litellm.acompletion", mock)
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", mock)
    return mock


@pytest.fixture()
def mock_litellm_fail(monkeypatch):
    """Patch litellm.acompletion to always raise an exception."""
    mock = AsyncMock(side_effect=Exception("connection refused"))
    monkeypatch.setattr("litellm.acompletion", mock)
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", mock)
    return mock


@pytest.fixture()
def base_request() -> dict:
    """Minimal valid RoutingRequest kwargs."""
    return {
        "messages": [{"role": "user", "content": "Hello"}],
        "task_type": TaskType.CHAT,
        "user_plan": "start",
        "session_id": "test-session-001",
    }
