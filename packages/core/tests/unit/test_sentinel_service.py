"""Unit tests for sentinel_service — Execution Runtime V1 verification layer.

Test matrix
-----------
S01) No criteria configured -> from_dict returns None (skip verification)
S02) require_success default True — failed last step fails Sentinel
S03) Passing case — success + all expected artifacts present
S04) Missing expected artifact fails Sentinel with a specific reason
S05) Path-traversal attempt in expected_artifacts is rejected, not followed
S06) min_output_length enforced
S07) forbidden_patterns enforced (case-insensitive)
S08) Multiple simultaneous failures all reported (not just the first)
"""
from __future__ import annotations

from src.services.sentinel_service import SentinelCriteria, verify


def test_S01_no_criteria_returns_none():
    assert SentinelCriteria.from_dict(None) is None
    assert SentinelCriteria.from_dict({}) is None


def test_S02_require_success_default_fails_on_unsuccessful_step(tmp_path):
    criteria = SentinelCriteria()  # defaults: require_success=True
    report = verify(criteria=criteria, last_step_success=False, combined_output="", workspace=tmp_path)
    assert report.passed is False
    assert any("did not succeed" in r for r in report.reasons)


def test_S03_passing_case_with_artifacts(tmp_path):
    (tmp_path / "report.txt").write_text("done", encoding="utf-8")
    criteria = SentinelCriteria(expected_artifacts=["report.txt"], min_output_length=2)
    report = verify(criteria=criteria, last_step_success=True, combined_output="ok output", workspace=tmp_path)
    assert report.passed is True
    assert report.reasons == []


def test_S04_missing_artifact_fails_with_reason(tmp_path):
    criteria = SentinelCriteria(expected_artifacts=["missing.txt"])
    report = verify(criteria=criteria, last_step_success=True, combined_output="x", workspace=tmp_path)
    assert report.passed is False
    assert any("missing.txt" in r for r in report.reasons)


def test_S05_path_traversal_in_expected_artifact_rejected(tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        criteria = SentinelCriteria(expected_artifacts=["../outside_secret.txt"])
        report = verify(criteria=criteria, last_step_success=True, combined_output="x", workspace=tmp_path)
        assert report.passed is False
        assert any("escapes workspace" in r for r in report.reasons)
    finally:
        outside.unlink(missing_ok=True)


def test_S06_min_output_length_enforced(tmp_path):
    criteria = SentinelCriteria(min_output_length=100)
    report = verify(criteria=criteria, last_step_success=True, combined_output="short", workspace=tmp_path)
    assert report.passed is False
    assert any("too short" in r for r in report.reasons)


def test_S07_forbidden_pattern_case_insensitive(tmp_path):
    criteria = SentinelCriteria(forbidden_patterns=["traceback"])
    report = verify(
        criteria=criteria, last_step_success=True,
        combined_output="Everything fine\nTRACEBACK (most recent call last)", workspace=tmp_path,
    )
    assert report.passed is False
    assert any("forbidden pattern" in r for r in report.reasons)


def test_S08_multiple_failures_all_reported(tmp_path):
    criteria = SentinelCriteria(
        expected_artifacts=["missing.txt"], min_output_length=1000, forbidden_patterns=["error"],
    )
    report = verify(criteria=criteria, last_step_success=False, combined_output="error occurred", workspace=tmp_path)
    assert report.passed is False
    assert len(report.reasons) == 4  # success + artifact + length + forbidden
