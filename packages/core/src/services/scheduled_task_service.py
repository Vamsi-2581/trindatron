"""Scheduled tasks ("Agendados"): an instruction TRINDATRON carries out on a clock.

Not a second scheduler. A scheduled task is an AgentSchedule with the "task"
job_payload shape, fired by the existing schedule poll loop
(agent_schedule_service._fire_due_schedules): same plan gate (agent mode), same
one-active-job invariant, same capacity refusal, same ExecutionJob runtime,
same TCU reservation. The only difference from a persistent agent's schedule is
that a task has nothing to CHECK and nothing to DECIDE — its time came.

  job_payload = {
    "task": {"title", "instruction", "job_type": "research",
             "payload": {"steps": [{"command": instruction}]}},
    "recurrence": {"kind": "hourly"|"daily"|"weekly", "time": "HH:MM", "weekday": 0-6, "timezone"},
  }

The instruction runs as a research job (Model Router, no tools): it can think,
write, summarise, plan and research with what the model knows. It does not act
on websites or files on its own schedule — acting still happens in a
conversation where every action behind an approval waits for the person.

Recurrence is what the schedule engine supports: a fixed interval anchored at
the first run (hourly, daily at a time, weekly on a day at a time). The clock is
absolute, so a daily task at 09:00 keeps its UTC instant across a daylight-saving
change. A one-off task is not offered yet.

Every AgentSchedule needs an AgentDefinition; tasks share one per person,
archived so it never appears among the person's own agents.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session as DBSession

from src.entitlements import get_plan_entitlement
from src.models.agent_definition import AgentDefinition
from src.models.agent_schedule import AgentSchedule
from src.models.execution import ExecutionJob
from src.models.user import User
from src.services.agent_schedule_service import _is_task_schedule, _schedule_has_active_job

INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_TASKS_AGENT_NAME = "Agendados"
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_TITLE, MAX_INSTRUCTION = 120, 4000
MAX_ACTIVE_TASKS = 20


class ScheduledTaskRefused(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def normalize_recurrence(recurrence: dict, user_timezone: str | None) -> dict:
    if not isinstance(recurrence, dict) or recurrence.get("kind") not in INTERVALS:
        raise ScheduledTaskRefused("invalid_request", "recurrence.kind must be hourly, daily or weekly")
    kind = recurrence["kind"]
    out: dict = {"kind": kind, "timezone": str(_zone(recurrence.get("timezone") or user_timezone))}
    if kind in ("daily", "weekly"):
        value = str(recurrence.get("time") or "")
        if not _TIME.match(value):
            raise ScheduledTaskRefused("invalid_request", "recurrence.time must be HH:MM")
        out["time"] = value
    if kind == "weekly":
        weekday = recurrence.get("weekday")
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ScheduledTaskRefused("invalid_request", "recurrence.weekday must be 0 (Monday) to 6 (Sunday)")
        out["weekday"] = weekday
    return out


def first_run_at(recurrence: dict, now: datetime | None = None) -> datetime:
    """The next instant this recurrence is due, in UTC."""
    now = (now or _now()).astimezone(timezone.utc)
    kind = recurrence["kind"]
    if kind == "hourly":
        return now + timedelta(seconds=INTERVALS["hourly"])
    zone = _zone(recurrence.get("timezone"))
    local_now = now.astimezone(zone)
    hour, minute = (int(part) for part in recurrence["time"].split(":"))
    candidate = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
    if kind == "weekly":
        candidate += timedelta(days=(recurrence["weekday"] - candidate.weekday()) % 7)
    while candidate <= local_now:
        candidate += timedelta(days=7 if kind == "weekly" else 1)
    return candidate.astimezone(timezone.utc)


def _require_plan(user: User) -> None:
    if not get_plan_entitlement(user.plan).supports_agent_mode:
        raise ScheduledTaskRefused("plan_not_included")


def _tasks_agent(db: DBSession, user: User) -> AgentDefinition:
    agent = (
        db.query(AgentDefinition)
        .filter(AgentDefinition.user_id == user.id, AgentDefinition.name == _TASKS_AGENT_NAME,
                AgentDefinition.is_archived.is_(True))
        .first()
    )
    if agent is None:
        agent = AgentDefinition(user_id=user.id, name=_TASKS_AGENT_NAME, is_archived=True,
                                description="Tarefas agendadas do TRINDATRON", skill_ids=[], priority="low",
                                execution_timeout_seconds=300)
        db.add(agent)
        db.flush()
    return agent


def _clean_text(value: str | None, limit: int, field: str) -> str:
    text = " ".join((value or "").split()) if field == "title" else (value or "").strip()
    if not text or len(text) > limit:
        raise ScheduledTaskRefused("invalid_request", f"{field} must be 1-{limit} characters")
    return text


def _task_payload(title: str, instruction: str, recurrence: dict) -> dict:
    return {
        "task": {"title": title, "instruction": instruction, "job_type": "research",
                 "payload": {"steps": [{"command": instruction}]}},
        "recurrence": recurrence,
    }


def create_task(db: DBSession, user: User, *, title: str, instruction: str, recurrence: dict) -> AgentSchedule:
    _require_plan(user)
    title = _clean_text(title, MAX_TITLE, "title")
    instruction = _clean_text(instruction, MAX_INSTRUCTION, "instruction")
    recurrence = normalize_recurrence(recurrence, user.timezone)
    active = [s for s in list_tasks(db, user) if s.is_active]
    if len(active) >= MAX_ACTIVE_TASKS:
        raise ScheduledTaskRefused("limit_reached")
    agent = _tasks_agent(db, user)
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=INTERVALS[recurrence["kind"]],
        job_payload=_task_payload(title, instruction, recurrence), priority="low",
        next_run_at=first_run_at(recurrence),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def list_tasks(db: DBSession, user: User) -> list[AgentSchedule]:
    rows = (
        db.query(AgentSchedule)
        .filter(AgentSchedule.user_id == user.id)
        .order_by(AgentSchedule.created_at.desc())
        .all()
    )
    return [row for row in rows if _is_task_schedule(row)]


def owned_task(db: DBSession, user: User, task_id: str) -> AgentSchedule:
    row = db.query(AgentSchedule).filter(AgentSchedule.id == task_id, AgentSchedule.user_id == user.id).first()
    if row is None or not _is_task_schedule(row):
        raise ScheduledTaskRefused("not_found")
    return row


def update_task(db: DBSession, user: User, task_id: str, *, title: str | None = None,
                instruction: str | None = None, is_active: bool | None = None,
                recurrence: dict | None = None) -> AgentSchedule:
    row = owned_task(db, user, task_id)
    payload = dict(row.job_payload)
    task = dict(payload["task"])
    if title is not None:
        task["title"] = _clean_text(title, MAX_TITLE, "title")
    if instruction is not None:
        task["instruction"] = _clean_text(instruction, MAX_INSTRUCTION, "instruction")
        task["payload"] = {"steps": [{"command": task["instruction"]}]}
    if recurrence is not None:
        payload["recurrence"] = normalize_recurrence(recurrence, user.timezone)
        row.interval_seconds = INTERVALS[payload["recurrence"]["kind"]]
        row.next_run_at = first_run_at(payload["recurrence"])
    if is_active is not None:
        if is_active and not row.is_active:
            _require_plan(user)
            row.next_run_at = first_run_at(payload.get("recurrence") or {"kind": "hourly"})
        row.is_active = is_active
    payload["task"] = task
    row.job_payload = payload
    db.commit()
    db.refresh(row)
    return row


def delete_task(db: DBSession, user: User, task_id: str) -> None:
    row = owned_task(db, user, task_id)
    db.delete(row)
    db.commit()


def run_now(db: DBSession, user: User, task_id: str) -> ExecutionJob:
    """Run the task once now, through the same runtime and the same one-active-job rule."""
    from src.services import execution_service

    _require_plan(user)
    row = owned_task(db, user, task_id)
    if _schedule_has_active_job(db, row):
        raise ScheduledTaskRefused("job_in_progress")
    task = row.job_payload["task"]
    try:
        job = execution_service.create_job(
            db, user_id=user.id, plan=user.plan, user_timezone=user.timezone, agent_id=row.agent_id,
            job_type=task.get("job_type", "research"), payload=task["payload"], priority="normal",
            timeout_seconds=300,
        )
    except execution_service.CapacityRefusedError as exc:
        raise ScheduledTaskRefused("capacity_refused") from exc
    row.last_run_job_id, row.last_run_at = job.id, _now()
    row.last_decision, row.last_decision_at = "acted", _now()
    db.commit()
    return job


def _excerpt(result) -> str | None:
    if isinstance(result, dict):
        for key in ("output", "content", "summary", "text"):
            if isinstance(result.get(key), str) and result[key].strip():
                return result[key].strip()[:600]
    return None


def task_view(db: DBSession, row: AgentSchedule) -> dict:
    payload = row.job_payload or {}
    task = payload.get("task") or {}
    last_job = db.query(ExecutionJob).filter(ExecutionJob.id == row.last_run_job_id).first() if row.last_run_job_id else None
    return {
        "id": row.id,
        "title": task.get("title", ""),
        "instruction": task.get("instruction", ""),
        "recurrence": payload.get("recurrence") or {},
        "is_active": row.is_active,
        "next_run_at": row.next_run_at.isoformat() if row.is_active and row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_outcome": row.last_decision,
        "last_run": None if last_job is None else {
            "job_id": last_job.id, "status": last_job.status,
            "completed_at": last_job.completed_at.isoformat() if last_job.completed_at else None,
            "excerpt": _excerpt(last_job.result),
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
