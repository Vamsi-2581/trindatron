"""Unit tests for agent_check_source.py — the CHECK step of Persistent
Agent Runtime V1, Tier 1.

Test matrix
-----------
C01) First check on a schedule with no previous signature -> changed=True
C02) Same payload, same previous signature -> changed=False
C03) Different payload -> different signature -> changed=True
C04) Key order in job_payload does not affect the signature (stable/sorted)
C05) Signature is deterministic (same payload -> same signature, always)
C06) default_check_source() returns a PayloadSignatureCheckSource
"""
from __future__ import annotations

from src.models.agent_schedule import AgentSchedule
from src.services.agent_check_source import (
    CheckResult,
    PayloadSignatureCheckSource,
    default_check_source,
)


def _schedule(job_payload: dict, last_check_signature: str | None = None) -> AgentSchedule:
    # Not persisted — PayloadSignatureCheckSource only reads attributes,
    # never touches the DB (see module docstring: CHECK never bills).
    return AgentSchedule(job_payload=job_payload, last_check_signature=last_check_signature)


async def test_C01_first_check_no_previous_signature_is_changed():
    source = PayloadSignatureCheckSource()
    result = await source.check(_schedule({"steps": [{"command": "x"}]}, last_check_signature=None))

    assert isinstance(result, CheckResult)
    assert result.changed is True
    assert result.signature is not None
    assert result.reason == "no_previous_signature"


async def test_C02_same_signature_is_unchanged():
    source = PayloadSignatureCheckSource()
    payload = {"steps": [{"command": "x"}]}
    signature = source._signature(payload)

    result = await source.check(_schedule(payload, last_check_signature=signature))

    assert result.changed is False
    assert result.signature == signature
    assert result.reason == "payload_unchanged"


async def test_C03_different_payload_is_changed():
    source = PayloadSignatureCheckSource()
    old_signature = source._signature({"steps": [{"command": "old"}]})

    result = await source.check(_schedule({"steps": [{"command": "new"}]}, last_check_signature=old_signature))

    assert result.changed is True
    assert result.signature != old_signature
    assert result.reason == "payload_changed"


def test_C04_key_order_does_not_affect_signature():
    source = PayloadSignatureCheckSource()
    sig_a = source._signature({"a": 1, "b": 2})
    sig_b = source._signature({"b": 2, "a": 1})

    assert sig_a == sig_b


def test_C05_signature_is_deterministic():
    source = PayloadSignatureCheckSource()
    payload = {"steps": [{"command": "check disk"}], "meta": {"n": 3}}

    assert source._signature(payload) == source._signature(payload)


def test_C06_default_check_source_is_payload_signature_source():
    assert isinstance(default_check_source(), PayloadSignatureCheckSource)
