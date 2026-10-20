from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.entitlements import get_plan_entitlement
from src.models.code_workspace import CodeWorkspace
from src.models.work_workspace import WorkWorkspace
from src.models.user import User
from src.schemas.code import CodeRunRequest, CodeTaskRequest
from src.schemas.work import WorkApprovalRequest, WorkTaskRequest
from src.schemas.project import (
    CreateCodeWorkspaceRequest,
    UpdateCodeWorkspaceRequest,
    CodeWorkspaceResponse,
    CreateWorkWorkspaceRequest,
    UpdateWorkWorkspaceRequest,
    WorkWorkspaceResponse,
    WorkspaceFileListResponse,
    WorkspaceFileContentResponse,
    ConnectorListResponse,
)
from src.services.code_service import stream_code_execute, stream_code_task
from src.services.work_service import resolve_gate, stream_work_task
from src.services.entitlement_service import check_and_reserve
from src.services.project_service import (
    ArbitraryWorkspaceRootDisabled,
    get_project,
    create_code_workspace,
    get_code_workspace,
    update_code_workspace,
    create_work_workspace,
    get_work_workspace,
    update_work_workspace,
    list_workspace_files,
    read_workspace_file,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _get_project_or_404(db: DBSession, project_id: str, user_id: str):
    project = get_project(db, project_id=project_id, user_id=user_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _get_code_ws_or_404(db: DBSession, workspace_id: str, user_id: str) -> CodeWorkspace:
    ws = db.query(CodeWorkspace).filter(CodeWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CODE workspace not found")
    _get_project_or_404(db, ws.project_id, user_id)
    return ws


def _require_plan_feature(plan: str, feature: str, product: str) -> None:
    """TRINITY CODE and WORK are features a plan includes or not
    (PlanEntitlement.supports_trinity_code / supports_trinity_work), exactly
    like agent mode. Until the 2026-09-13 pre-launch audit nothing checked
    them here: a FREE account could run CODE tasks and sandbox commands, and
    START could run WORK. Checked after ownership (a foreign workspace stays a
    404) and before any reservation, so a plan without the feature never
    reserves, never calls a provider and never starts a sandbox. Same 403
    shape as sessions.py's agent-mode gate."""
    if not getattr(get_plan_entitlement(plan), feature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "feature_not_entitled",
                "message": f"{product} is not included in your current plan. Upgrade your plan to access it.",
                "plan": plan,
            },
        )


# ── CODE Workspaces (stub — no execution yet) ─────────────────────────────────

@router.post("/code", response_model=CodeWorkspaceResponse, status_code=status.HTTP_201_CREATED)
def create_code_ws(
    body: CreateCodeWorkspaceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, body.project_id, current_user.id)
    if db.query(CodeWorkspace).filter_by(project_id=body.project_id).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="CODE workspace already exists for this project")
    try:
        ws = create_code_workspace(
            db,
            project_id=body.project_id,
            root_path=body.root_path,
            language_hint=body.language_hint,
            run_command=body.run_command,
            build_command=body.build_command,
            test_command=body.test_command,
            git_enabled=body.git_enabled,
            sandbox_image=body.sandbox_image,
        )
    except ArbitraryWorkspaceRootDisabled as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return ws


@router.get("/code/{workspace_id}", response_model=CodeWorkspaceResponse)
def get_code_ws(
    workspace_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(CodeWorkspace).filter(CodeWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CODE workspace not found")
    _get_project_or_404(db, ws.project_id, current_user.id)
    return ws


@router.patch("/code/{workspace_id}", response_model=CodeWorkspaceResponse)
def update_code_ws(
    workspace_id: str,
    body: UpdateCodeWorkspaceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(CodeWorkspace).filter(CodeWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CODE workspace not found")
    _get_project_or_404(db, ws.project_id, current_user.id)
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        return update_code_workspace(db, ws, **updates)
    except ArbitraryWorkspaceRootDisabled as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ── CODE Workspace — Execute ──────────────────────────────────────────────────

@router.post("/code/{workspace_id}/execute")
async def execute_code_ws(
    workspace_id: str,
    body: CodeRunRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(CodeWorkspace).filter(CodeWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CODE workspace not found")
    _get_project_or_404(db, ws.project_id, current_user.id)
    _require_plan_feature(current_user.plan, "supports_trinity_code", "TRINITY CODE")

    # Enforce quota BEFORE returning StreamingResponse.
    # session_id=None: workspace execution is not tied to a chat session.
    reservation_id = check_and_reserve(
        db,
        user_id=current_user.id,
        session_id=None,
        plan=current_user.plan,
        user_timezone=current_user.timezone,
    )

    return StreamingResponse(
        stream_code_execute(
            db=db,
            workspace=ws,
            command=body.command,
            timeout=body.timeout,
            workspace_id=workspace_id,
            user_plan=current_user.plan,
            reservation_id=reservation_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── CODE Workspace — AI Task ──────────────────────────────────────────────────

@router.post("/code/{workspace_id}/task")
async def run_code_task(
    workspace_id: str,
    body: CodeTaskRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _get_code_ws_or_404(db, workspace_id, current_user.id)
    _require_plan_feature(current_user.plan, "supports_trinity_code", "TRINITY CODE")

    reservation_id = check_and_reserve(
        db,
        user_id=current_user.id,
        session_id=None,
        plan=current_user.plan,
        user_timezone=current_user.timezone,
    )

    return StreamingResponse(
        stream_code_task(
            db=db,
            workspace=ws,
            task=body.task,
            max_iterations=body.max_iterations,
            workspace_id=workspace_id,
            user_plan=current_user.plan,
            reservation_id=reservation_id,
            user_id=current_user.id,
            user_timezone=current_user.timezone,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── CODE Workspace — File Browser ────────────────────────────────────────────

@router.get("/code/{workspace_id}/files", response_model=WorkspaceFileListResponse)
def list_code_ws_files(
    workspace_id: str,
    path: str = "",
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _get_code_ws_or_404(db, workspace_id, current_user.id)
    if not ws.root_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Workspace root_path not configured",
        )
    try:
        entries = list_workspace_files(ws.root_path, path)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal detected")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return WorkspaceFileListResponse(entries=entries, path=path)


@router.get("/code/{workspace_id}/files/{filepath:path}", response_model=WorkspaceFileContentResponse)
def read_code_ws_file(
    workspace_id: str,
    filepath: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _get_code_ws_or_404(db, workspace_id, current_user.id)
    if not ws.root_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Workspace root_path not configured",
        )
    try:
        content = read_workspace_file(ws.root_path, filepath)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal detected")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return WorkspaceFileContentResponse(path=filepath, content=content)


# ── WORK Workspaces — Execution ──────────────────────────────────────────────

def _get_work_ws_or_404(db: DBSession, workspace_id: str, user_id: str) -> WorkWorkspace:
    ws = db.query(WorkWorkspace).filter(WorkWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORK workspace not found")
    _get_project_or_404(db, ws.project_id, user_id)
    return ws


@router.post("/work/{workspace_id}/execute")
async def execute_work_task(
    workspace_id: str,
    body: WorkTaskRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _get_work_ws_or_404(db, workspace_id, current_user.id)
    _require_plan_feature(current_user.plan, "supports_trinity_work", "TRINITY WORK")

    reservation_id = check_and_reserve(
        db,
        user_id=current_user.id,
        session_id=None,
        plan=current_user.plan,
        user_timezone=current_user.timezone,
    )

    return StreamingResponse(
        stream_work_task(
            db=db,
            workspace=ws,
            task=body.task,
            max_iterations=body.max_iterations,
            workspace_id=workspace_id,
            user_plan=current_user.plan,
            reservation_id=reservation_id,
            user_id=current_user.id,
            user_timezone=current_user.timezone,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/work/{workspace_id}/approve", status_code=status.HTTP_200_OK)
def approve_work_action(
    workspace_id: str,
    body: WorkApprovalRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_work_ws_or_404(db, workspace_id, current_user.id)
    found = resolve_gate(body.action_id, approved=True, workspace_id=workspace_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval gate not found or already resolved",
        )
    return {"status": "approved"}


@router.post("/work/{workspace_id}/reject", status_code=status.HTTP_200_OK)
def reject_work_action(
    workspace_id: str,
    body: WorkApprovalRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_work_ws_or_404(db, workspace_id, current_user.id)
    found = resolve_gate(body.action_id, approved=False, workspace_id=workspace_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval gate not found or already resolved",
        )
    return {"status": "rejected"}


@router.post("/work", response_model=WorkWorkspaceResponse, status_code=status.HTTP_201_CREATED)
def create_work_ws(
    body: CreateWorkWorkspaceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, body.project_id, current_user.id)
    if db.query(WorkWorkspace).filter_by(project_id=body.project_id).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WORK workspace already exists for this project")
    ws = create_work_workspace(
        db,
        project_id=body.project_id,
        requires_approval=body.requires_approval,
        approval_policy=body.approval_policy,
    )
    return ws


@router.get("/work/{workspace_id}", response_model=WorkWorkspaceResponse)
def get_work_ws(
    workspace_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(WorkWorkspace).filter(WorkWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORK workspace not found")
    _get_project_or_404(db, ws.project_id, current_user.id)
    return ws


@router.patch("/work/{workspace_id}", response_model=WorkWorkspaceResponse)
def update_work_ws(
    workspace_id: str,
    body: UpdateWorkWorkspaceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(WorkWorkspace).filter(WorkWorkspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORK workspace not found")
    _get_project_or_404(db, ws.project_id, current_user.id)
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    return update_work_workspace(db, ws, **updates)


# ── WORK Connectors ───────────────────────────────────────────────────────────

@router.get("/work/{workspace_id}/connectors", response_model=ConnectorListResponse)
def list_work_connectors(
    workspace_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List connectors enabled for this WORK workspace.

    Connector integration is planned for a future phase (section 4.10 of the plan).
    Returns an empty list until connectors are configured. No secrets are ever
    included in this response — connector credentials live outside this service.
    """
    _get_work_ws_or_404(db, workspace_id, current_user.id)
    return ConnectorListResponse(workspace_id=workspace_id, connectors=[])
