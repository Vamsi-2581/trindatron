from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    # No model field: which model answered is internal and never returned
    # (src/prompting/identity_guard.py). Message.model_used stays in the
    # database for operators.
    # The creation this reply started, followed with GET /media/jobs/{id}.
    media_job_id: Optional[str] = None
    # The execution job this reply started (a presentation): its progress and its file,
    # followed with GET /execution/jobs/{id} and GET /execution/jobs/{id}/artifacts.
    execution_job_id: Optional[str] = None
    # The reply stopped at the plan's output limit; clients say so and offer to continue.
    truncated: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("truncated", mode="before")
    @classmethod
    def _unset_is_false(cls, value):
        return bool(value)


class ChatRequest(BaseModel):
    content: str
    model: Optional[str] = None
    # The language the client is running in ("pt", "en", "es", or a tag like "pt-BR"). It is what the person's
    # device is set to, and it decides the answer's language whenever they did not choose one in Settings — see
    # src/language.py. Absent (an older client) keeps the previous behaviour: the text decides.
    locale: Optional[str] = None


class SessionDetailResponse(BaseModel):
    id: str
    title: str
    messages: list[MessageResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
