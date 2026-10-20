from __future__ import annotations

from pydantic import BaseModel, Field


class CodeRunRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    timeout: int = Field(default=30, ge=1, le=120)


class CodeTaskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=8000)
    max_iterations: int = Field(default=10, ge=1, le=20)
