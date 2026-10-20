from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.approval import ApprovalRequest


class WorkTaskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=8000)
    max_iterations: int = Field(default=8, ge=1, le=20)


class WorkApprovalRequest(ApprovalRequest):
    """Thin subclass — see schemas/approval.py's ApprovalRequest for the
    shared shape (kept as its own name for backward compatibility with
    existing WORK route/client code)."""
