from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CreateAgentDefinitionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    avatar_emoji: Optional[str] = Field(None, max_length=10)
    system_instructions: Optional[str] = None
    skill_ids: list[str] = Field(default_factory=list)
    model_preference: Literal["power", "balanced", "fast"] = "balanced"


class UpdateAgentDefinitionRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    avatar_emoji: Optional[str] = Field(None, max_length=10)
    system_instructions: Optional[str] = None
    skill_ids: Optional[list[str]] = None
    model_preference: Optional[Literal["power", "balanced", "fast"]] = None


class AgentDefinitionResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str]
    avatar_emoji: Optional[str]
    system_instructions: Optional[str]
    skill_ids: list[str]
    model_preference: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentDefinitionListResponse(BaseModel):
    agents: list[AgentDefinitionResponse]
    total: int


class ProjectAgentResponse(BaseModel):
    project_id: str
    agent_definition_id: str
    created_at: datetime

    model_config = {"from_attributes": True}
