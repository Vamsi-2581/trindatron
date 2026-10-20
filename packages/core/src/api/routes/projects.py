from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.models.session import Session
from src.models.user import User
from src.schemas.project import (
    CreateProjectRequest,
    UpdateProjectRequest,
    ProjectResponse,
    ProjectListResponse,
    ProjectFileResponse,
    CodeWorkspaceResponse,
    WorkWorkspaceResponse,
)
from src.schemas.session import SessionListResponse, SessionResponse
from src.services.project_service import (
    create_project,
    get_projects,
    get_project,
    update_project,
    archive_project,
    add_project_file,
    get_project_file,
    delete_project_file,
    get_code_workspace_by_project,
    get_work_workspace_by_project,
)

router = APIRouter(prefix="/projects", tags=["projects"])

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


def _get_project_or_404(db: DBSession, project_id: str, user_id: str):
    project = get_project(db, project_id=project_id, user_id=user_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


# ── Projects CRUD ─────────────────────────────────────────────────────────────

@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: CreateProjectRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = create_project(
        db,
        user_id=current_user.id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
    )
    return project


@router.get("", response_model=ProjectListResponse)
def list_projects(
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    projects = get_projects(db, user_id=current_user.id)
    return ProjectListResponse(projects=projects, total=len(projects))


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project_detail(
    project_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_project_or_404(db, project_id, current_user.id)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project_detail(
    project_id: str,
    body: UpdateProjectRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_project_or_404(db, project_id, current_user.id)
    updates = body.model_dump(exclude_unset=True)
    return update_project(db, project, **updates)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive(
    project_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_project_or_404(db, project_id, current_user.id)
    archive_project(db, project)


# ── Code Workspace ────────────────────────────────────────────────────────────

@router.get("/{project_id}/workspace/code", response_model=CodeWorkspaceResponse)
def get_project_code_workspace(
    project_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    ws = get_code_workspace_by_project(db, project_id)
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No CODE workspace for this project")
    return ws


# ── Work Workspace ─────────────────────────────────────────────────────────────

@router.get("/{project_id}/workspace/work", response_model=WorkWorkspaceResponse)
def get_project_work_workspace(
    project_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    ws = get_work_workspace_by_project(db, project_id)
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No WORK workspace for this project")
    return ws


# ── Project Sessions ──────────────────────────────────────────────────────────

@router.get("/{project_id}/sessions", response_model=SessionListResponse)
def list_project_sessions(
    project_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    sessions = (
        db.query(Session)
        .filter(Session.project_id == project_id, Session.user_id == current_user.id)
        .order_by(Session.updated_at.desc())
        .all()
    )
    return SessionListResponse(sessions=sessions, total=len(sessions))


# ── Project Files ─────────────────────────────────────────────────────────────

@router.post("/{project_id}/files", response_model=ProjectFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    file: UploadFile = File(...),
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_project_or_404(db, project_id, current_user.id)
    content = await file.read(_MAX_FILE_BYTES + 1)
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds 10 MB limit",
        )
    mime = file.content_type or "application/octet-stream"
    try:
        pf = add_project_file(db, project_id=project.id, name=file.filename or "file", mime_type=mime, content=content)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc))
    return pf


@router.get("/{project_id}/files/{file_id}/content")
def file_content(
    project_id: str,
    file_id: str,
    download: bool = False,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview (inline) or download a project file. Owner only; 404 otherwise."""
    _get_project_or_404(db, project_id, current_user.id)
    pf = get_project_file(db, file_id=file_id, project_id=project_id)
    if not pf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    disposition = "attachment" if download else "inline"
    safe_name = pf.name.replace('"', "'")
    return Response(
        content=pf.content,
        media_type=pf.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{project_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    project_id: str,
    file_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    pf = get_project_file(db, file_id=file_id, project_id=project_id)
    if not pf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    delete_project_file(db, pf)
