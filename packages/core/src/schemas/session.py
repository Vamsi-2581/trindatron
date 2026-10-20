from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class CreateSessionRequest(BaseModel):
    title: str = "New conversation"
    model: Optional[str] = None
    project_id: Optional[str] = None
    agent_definition_id: Optional[str] = None
    is_temporary: bool = False


class SessionResponse(BaseModel):
    id: str
    title: str
    project_id: Optional[str] = None
    agent_definition_id: Optional[str] = None
    is_temporary: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
