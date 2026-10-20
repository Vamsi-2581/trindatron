"""
Tests for RoutingRequest.capability field and its propagation through ModelRouter.

Verifies that:
1. RoutingRequest has a capability field with None default (backward compat).
2. capability flows from RoutingRequest → _build_chain → select_provider_chain.
3. capability=None does not change routing behavior for any existing path.
4. The provider chains selected remain identical whether capability is None or set.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from trinity_router import ModelRouter, RoutingRequest, TaskType


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_response(content="ok"):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.prompt_tokens_details = None
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


# ── RoutingRequest field contract ─────────────────────────────────────────────

class TestRoutingRequestCapabilityField:
    def test_capability_field_default_is_none(self):
        req = RoutingRequest(
            messages=[],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        )
        assert req.capability is None

    def test_capability_field_accepts_string(self):
        req = RoutingRequest(
            messages=[],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
            capability="code",
        )
        assert req.capability == "code"

    @pytest.mark.parametrize("cap", [
        "chat", "research", "code", "work", "files", "automation", "agent",
        "image", "video", "audio",
    ])
    def test_capability_accepts_all_capability_values(self, cap):
        req = RoutingRequest(
            messages=[],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
            capability=cap,
        )
        assert req.capability == cap

    def test_existing_fields_not_affected(self):
        req = RoutingRequest(
            messages=[{"role": "user", "content": "hi"}],
            task_type=TaskType.AGENT,
            user_plan="pro",
            session_id="sess-99",
            max_output_tokens=2048,
            capability="research",
        )
        assert req.task_type == TaskType.AGENT
        assert req.user_plan == "pro"
        assert req.session_id == "sess-99"
        assert req.max_output_tokens == 2048
        assert req.messages == [{"role": "user", "content": "hi"}]


# ── capability flows to select_provider_chain ─────────────────────────────────

class TestCapabilityFlowsThroughRouter:
    async def test_capability_reaches_select_provider_chain(self, monkeypatch):
        """capability string in RoutingRequest is forwarded to select_provider_chain."""
        captured: dict = {}

        import trinity_router.router as _router_module

        original_select = _router_module.select_provider_chain

        def _spy(task_type, user_plan, capability=None):
            captured["capability"] = capability
            return original_select(task_type, user_plan, capability)

        monkeypatch.setattr(_router_module, "select_provider_chain", _spy)
        monkeypatch.setattr(
            _router_module.litellm, "acompletion",
            AsyncMock(return_value=_make_response()),
        )

        router = ModelRouter()
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "write a sort function"}],
            task_type=TaskType.AGENT,
            user_plan="start",
            session_id="s1",
            capability="code",
        ))

        assert captured.get("capability") == "code"

    async def test_none_capability_forwarded_as_none(self, monkeypatch):
        """capability=None (default) is forwarded, not replaced with a string."""
        captured: dict = {}

        import trinity_router.router as _router_module

        original_select = _router_module.select_provider_chain

        def _spy(task_type, user_plan, capability=None):
            captured["capability"] = capability
            return original_select(task_type, user_plan, capability)

        monkeypatch.setattr(_router_module, "select_provider_chain", _spy)
        monkeypatch.setattr(
            _router_module.litellm, "acompletion",
            AsyncMock(return_value=_make_response()),
        )

        router = ModelRouter()
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))

        assert captured.get("capability") is None

    async def test_provider_chain_identical_regardless_of_capability(self, monkeypatch):
        """Adding capability does not change the provider actually selected."""
        selected_models: list[str] = []

        import trinity_router.router as _router_module

        original_build = ModelRouter._build_chain

        def _spy_build(self, task_type, user_plan, plan_limits, capability=None, forced_provider_key=None, disabled_provider_keys=frozenset(), messages=None, tools=None):
            chain = original_build(self, task_type, user_plan, plan_limits, capability, forced_provider_key, disabled_provider_keys, messages, tools)
            selected_models.extend(k for k, _ in chain)
            return chain

        monkeypatch.setattr(ModelRouter, "_build_chain", _spy_build)
        monkeypatch.setattr(
            _router_module.litellm, "acompletion",
            AsyncMock(return_value=_make_response()),
        )

        router = ModelRouter()

        # Without capability
        selected_models.clear()
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
        ))
        chain_without = list(selected_models)

        # With capability="chat"
        selected_models.clear()
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}],
            task_type=TaskType.CHAT,
            user_plan="start",
            session_id="s1",
            capability="chat",
        ))
        chain_with = list(selected_models)

        assert chain_without == chain_with

    async def test_complete_returns_result_with_capability_set(self, monkeypatch):
        """RoutingResult is returned normally when capability is set."""
        import trinity_router.router as _router_module

        monkeypatch.setattr(
            _router_module.litellm, "acompletion",
            AsyncMock(return_value=_make_response("answer")),
        )

        router = ModelRouter()
        result = await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "research this"}],
            task_type=TaskType.AGENT,
            user_plan="pro",
            session_id="s1",
            capability="research",
        ))

        assert result.content == "answer"
        assert result.provider != ""
