"""
Integration tests for the Capability Router.

Verifies that the capability classifier and pipeline mapping work correctly
within the full import graph (trinity_router, etc. must be available).

Execution Mode etapa (2026-09-07): get_task_type()/CAPABILITY_TASK_TYPE are
no longer /message's authority for CODE/RESEARCH — see
infer_execution_mode() in test_execution_mode.py for the tests that
replaced the old "CODE always maps to AGENT" / "RESEARCH always maps to
AGENT" assumptions this file used to encode. get_task_type() itself is
kept (legitimate remaining callers: chat_service.py/agent_service.py's
internal RoutingRequest.task_type, load_classification.py's
forced_task_type derivation — all observability/candidate-filtering only,
never a real "does this need Agent" decision anymore), so the mapping
itself is still tested here — just no longer treated as decisive for
CODE/RESEARCH routing.
"""
from __future__ import annotations

import pytest

from src.capability.classifier import Capability, classify
from src.capability.pipelines import (
    CAPABILITY_TASK_TYPE,
    UNIMPLEMENTED_CAPABILITIES,
    get_task_type,
)
from trinity_router import TaskType


class TestCapabilityToTaskTypeMapping:

    def test_all_capabilities_have_a_task_type(self):
        """Every Capability member must appear in CAPABILITY_TASK_TYPE."""
        for cap in Capability:
            assert cap in CAPABILITY_TASK_TYPE, f"{cap} missing from CAPABILITY_TASK_TYPE"

    def test_chat_maps_to_chat_task(self):
        assert get_task_type(Capability.CHAT) == TaskType.CHAT

    @pytest.mark.parametrize("capability", [
        Capability.WORK,
        Capability.FILES,
        Capability.AUTOMATION,
        Capability.AGENT,
    ])
    def test_unconditional_agent_capabilities_map_to_agent_task(self, capability):
        """WORK/FILES/AUTOMATION/AGENT are the capabilities whose own
        classifier trigger phrases already imply real action — unlike
        CODE/RESEARCH (removed from this parametrization — see
        test_execution_mode.py), there is no "topic without execution
        intent" case for these four, so the plain capability->task_type
        mapping is still correct and unconditional for them."""
        assert get_task_type(capability) == TaskType.AGENT

    @pytest.mark.parametrize("capability", [
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.AUDIO,
    ])
    def test_multimodal_capabilities_are_unimplemented(self, capability):
        assert capability in UNIMPLEMENTED_CAPABILITIES

    def test_unimplemented_capabilities_are_subset_of_all(self):
        all_caps = set(Capability)
        assert UNIMPLEMENTED_CAPABILITIES.issubset(all_caps)


class TestClassifyIntegration:

    def test_classify_returns_capability_enum(self):
        result = classify("Hello")
        assert isinstance(result, Capability)

    def test_classify_fallback_maps_to_chat_task(self):
        cap = classify("Tell me a joke")
        task = get_task_type(cap)
        assert task == TaskType.CHAT

    def test_code_classify_still_returns_code_capability(self):
        """classify() itself (WHAT the topic is) is untouched by this
        etapa — only HOW it executes (infer_execution_mode(), tested in
        test_execution_mode.py) changed. get_task_type(CODE) alone still
        returns AGENT (it's the legacy, capability-only mapping, kept for
        its other legitimate callers) — /message no longer calls it for
        this capability, which is exactly what test_execution_mode.py's
        "code text-only -> CHAT" test proves."""
        cap = classify("write a function to reverse a string")
        assert cap == Capability.CODE
        assert get_task_type(cap) == TaskType.AGENT  # legacy mapping value, unchanged

    def test_research_classify_still_returns_research_capability(self):
        cap = classify("search the web for Python tutorials")
        assert cap == Capability.RESEARCH
        assert get_task_type(cap) == TaskType.AGENT  # legacy mapping value, unchanged


class TestExecutionModeToolsetAttachment:
    """Route-level proof (items 21/22 of the Execution Mode etapa): the
    toolset a request actually reaches the provider with is a direct
    consequence of infer_execution_mode()'s decision, via which service
    function runs (chat_service.stream_chat() never attaches tools;
    agent_service.stream_agent() always attaches the real toolset) — not
    of the topic capability alone. Uses a "start"-plan user (no FREE
    entitlement restrictions in play) via the real /message route."""

    def test_code_text_only_reaches_provider_with_no_toolset(self, client, db):
        from unittest.mock import AsyncMock, patch

        resp = client.post("/auth/register", json={
            "email": "exec-mode-code-textonly@test.com", "password": "TestPass123!",
        })
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        # A signup is FREE; this class documents a START user, so set it.
        from src.models.user import User
        db.query(User).filter(User.id == resp.json()["user_id"]).one().plan = "start"
        db.commit()
        session_id = client.post("/sessions", json={}, headers=headers).json()["id"]

        captured: dict = {}

        async def _acompletion(*, model, **kwargs):
            captured["tools"] = kwargs.get("tools")

            class _StreamChunk:
                choices = [type("C", (), {"delta": type("D", (), {"content": "ok"})()})]
                usage = None

            class _Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if getattr(self, "_done", False):
                        raise StopAsyncIteration
                    self._done = True
                    return _StreamChunk()

            return _Stream()

        with patch("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion)):
            resp = client.post(
                f"/sessions/{session_id}/message",
                json={"content": "write a function to add two numbers"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert captured["tools"] is None

    def test_code_with_execution_intent_reaches_provider_with_real_toolset(self, client, db):
        from unittest.mock import AsyncMock, patch

        resp = client.post("/auth/register", json={
            "email": "exec-mode-code-execution@test.com", "password": "TestPass123!",
        })
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        # A signup is FREE; execution intent needs agent mode, which START has.
        from src.models.user import User
        db.query(User).filter(User.id == resp.json()["user_id"]).one().plan = "start"
        db.commit()
        session_id = client.post("/sessions", json={}, headers=headers).json()["id"]

        captured: dict = {}

        async def _acompletion(*, model, **kwargs):
            captured["tools"] = kwargs.get("tools")

            class _Resp:
                choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
                usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
            return _Resp()

        with patch("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion)):
            resp = client.post(
                f"/sessions/{session_id}/message",
                json={"content": "open the repository, modify the files and run the tests"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert captured["tools"] is not None
        assert len(captured["tools"]) > 0
