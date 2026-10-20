"""
Sentinel — verification layer for Execution Runtime V1.

Confirmed via repository-wide search (no match for "sentinel", case-
insensitive, anywhere in packages/core/src before this file) that no prior
verification layer exists — this is new, not a duplicate.

Answers exactly the question the mission spec poses: "process terminou" is
NOT automatically "tarefa foi concluída". A job whose shell command exits 0
but never produced the file it promised, or whose output contains an error
string despite exit 0, must fail Sentinel — execution_service.py treats a
FAIL as informing (and bounding, via RetryPolicy) a retry, never as silent
success.

Checks implemented (real, filesystem-backed — not simulated)
----------------------------------------------------------------
  exit status         — StepOutcome.success from the last executed step
  expected_artifacts  — each listed filename must exist under the job's
                         workspace directory (real Path.exists() check)
  min_output_length   — len(combined step output) >= threshold
  forbidden_patterns  — none of these substrings may appear in the output
                         (case-insensitive) — catches "exit 0 but printed
                         'Traceback' / 'ERROR:'" style silent failures

Not implemented: structural/semantic validation of artifact CONTENTS
(e.g. "is this JSON valid", "does this PDF have N pages") — genuinely
job-type-specific, deliberately out of scope for a criteria shape generic
enough to serve any job_type. See docs/EXECUTION_RUNTIME.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SentinelCriteria:
    """Job-supplied verification criteria — see ExecutionJob.sentinel_criteria.
    Every field is optional; a criteria object with everything at its
    default (None steps ignored) still checks require_success, since a
    job that asked for verification at all should at minimum expect its
    last step to have succeeded."""

    require_success: bool = True
    expected_artifacts: list[str] = field(default_factory=list)
    min_output_length: int | None = None
    forbidden_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None) -> "SentinelCriteria | None":
        if not data:
            return None
        return cls(
            require_success=bool(data.get("require_success", True)),
            expected_artifacts=list(data.get("expected_artifacts") or []),
            min_output_length=data.get("min_output_length"),
            forbidden_patterns=list(data.get("forbidden_patterns") or []),
        )


@dataclass
class SentinelReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons}


def verify(
    *,
    criteria: SentinelCriteria,
    last_step_success: bool,
    combined_output: str,
    workspace: Path,
) -> SentinelReport:
    """Pure, synchronous verification against real filesystem state under
    `workspace`. No network, no LLM call — deliberately cheap so calling it
    is never itself a budget concern (mission section 10's "API check ->
    sem LLM" principle applied to verification too)."""
    reasons: list[str] = []

    if criteria.require_success and not last_step_success:
        reasons.append("last step did not succeed")

    for filename in criteria.expected_artifacts:
        candidate = (workspace / filename)
        try:
            resolved = candidate.resolve()
            # Same invariant capability/sandbox_utils.py's _sandboxed()
            # enforces elsewhere: never trust a criteria-supplied filename
            # to point outside the job's own workspace.
            if not resolved.is_relative_to(workspace.resolve()):
                reasons.append(f"expected artifact path escapes workspace: {filename!r}")
                continue
        except (OSError, ValueError):
            reasons.append(f"expected artifact path invalid: {filename!r}")
            continue
        if not resolved.exists():
            reasons.append(f"expected artifact missing: {filename!r}")

    if criteria.min_output_length is not None and len(combined_output) < criteria.min_output_length:
        reasons.append(
            f"output too short: {len(combined_output)} < {criteria.min_output_length}"
        )

    lowered = combined_output.lower()
    for pattern in criteria.forbidden_patterns:
        if pattern.lower() in lowered:
            reasons.append(f"forbidden pattern found in output: {pattern!r}")

    return SentinelReport(passed=not reasons, reasons=reasons)
