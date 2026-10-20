from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateSkillRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    instructions: Optional[str] = None
    tool_ids: list[str] = Field(default_factory=list)
    icon_emoji: Optional[str] = Field(None, max_length=10)


class UpdateSkillRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    instructions: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    icon_emoji: Optional[str] = Field(None, max_length=10)


class SkillResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    tool_ids: list[str] = []
    icon_emoji: Optional[str] = None
    is_builtin: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]
    total: int
