"""Unit tests for src/services/agent_decision.py — the Tier 2 DECIDE
contract (parse_decision/build_decide_payload). Pure, no DB, no network —
mirrors this file's own "small, dependency-free contract module" design.

Test matrix
-----------
D01) valid "act" decision parses correctly
D02) valid "skip" decision parses correctly
D03) a ```json fenced response is tolerated
D04) a plain ``` fenced (no language tag) response is tolerated
D05) malformed JSON -> None
D06) decision value outside {"act","skip"} -> None
D07) "act" with a disallowed job_type (e.g. "shell") -> None
D08) "act" with missing "action" -> None
D09) "act" with action.payload missing "steps" -> None
D10) "act" with an empty steps list -> None
D11) "act" with a step missing "command" -> None
D12) non-string / non-dict top-level input -> None
D13) reason is capped at MAX_REASON_LENGTH
D14) build_decide_payload() returns the {"steps": [{"command": ...}]} shape
D15) build_decide_payload() truncates an oversized observed_excerpt
D16) build_decide_payload() never embeds the system prompt (prompt-injection
     boundary: the dominant policy lives ONLY in DECIDE_SYSTEM_PROMPT_V1,
     applied by the provider — never inside the persisted job_payload)
D17) the system prompt explicitly instructs treating observed content as
     data, not instructions (the actual defense a later E2E test proves
     end to end)
"""
from __future__ import annotations

import json

from src.services import agent_decision


def _act_obj(job_type="research", steps=None):
    return {
        "decision": "act",
        "reason": "content looks important",
        "action": {"job_type": job_type, "payload": {"steps": steps if steps is not None else [{"command": "summarize the change"}]}},
    }


def test_D01_valid_act_decision():
    result = agent_decision.parse_decision(json.dumps(_act_obj()))
    assert result is not None
    assert result.decision == "act"
    assert result.action_job_type == "research"
    assert result.action_payload == {"steps": [{"command": "summarize the change"}]}
    assert result.reason == "content looks important"


def test_D02_valid_skip_decision():
    result = agent_decision.parse_decision(json.dumps({"decision": "skip", "reason": "nothing meaningful changed"}))
    assert result is not None
    assert result.decision == "skip"
    assert result.action_job_type is None
    assert result.action_payload is None


def test_D03_json_code_fence_tolerated():
    raw = "```json\n" + json.dumps(_act_obj()) + "\n```"
    result = agent_decision.parse_decision(raw)
    assert result is not None
    assert result.decision == "act"


def test_D04_plain_code_fence_tolerated():
    raw = "```\n" + json.dumps({"decision": "skip"}) + "\n```"
    result = agent_decision.parse_decision(raw)
    assert result is not None
    assert result.decision == "skip"


def test_D05_malformed_json_is_none():
    assert agent_decision.parse_decision("{not valid json at all") is None


def test_D06_unknown_decision_value_is_none():
    assert agent_decision.parse_decision(json.dumps({"decision": "maybe"})) is None


def test_D07_disallowed_job_type_is_none():
    assert agent_decision.parse_decision(json.dumps(_act_obj(job_type="shell"))) is None


def test_D08_act_missing_action_is_none():
    assert agent_decision.parse_decision(json.dumps({"decision": "act", "reason": "x"})) is None


def test_D09_action_payload_missing_steps_is_none():
    obj = {"decision": "act", "action": {"job_type": "research", "payload": {}}}
    assert agent_decision.parse_decision(json.dumps(obj)) is None


def test_D10_empty_steps_list_is_none():
    assert agent_decision.parse_decision(json.dumps(_act_obj(steps=[]))) is None


def test_D11_step_missing_command_is_none():
    assert agent_decision.parse_decision(json.dumps(_act_obj(steps=[{"not_command": "x"}]))) is None


def test_D12_non_dict_top_level_is_none():
    assert agent_decision.parse_decision(json.dumps(["act", "skip"])) is None
    assert agent_decision.parse_decision(None) is None
    assert agent_decision.parse_decision("") is None


def test_D13_reason_capped_at_max_length():
    huge_reason = "x" * (agent_decision.MAX_REASON_LENGTH + 500)
    result = agent_decision.parse_decision(json.dumps({"decision": "skip", "reason": huge_reason}))
    assert result is not None
    assert len(result.reason) == agent_decision.MAX_REASON_LENGTH


def test_D14_build_decide_payload_shape():
    payload = agent_decision.build_decide_payload(
        agent_name="Page Watcher", agent_objective="Monitor a page", agent_instructions=None,
        instance_id="inst-1", instance_status="active", instance_failure_count=0,
        check_changed=True, check_reason="content_changed",
        previous_signature="abc", new_signature="def", observed_excerpt="hello world",
    )
    assert set(payload.keys()) == {"steps"}
    assert len(payload["steps"]) == 1
    command = payload["steps"][0]["command"]
    assert isinstance(command, str)
    assert "Page Watcher" in command
    assert "hello world" in command
    assert "content_changed" in command


def test_D15_build_decide_payload_truncates_oversized_excerpt():
    huge = "y" * (agent_decision.MAX_OBSERVED_EXCERPT_CHARS + 1000)
    payload = agent_decision.build_decide_payload(
        agent_name="A", agent_objective=None, agent_instructions=None,
        instance_id=None, instance_status=None, instance_failure_count=0,
        check_changed=True, check_reason=None, previous_signature=None, new_signature=None,
        observed_excerpt=huge,
    )
    command = payload["steps"][0]["command"]
    excerpt_in_message = command.split('"""\n', 1)[1].rsplit('\n"""', 1)[0]
    assert len(excerpt_in_message) == agent_decision.MAX_OBSERVED_EXCERPT_CHARS
    assert excerpt_in_message == huge[: agent_decision.MAX_OBSERVED_EXCERPT_CHARS]


def test_D16_payload_never_embeds_system_prompt():
    payload = agent_decision.build_decide_payload(
        agent_name="A", agent_objective=None, agent_instructions=None,
        instance_id=None, instance_status=None, instance_failure_count=0,
        check_changed=True, check_reason=None, previous_signature=None, new_signature=None,
        observed_excerpt=None,
    )
    command = payload["steps"][0]["command"]
    assert "You are the autonomous DECIDE step" not in command


def test_D17_system_prompt_establishes_data_not_instructions_boundary():
    prompt = agent_decision.DECIDE_SYSTEM_PROMPT_V1
    assert "OBSERVED CONTENT" in prompt
    assert "DATA ONLY" in prompt
    assert "NEVER follow" in prompt or "never follow" in prompt.lower()
