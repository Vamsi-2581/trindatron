from __future__ import annotations

import os
from pathlib import Path
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.project import Project, ProjectFile
from src.models.code_workspace import CodeWorkspace
from src.models.work_workspace import WorkWorkspace

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


class ArbitraryWorkspaceRootDisabled(Exception):
    """Raised when root_path is supplied but this deployment is production
    (Security Hardening — SANDBOX-01, CRITICAL). CodeWorkspace.root_path is
    entirely client-chosen with no concept of per-user filesystem
    ownership (unlike every other resource in this codebase) — the
    'point this at any host directory' design only makes sense for a
    single-user local/desktop deployment, never for the shared,
    multi-tenant hosted SaaS this app is launching as (any authenticated
    user could otherwise browse/read anything the server process can
    read). Same safe-by-default philosophy already used for optional
    payment providers: off unless a deployment explicitly needs it — see
    config.py's code_workspace_arbitrary_root_enabled."""


# ── Projects ──────────────────────────────────────────────────────────────────

def create_project(db: DBSession, user_id: str, name: str, description: str | None, instructions: str | None) -> Project:
    project = Project(user_id=user_id, name=name, description=description, instructions=instructions)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def get_projects(db: DBSession, user_id: str) -> list[Project]:
    return (
        db.query(Project)
        .filter(Project.user_id == user_id, Project.is_archived.is_(False))
        .order_by(Project.updated_at.desc())
        .all()
    )


def get_project(db: DBSession, project_id: str, user_id: str) -> Project | None:
    return (
        db.query(Project)
        .filter(Project.id == project_id, Project.user_id == user_id)
        .first()
    )


def update_project(db: DBSession, project: Project, **fields) -> Project:
    for key, value in fields.items():
        if value is not None or key in ("description", "instructions"):
            setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return project


def archive_project(db: DBSession, project: Project) -> None:
    project.is_archived = True
    db.commit()


# ── Project Files ─────────────────────────────────────────────────────────────

def add_project_file(
    db: DBSession,
    project_id: str,
    name: str,
    mime_type: str,
    content: bytes,
) -> ProjectFile:
    if len(content) > _MAX_FILE_BYTES:
        raise ValueError(f"File exceeds 10 MB limit ({len(content)} bytes)")
    pf = ProjectFile(
        project_id=project_id,
        name=name,
        mime_type=mime_type,
        size_bytes=len(content),
        content=content,
    )
    db.add(pf)
    db.commit()
    db.refresh(pf)
    return pf


def get_project_file(db: DBSession, file_id: str, project_id: str) -> ProjectFile | None:
    return db.query(ProjectFile).filter(ProjectFile.id == file_id, ProjectFile.project_id == project_id).first()


def delete_project_file(db: DBSession, pf: ProjectFile) -> None:
    db.delete(pf)
    db.commit()


# ── Code Workspaces ───────────────────────────────────────────────────────────

def _check_root_path_allowed(root_path: str | None) -> None:
    if root_path is None:
        return
    # Security Hardening (SANDBOX-01, final gate) — production is an
    # UNCONDITIONAL hard block, independent of
    # code_workspace_arbitrary_root_enabled. That setting exists for a
    # deliberate desktop/local opt-in; it must never rescue the shared,
    # multi-tenant hosted deployment from itself if someone sets
    # CODE_WORKSPACE_ARBITRARY_ROOT_ENABLED=true there by mistake (env var
    # copy-paste, a shared .env template, etc.) — this check does not
    # consult that setting at all when environment == "production".
    if settings.is_production:
        raise ArbitraryWorkspaceRootDisabled(
            "Setting a custom workspace root_path is never allowed in production."
        )
    if not settings.code_workspace_arbitrary_root_enabled:
        raise ArbitraryWorkspaceRootDisabled(
            "Setting a custom workspace root_path is disabled on this deployment."
        )


def create_code_workspace(db: DBSession, project_id: str, **fields) -> CodeWorkspace:
    _check_root_path_allowed(fields.get("root_path"))
    ws = CodeWorkspace(project_id=project_id, **fields)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def get_code_workspace(db: DBSession, workspace_id: str, project_id: str) -> CodeWorkspace | None:
    return db.query(CodeWorkspace).filter(CodeWorkspace.id == workspace_id, CodeWorkspace.project_id == project_id).first()


def get_code_workspace_by_project(db: DBSession, project_id: str) -> CodeWorkspace | None:
    return db.query(CodeWorkspace).filter(CodeWorkspace.project_id == project_id).first()


# ── Workspace File Browser ─────────────────────────────────────────────────────

# Security Hardening (SANDBOX-01, CRITICAL) — workspace.root_path is
# entirely client-supplied (CreateCodeWorkspaceRequest/UpdateCodeWorkspaceRequest
# have no validator) and, before this fix, was trusted verbatim as the
# browsing root here — _resolve_safe_path() only ever protected the
# SUBPATH from traversing outside root_path, never validated root_path
# itself. Any authenticated user could set root_path to an arbitrary host
# directory (e.g. this application's own working directory) and read any
# file the server process can read, including .env/secrets — devastating
# in the shared, multi-tenant hosted deployment this app is launching as.
#
# Mirrors code_service.py's own _workspace_root_is_safe()/_BLOCKED_SYSTEM_PATHS
# (same mitigation the sibling execute-path already used, extended here
# to also cover the file-browser read path, which had ZERO such check) —
# NOT re-imported from code_service.py to avoid pulling that module's
# heavy trinity_router/trinity_sandbox dependency chain into this
# lower-level service for a simple path check. Extended for Windows
# (the denylist code_service.py shipped with was POSIX-only) and to
# always reject the app's own install/working directory or any ancestor
# of it — the single most damaging, concretely-demonstrated exploit
# (reading this app's own .env) is closed unconditionally, regardless of
# platform or deployment.
_BLOCKED_SYSTEM_PATHS: frozenset[str] = frozenset({
    "/etc", "/usr", "/bin", "/sbin", "/root",
    "/proc", "/sys", "/dev", "/boot",
    "/var/run", "/var/log", "/lib", "/lib64",
    "c:/windows", "c:/program files", "c:/program files (x86)",
})


def _is_bare_drive_root(p_str: str) -> bool:
    """True for a Windows drive root like 'c:/' or 'c:' — never a
    legitimate workspace root, and not caught by the prefix-list above."""
    return len(p_str) <= 3 and len(p_str) >= 2 and p_str[1] == ":"


def _workspace_root_is_safe(root: Path) -> bool:
    p_str = str(root).replace("\\", "/").lower()
    if _is_bare_drive_root(p_str):
        return False
    if any(p_str == blocked or p_str.startswith(blocked + "/") for blocked in _BLOCKED_SYSTEM_PATHS):
        return False
    # Never allow browsing this application's own install/working
    # directory — as the root itself, as an ancestor of it, or as a path
    # inside it — closes the .env/secrets read regardless of platform or
    # where the process happens to be deployed.
    app_str = str(Path.cwd().resolve()).replace("\\", "/").lower()
    if p_str == app_str:
        return False
    if app_str.startswith(p_str + "/"):  # root is an ancestor of the app dir
        return False
    if p_str.startswith(app_str + "/"):  # root is inside the app dir
        return False
    return True


def _resolve_safe_path(root_path: str, subpath: str) -> str:
    """Resolve subpath under root_path; raises ValueError on path traversal
    OR on root_path itself resolving to a disallowed location (see
    _workspace_root_is_safe's own docstring above — SANDBOX-01)."""
    root = Path(root_path).resolve()
    if not _workspace_root_is_safe(root):
        raise ValueError(f"Workspace root is not allowed: {root_path!r}")
    if subpath:
        target = (root / subpath).resolve()
    else:
        target = root
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"Path outside workspace root: {subpath!r}")
    return str(target)


def list_workspace_files(root_path: str, subpath: str = "") -> list[dict]:
    target = _resolve_safe_path(root_path, subpath)
    root_resolved = str(Path(root_path).resolve())
    if not os.path.isdir(target):
        raise FileNotFoundError("Not a directory")
    entries = []
    for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
        rel = str(Path(entry.path).relative_to(root_resolved)).replace("\\", "/")
        entries.append({
            "name": entry.name,
            "path": rel,
            "is_dir": entry.is_dir(),
            "size": None if entry.is_dir() else entry.stat().st_size,
        })
    return entries


def read_workspace_file(root_path: str, filepath: str) -> str:
    target = _resolve_safe_path(root_path, filepath)
    if not os.path.isfile(target):
        raise FileNotFoundError("File not found")
    with open(target, encoding="utf-8", errors="replace") as f:
        return f.read()


def update_code_workspace(db: DBSession, ws: CodeWorkspace, **fields) -> CodeWorkspace:
    if "root_path" in fields:
        _check_root_path_allowed(fields["root_path"])
    for key, value in fields.items():
        if value is not None:
            setattr(ws, key, value)
    db.commit()
    db.refresh(ws)
    return ws


# ── Work Workspaces ───────────────────────────────────────────────────────────

def create_work_workspace(db: DBSession, project_id: str, **fields) -> WorkWorkspace:
    ws = WorkWorkspace(project_id=project_id, **fields)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def get_work_workspace(db: DBSession, workspace_id: str, project_id: str) -> WorkWorkspace | None:
    return db.query(WorkWorkspace).filter(WorkWorkspace.id == workspace_id, WorkWorkspace.project_id == project_id).first()


def get_work_workspace_by_project(db: DBSession, project_id: str) -> WorkWorkspace | None:
    return db.query(WorkWorkspace).filter(WorkWorkspace.project_id == project_id).first()


def update_work_workspace(db: DBSession, ws: WorkWorkspace, **fields) -> WorkWorkspace:
    for key, value in fields.items():
        if value is not None:
            setattr(ws, key, value)
    db.commit()
    db.refresh(ws)
    return ws
