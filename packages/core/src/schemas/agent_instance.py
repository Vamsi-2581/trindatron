"""Persistent Agent Instance V1 — request/response schemas for
/agent-instances routes.

Never returned: secrets, reservation_id, TCU, tokens, cost — none of
those are even columns on AgentInstance (see models/agent_instance.py's
own docstring for why), so there is nothing to accidentally leak here,
same as ExecutionJobResponse's own guarantee in schemas/execution.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateAgentInstanceRequest(BaseModel):
    agent_definition_id: str
    name: str = Field(..., min_length=1, max_length=100)
    project_id: Optional[str] = None


class UpdateAgentInstanceRequest(BaseModel):
    # Deliberately NOT: status (goes through /pause, /resume — see
    # agent_instance_service.update_agent_instance()'s own docstring),
    # user_id, agent_definition_id, or anything execution/billing-related.
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    project_id: Optional[str] = None
    # Distinguishes "field omitted" (leave project_id unchanged) from
    # "field explicitly sent as null" (clear it) — Pydantic's
    # model_fields_set is checked by the route, not this schema itself.


class AgentInstanceResponse(BaseModel):
    id: str
    user_id: str
    agent_definition_id: str
    project_id: Optional[str] = None
    name: str
    status: str
    health: str  # derived — see agent_instance_service.derive_health()
    failure_count: int
    last_error: Optional[str] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None  # derived (MIN across its schedules) — see route
    last_execution_job_id: Optional[str] = None
    current_execution_job_status: Optional[str] = None  # derived — see route
    # Tier 2 (LLM DECIDE) observability — derived from whichever active
    # schedule most recently decided (see route). One of
    # AGENT_SCHEDULE_DECISIONS (models/agent_schedule.py), e.g. "acted",
    # "decide_pending", "decide_skip" — or None if this instance has no
    # schedule that has ever fired a decision yet. Still no TCU/tokens/
    # cost/reservation_id/provider detail — same guarantee as every other
    # field here.
    last_decision: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentInstanceListResponse(BaseModel):
    instances: list[AgentInstanceResponse]
    total: int
