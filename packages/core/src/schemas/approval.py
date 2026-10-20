from __future__ import annotations

from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    """Generic shape for resolving an approval gate — shared by TRINITY
    WORK (schemas/work.py's WorkApprovalRequest, kept as a thin subclass
    for backward compatibility) and TRINITY AGENT."""
    action_id: str = Field(..., min_length=1, max_length=100)
