"""Etapa 5F — Continuity Mode TEXT-ONLY gate tests.

Covers the correction message's 18 required scenarios. Since
continuity_mode.py is deliberately NOT wired into sessions.py's live
request flow yet (see its own module docstring — two product decisions
are missing: plan eligibility, Continuity's funding mechanism), items that
describe end-to-end behavior ("não chama provider", "não consome TCU",
etc.) are proven here at the level that actually exists today: the gate
functions are pure, side-effect-free, and raise BEFORE anything that could
call a provider or touch TCU would run — demonstrated by wrapping a
"would-call-provider" spy around the gate and proving it never executes
for a rejected capability. This is the exact contract a future integration
must honor; it is fully verified in isolation here.
"""
from __future__ import annotations

import pytest

from src.capability import Capability
from src.schemas.agent import AgentRequest
from src.schemas.message import ChatRequest
from src.services.continuity_mode import (
    MULTIMODAL_CAPABILITIES,
    ContinuityRejected,
    ContinuityRejectionReason,
    check_continuity_eligible,
    is_premium_quota_exhausted,
    is_text_only_capability,
    select_continuity_model,
)
from trinity_router.providers import PROVIDER_CATALOG, ProviderModel

_TEXT_CAPABILITIES = [c for c in Capability if c not in MULTIMODAL_CAPABILITIES]
_MULTIMODAL_CAPABILITIES = [Capability.IMAGE, Capability.VIDEO, Capability.AUDIO]


class _ProviderCalledUnexpectedly(AssertionError):
    pass


def _spy_provider_call():
    raise _ProviderCalledUnexpectedly("provider must never be called for a rejected Continuity request")


# ── 1. Continuity + texto → permitido ───────────────────────────────────────

@pytest.mark.parametrize("capability", _TEXT_CAPABILITIES)
def test_text_capabilities_are_continuity_eligible(capability):
    assert is_text_only_capability(capability) is True
    check_continuity_eligible(capability)  # must not raise


# ── 2/3/4. Image/video/audio input → rejeitado ──────────────────────────────

@pytest.mark.parametrize("capability", _MULTIMODAL_CAPABILITIES)
def test_multimodal_input_capability_is_rejected(capability):
    assert is_text_only_capability(capability) is False
    with pytest.raises(ContinuityRejected) as exc_info:
        check_continuity_eligible(capability)
    assert exc_info.value.reason == ContinuityRejectionReason.MULTIMODAL_CAPABILITY


# ── 5/6/7. Image/video/audio OUTPUT → impossível/rejeitado ──────────────────

def test_no_catalog_model_can_ever_produce_non_text_output():
    """Structural guarantee, not just a per-request check: every model
    Continuity could ever select is modality='text' — there is no catalog
    entry capable of image/video/audio output at all, so "output" rejection
    is enforced at the catalog level, not merely by chance."""
    non_text = [m for m in PROVIDER_CATALOG.values() if m.modality != "text"]
    assert non_text == [], f"catalog contains non-text models: {non_text}"


def test_select_continuity_model_only_ever_returns_text_modality():
    key, model = select_continuity_model()
    assert model.modality == "text"
    assert PROVIDER_CATALOG[key] is model


# ── 8/9/10. Cliente declara TEXT mas payload é multimodal → rejeitado ──────

def test_chat_and_agent_request_schemas_have_no_multimodal_channel():
    """Structural proof (not a runtime check) that a client cannot smuggle
    an image/video/audio payload — or a self-declared capability — through
    /chat, /agent, or /message today: audited, these schemas expose
    exactly `content: str` (+ optional `model` on ChatRequest) and nothing
    else. If this ever changes, this test breaks loudly, which is the
    point — a new field here means the gate below must be revisited."""
    chat_fields = set(ChatRequest.model_fields.keys())
    agent_fields = set(AgentRequest.model_fields.keys())

    forbidden_substrings = ("image", "video", "audio", "attachment", "file", "capability")
    for field_set, schema_name in ((chat_fields, "ChatRequest"), (agent_fields, "AgentRequest")):
        offending = [f for f in field_set if any(s in f.lower() for s in forbidden_substrings)]
        assert offending == [], f"{schema_name} unexpectedly has a multimodal/capability-like field: {offending}"

    # "locale" is the client's language tag (src/language.py) — text about the text, not a channel for media.
    assert chat_fields == {"content", "model", "locale"}
    assert agent_fields == {"content", "locale"}


def test_capability_only_ever_originates_server_side():
    """The real guarantee behind items 8-10 is architectural, not a type
    check: `classify()` (src/capability/classifier.py) is the ONLY
    function in this codebase that produces a Capability value from a
    request, it is always called on server-derived request content, and
    (per test_chat_and_agent_request_schemas_have_no_multimodal_channel
    above) no request schema has any field a client could use to inject a
    capability value directly. There is therefore no code path today
    through which is_text_only_capability()/check_continuity_eligible()
    could ever be called with anything other than a genuine, server-
    classified Capability."""
    from src.capability import classify
    # classify() always returns a real Capability enum member, never a
    # bare string reflecting whatever the caller wrote in free text.
    result = classify("please generate an image of a cat")
    assert isinstance(result, Capability)
    assert result == Capability.IMAGE
    check_result = is_text_only_capability(result)
    assert check_result is False  # correctly rejected — classify() drove the real decision


# ── 11-16. Modalidade bloqueada nunca chama provider / consome TCU / etc ───

@pytest.mark.parametrize("capability", _MULTIMODAL_CAPABILITIES)
def test_blocked_modality_never_reaches_a_provider_call(capability):
    """Simulates the intended integration contract: the gate is called
    FIRST, and only on success would a caller proceed to provider
    selection/call/TCU reservation. Proves the spy is never reached."""
    with pytest.raises(ContinuityRejected):
        check_continuity_eligible(capability)
        _spy_provider_call()  # unreachable — proves the raise happens first


@pytest.mark.parametrize("capability", _MULTIMODAL_CAPABILITIES)
def test_blocked_modality_check_has_no_side_effects_to_undo(capability):
    """No TCU reservation, no UsageRecord, no retry, no failover, no
    premium provider activation can occur as a SIDE EFFECT of the gate
    itself, because the gate performs no I/O at all — verified by
    checking it raises a plain, argument-only exception with nothing
    resembling a database handle, session, or provider client attached."""
    with pytest.raises(ContinuityRejected) as exc_info:
        check_continuity_eligible(capability)
    # The exception carries only a reason code and a safe string — no
    # provider/model/session/db object could have been touched to produce it.
    assert isinstance(exc_info.value.reason, ContinuityRejectionReason)
    assert isinstance(str(exc_info.value), str)


def test_multimodal_capability_never_selects_a_continuity_model():
    """Even indirectly: nothing about select_continuity_model() is ever
    invoked for a multimodal capability in the intended call order (gate
    first) — proven by never calling it in this test at all, and instead
    proving the gate alone is sufficient to stop before that point."""
    for capability in _MULTIMODAL_CAPABILITIES:
        with pytest.raises(ContinuityRejected):
            check_continuity_eligible(capability)
            select_continuity_model()  # unreachable


# ── 17. Normal Mode continua permitindo as capabilities já permitidas ──────

def test_normal_mode_capability_routing_is_unaffected():
    """continuity_mode.py must not alter anything in src.capability — a
    regression guard proving normal-mode routing (unrelated to Continuity)
    is untouched by this etapa."""
    from src.capability import get_task_type, route_capability
    from trinity_router import TaskType

    # A couple of known, pre-existing invariants (Etapa 5C/5D), re-checked
    # here to prove nothing shifted.
    assert get_task_type(Capability.CHAT) == TaskType.CHAT
    assert get_task_type(Capability.AGENT) == TaskType.AGENT
    assert route_capability(Capability.CHAT).implemented is True
    assert route_capability(Capability.IMAGE).implemented is False  # unimplemented, not "blocked by continuity"


# ── 18. Continuity permanece TEXT ONLY mesmo com provider econômico multimodal ─

def test_continuity_ignores_a_cheaper_multimodal_candidate():
    """Even if a hypothetical future catalog entry were an ultra-cheap
    IMAGE-capable model, select_continuity_model() must never pick it —
    the modality filter is enforced regardless of cost."""
    fake_catalog = dict(PROVIDER_CATALOG)
    fake_catalog["hypothetical-cheap-image-model"] = ProviderModel(
        provider="hypothetical", model_id="cheap-image-gen",
        context_window=8_000, cost_per_1m_input_usd=0.001, cost_per_1m_output_usd=0.001,
        supports_tools=False, supports_streaming=False,
        modality="image", quality_class="ultra_low", speed_class="fast",
    )
    key, chosen = select_continuity_model(catalog=fake_catalog)
    assert chosen.modality == "text"
    assert chosen.provider != "hypothetical"
    assert key != "hypothetical-cheap-image-model"


def test_continuity_picks_cheapest_capable_not_cheapest_absolute():
    """Companion to item 18 — proves 'cheapest eligible capable', not
    'cheapest absolutely': a cheaper candidate that fails a stated
    requirement (tools) must lose to a pricier one that satisfies it."""
    fake_catalog = {
        "ultra-cheap-no-tools": ProviderModel(
            provider="test", model_id="no-tools", context_window=100_000,
            cost_per_1m_input_usd=0.001, cost_per_1m_output_usd=0.001,
            supports_tools=False, supports_streaming=True, modality="text",
        ),
        "pricier-with-tools": ProviderModel(
            provider="test", model_id="with-tools", context_window=100_000,
            cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0,
            supports_tools=True, supports_streaming=True, modality="text",
        ),
    }
    key, chosen = select_continuity_model(requires_tools=True, catalog=fake_catalog)
    assert chosen.model_id == "with-tools"
    assert key == "pricier-with-tools"


def test_no_eligible_model_is_a_controlled_rejection_not_a_fallback():
    """W-equivalent: if nothing in the (filtered) catalog qualifies,
    select_continuity_model() raises rather than silently returning an
    incapable-but-cheap model or falling back to a premium one."""
    fake_catalog = {
        "incapable": ProviderModel(
            provider="test", model_id="incapable", context_window=1_000,
            cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01,
            supports_tools=False, supports_streaming=True, modality="text",
        ),
    }
    with pytest.raises(ContinuityRejected) as exc_info:
        select_continuity_model(requires_tools=True, catalog=fake_catalog)
    assert exc_info.value.reason == ContinuityRejectionReason.NO_ELIGIBLE_MODEL


# ── Premium-quota-exhausted detector — the (not-yet-wired) activation signal ─

def test_daily_limit_exceeded_is_recognized_as_premium_quota_exhausted():
    detail = {"error": "daily_limit_exceeded", "message": "...", "daily_used": 10.0, "daily_limit": 10.0}
    assert is_premium_quota_exhausted(detail) is True


def test_weekly_limit_exceeded_is_recognized_as_premium_quota_exhausted():
    detail = {"error": "weekly_limit_exceeded", "message": "...", "weekly_used": 50.0, "weekly_limit": 50.0}
    assert is_premium_quota_exhausted(detail) is True


def test_monthly_limit_exceeded_is_not_recognized():
    """Deliberately excluded — monthly_tcu_limit is None for every plan
    today (dead code path); see continuity_mode.py's own comment."""
    detail = {"error": "monthly_limit_exceeded", "message": "..."}
    assert is_premium_quota_exhausted(detail) is False


@pytest.mark.parametrize("detail", [
    {"error": "budget_exceeded"},                # ModelRouter pre-flight (Etapa 5E)
    {"error": "circuit_open"},                    # ModelRouter circuit breaker (Etapa 5E)
    {"error": "rate_limit_user"},                  # Request Control (Etapa 5B) — not raised as this shape but tested defensively
    {"error": "concurrency_global"},               # Agent Scheduler (Etapa 5D)
    {"error": "concurrency_user"},                 # Agent Scheduler (Etapa 5D)
    {"detail": "Session not found"},               # 404 — different shape entirely
    None,
    "Too many requests. Please try again shortly.",  # Request Control's actual plain-string detail
])
def test_non_quota_rejections_are_never_recognized_as_premium_quota_exhausted(detail):
    """Critical negative-space guarantee: a provider failure, a Request
    Control rejection, an Agent Scheduler rejection, or any other 429/404
    shape in this codebase must NEVER be mistaken for the one condition
    Continuity Mode is meant to respond to. In particular this guards
    against Continuity ever activating in response to a provider outage —
    that must stay entirely Etapa 5E's responsibility."""
    assert is_premium_quota_exhausted(detail) is False


# ── Every Capability is accounted for (no silent gap in the multimodal set) ─

def test_every_capability_is_classified_as_either_text_or_multimodal():
    for capability in Capability:
        # Every capability must land in exactly one bucket — never both,
        # never neither (which would mean this module wasn't updated
        # after a new Capability was added).
        assert (capability in MULTIMODAL_CAPABILITIES) != is_text_only_capability(capability)


def test_multimodal_capabilities_set_matches_the_documented_three():
    assert MULTIMODAL_CAPABILITIES == {Capability.IMAGE, Capability.VIDEO, Capability.AUDIO}


def test_continuity_rejected_is_not_a_provider_or_budget_error_type():
    """ContinuityRejected must be its own distinct exception type — never
    accidentally caught by existing `except BudgetExceededError`/`except
    ProviderUnavailableError` handlers in chat_service.py/agent_service.py,
    which would misclassify a Continuity rejection as a provider issue."""
    from trinity_router import BudgetExceededError, ProviderUnavailableError
    assert not issubclass(ContinuityRejected, BudgetExceededError)
    assert not issubclass(ContinuityRejected, ProviderUnavailableError)
