"""Scheduled tasks ("Agendados") — see src/services/scheduled_task_service.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.models.user import User
from src.services import scheduled_task_service as tasks
from src.services.scheduled_task_service import ScheduledTaskRefused

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled"])

_STATUS = {"plan_not_included": status.HTTP_403_FORBIDDEN, "not_found": status.HTTP_404_NOT_FOUND,
           "invalid_request": status.HTTP_422_UNPROCESSABLE_ENTITY, "limit_reached": status.HTTP_409_CONFLICT,
           "job_in_progress": status.HTTP_409_CONFLICT, "capacity_refused": status.HTTP_429_TOO_MANY_REQUESTS}


def _http(exc: ScheduledTaskRefused) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
                         detail={"error": exc.code, "message": str(exc) if str(exc) != exc.code else ""})


class Recurrence(BaseModel):
    kind: str
    time: str | None = None
    weekday: int | None = None
    timezone: str | None = None


class CreateTask(BaseModel):
    title: str = Field(min_length=1, max_length=tasks.MAX_TITLE)
    instruction: str = Field(min_length=1, max_length=tasks.MAX_INSTRUCTION)
    recurrence: Recurrence


class UpdateTask(BaseModel):
    title: str | None = Field(default=None, max_length=tasks.MAX_TITLE)
    instruction: str | None = Field(default=None, max_length=tasks.MAX_INSTRUCTION)
    is_active: bool | None = None
    recurrence: Recurrence | None = None


@router.get("")
def list_scheduled_tasks(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {"tasks": [tasks.task_view(db, row) for row in tasks.list_tasks(db, current_user)]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_scheduled_task(body: CreateTask, db: DBSession = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    try:
        row = tasks.create_task(db, current_user, title=body.title, instruction=body.instruction,
                                recurrence=body.recurrence.model_dump(exclude_none=True))
    except ScheduledTaskRefused as exc:
        raise _http(exc) from exc
    return tasks.task_view(db, row)


@router.patch("/{task_id}")
def update_scheduled_task(task_id: str, body: UpdateTask, db: DBSession = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    try:
        row = tasks.update_task(db, current_user, task_id, title=body.title, instruction=body.instruction,
                                is_active=body.is_active,
                                recurrence=body.recurrence.model_dump(exclude_none=True) if body.recurrence else None)
    except ScheduledTaskRefused as exc:
        raise _http(exc) from exc
    return tasks.task_view(db, row)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scheduled_task(task_id: str, db: DBSession = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    try:
        tasks.delete_task(db, current_user, task_id)
    except ScheduledTaskRefused as exc:
        raise _http(exc) from exc


@router.post("/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_scheduled_task_now(task_id: str, db: DBSession = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    try:
        job = tasks.run_now(db, current_user, task_id)
    except ScheduledTaskRefused as exc:
        raise _http(exc) from exc
    return {"job_id": job.id, "status": job.status}
