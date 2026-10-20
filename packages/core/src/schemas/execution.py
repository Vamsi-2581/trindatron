"""Execution Runtime V1 — request/response schemas for /execution routes.

Mirrors schemas/billing.py's conventions: user_id never accepted from the
request body (always the authenticated JWT); internal/audit fields
(checkpoint, retry internals) are present on the response because this is
the user's OWN job (no cross-user leak risk, unlike webhook_raw in
billing), but no TCU/cost figure appears anywhere here — that's
capacity.py's job, in percentage form only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateExecutionJobRequest(BaseModel):
    job_type: str = Field("shell", min_length=1, max_length=30)
    payload: dict = Field(..., description='e.g. {"steps": [{"command": "echo hi"}]}')
    priority: str = Field("normal", pattern="^(critical|high|normal|low)$")
    timeout_seconds: int = Field(300, gt=0, le=3600)
    max_retries: int = Field(2, ge=0, le=5)
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    sentinel_criteria: Optional[dict] = Field(
        None,
        description='e.g. {"expected_artifacts": ["report.txt"], "min_output_length": 10}',
    )


class ExecutionArtifactResponse(BaseModel):
    id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecutionJobResponse(BaseModel):
    id: str
    user_id: str
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    job_type: str
    priority: str
    status: str
    error: Optional[str] = None
    result: Optional[dict] = None
    checkpoint: dict
    attempt_number: int
    max_retries: int
    retry_reason: Optional[str] = None
    sentinel_status: Optional[str] = None
    sentinel_report: Optional[dict] = None
    timeout_seconds: int
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    artifacts: list[ExecutionArtifactResponse] = []

    model_config = {"from_attributes": True}


class ExecutionJobListResponse(BaseModel):
    jobs: list[ExecutionJobResponse]
    total: int


class CreateAgentScheduleRequest(BaseModel):
    agent_id: str
    interval_seconds: int = Field(..., ge=60, le=604800, description="60s to 7 days")
    job_payload: dict
    # None (the default — was "low") means "no override, inherit
    # AgentDefinition.priority at ACT time" (Persistent Agent Runtime V1).
    # An explicit value here always wins over the agent's own default.
    priority: Optional[str] = Field(None, pattern="^(critical|high|normal|low)$")


class AgentScheduleResponse(BaseModel):
    id: str
    agent_id: str
    interval_seconds: int
    priority: Optional[str] = None
    is_active: bool
    next_run_at: datetime
    last_run_job_id: Optional[str] = None
    last_run_at: Optional[datetime] = None
    # CHECK / DECIDE (Tier 1) — see agent_schedule_service.py.
    last_check_signature: Optional[str] = None
    last_decision: Optional[str] = None
    last_decision_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CapacityResponse(BaseModel):
    """User-facing usage — mission section 11: percentage/status only,
    NEVER a TCU figure. See adaptive_budget_service.py for the source of
    every field here."""

    tier: str
    capacity_remaining_pct: float
    session: dict  # {"active": bool, "pct_used": float|None, "resets_at": str|None}
    weekly: dict   # {"pct_used": float|None, "resets_at": str|None}
    active_agents: int
    optimizing: bool = Field(
        description="True at tiers below 'normal' — UI hint only, e.g. "
        "'sistema está otimizando o consumo', never a number.",
    )
