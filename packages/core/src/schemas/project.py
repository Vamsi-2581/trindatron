from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Project ──────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    instructions: Optional[str] = None


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    instructions: Optional[str] = None


class ProjectFileResponse(BaseModel):
    id: str
    project_id: str
    name: str
    mime_type: str
    size_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str]
    instructions: Optional[str]
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    files: list[ProjectFileResponse] = []

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


# ── CodeWorkspace ─────────────────────────────────────────────────────────────

class CreateCodeWorkspaceRequest(BaseModel):
    project_id: str
    root_path: Optional[str] = None
    language_hint: Optional[str] = None
    run_command: Optional[str] = None
    build_command: Optional[str] = None
    test_command: Optional[str] = None
    git_enabled: bool = False
    sandbox_image: Optional[str] = None


class UpdateCodeWorkspaceRequest(BaseModel):
    root_path: Optional[str] = None
    language_hint: Optional[str] = None
    run_command: Optional[str] = None
    build_command: Optional[str] = None
    test_command: Optional[str] = None
    git_enabled: Optional[bool] = None
    sandbox_image: Optional[str] = None


class CodeWorkspaceResponse(BaseModel):
    id: str
    project_id: str
    root_path: Optional[str]
    language_hint: Optional[str]
    run_command: Optional[str]
    build_command: Optional[str]
    test_command: Optional[str]
    git_enabled: bool
    sandbox_image: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── WorkWorkspace ─────────────────────────────────────────────────────────────

class CreateWorkWorkspaceRequest(BaseModel):
    project_id: str
    requires_approval: bool = True
    approval_policy: str = "smart"


class UpdateWorkWorkspaceRequest(BaseModel):
    requires_approval: Optional[bool] = None
    approval_policy: Optional[str] = None


class WorkWorkspaceResponse(BaseModel):
    id: str
    project_id: str
    requires_approval: bool
    approval_policy: str


# ── Workspace File Browser ─────────────────────────────────────────────────────

class WorkspaceFileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None


class WorkspaceFileListResponse(BaseModel):
    entries: list[WorkspaceFileEntry]
    path: str


class WorkspaceFileContentResponse(BaseModel):
    path: str
    content: str


# ── Work Connectors ────────────────────────────────────────────────────────────
# Connector integration is planned for a future phase (section 4.10).
# The endpoint exists now to satisfy the API surface contract; connectors are
# configured outside this service and never expose secrets here.

class ConnectorResponse(BaseModel):
    id: str
    name: str
    connector_type: str  # e.g. "web_search", "email", "calendar"
    enabled: bool
    # No secret fields (api_key, token, credential) — intentionally absent.

    model_config = {"from_attributes": True}


class ConnectorListResponse(BaseModel):
    workspace_id: str
    connectors: list[ConnectorResponse]
