"""Turning database rows into prompt layers, once.

chat_service and agent_service previously each carried their own copy of this
logic — load the project, load the agent definition, load the skills, load
memory, concatenate — and the copies had drifted. These builders exist so the
two paths cannot drift again: whatever a project's instructions mean for a chat
turn, they mean the same for an agent turn, because it is the same function.

Kept out of assembly.py deliberately. That module knows how to order and
budget layers and touches no database; this one knows the schema and touches no
prompt structure. The split keeps assembly unit-testable without a session and
keeps ORM details out of the layer that every service imports.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session as DBSession

from src.models.memory import Memory
from src.prompting.memory_context import DEFAULT_LIMIT, MemoryContext
from src.prompting.task_context import TaskContext
from src.services.skill_service import load_skills_for_context

logger = logging.getLogger("trinity.prompting")

#: How many memories to consider before relevance selection narrows them. The
#: pool is wider than what ships (DEFAULT_LIMIT) so selection has something to
#: choose from; only the selected few ever reach the prompt.
_MEMORY_POOL = 50


def build_task_context(
    db: DBSession,
    session,
    *,
    task_type: str = "",
    capability: str = "",
    complexity: str = "",
    language: str = "",
    client_locale: str = "",
    has_tools: bool = False,
) -> TaskContext:
    """Assemble the per-request context from the session's own records.

    Lazy relationship access here is real querying, not attribute access — the
    callers run this inside asyncio.to_thread for exactly that reason.
    """
    project_instructions = ""
    if session.project_id and session.project and session.project.instructions:
        project_instructions = session.project.instructions

    agent_instructions = ""
    skills: list[tuple[str, str]] = []
    if session.agent_definition_id and session.agent_definition:
        agent = session.agent_definition
        agent_instructions = agent.system_instructions or ""
        if agent.skill_ids:
            for skill in load_skills_for_context(db, agent.skill_ids, session.user_id):
                if skill.instructions:
                    skills.append((skill.name, skill.instructions))

    from src.services.attachment_service import context_for_session

    capabilities: tuple[tuple[str, bool, str], ...] = ()
    try:
        from src.capability import matrix as capability_matrix
        from src.models.user import User

        user = db.query(User).filter(User.id == session.user_id).first() if getattr(session, "user_id", None) else None
        if user is not None:
            capabilities = capability_matrix.prompt_lines(capability_matrix.snapshot(db, user))
            # What the model was told it cannot do this turn: a reply saying "not available" is then
            # checkable against what it was actually told (2026-09-27: video was refused while it worked).
            missing = sorted(name for name, ok, _ in capabilities if not ok)
            logger.info("prompting: capabilities | session=%s missing=%s", session.id, ",".join(missing) or "none")
    except Exception:
        # The capability block informs the model; a failure to build it must never break the turn.
        logger.warning("prompting: capability block unavailable", exc_info=True)

    # The person's own settings: answer language (when chosen) and response style. Per person, per request.
    response_style = ""
    if getattr(session, "user_id", None):
        try:
            from src.services import preferences_service

            preferences = preferences_service.get_preferences(db, session.user_id)
            language = language or preferences_service.prompt_language(preferences)
            response_style = preferences_service.prompt_style(preferences)
        except Exception:
            logger.warning("prompting: preferences unavailable", exc_info=True)

    # "Automatic" is the device's language, not a guess from the text: the client sends what the phone or the
    # browser is set to, and it is used whenever the person did not choose a language themselves (src/language.py).
    if not language:
        from src.language import normalise
        from src.services.preferences_service import LANGUAGE_NAMES

        language = LANGUAGE_NAMES.get(normalise(client_locale), "")

    # Today, in the person's own timezone when it is known. Without it a model cannot tell how old its
    # own knowledge is, and answers "what is the latest" as confidently as a question about arithmetic.
    today = ""
    try:
        from datetime import datetime, timezone as _timezone
        from zoneinfo import ZoneInfo

        from src.models.user import User as _User
        from src.timezones import FALLBACK_TIMEZONE, resolve_user_timezone

        zone = FALLBACK_TIMEZONE
        if getattr(session, "user_id", None):
            row = db.query(_User.timezone).filter(_User.id == session.user_id).first()
            zone = resolve_user_timezone(row[0] if row else None)
        today = datetime.now(_timezone.utc).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d")
    except Exception:
        logger.warning("prompting: today's date unavailable", exc_info=True)

    return TaskContext(
        capabilities=capabilities,
        has_tools=has_tools,
        task_type=task_type,
        capability=capability,
        complexity=complexity,
        language=language,
        response_style=response_style,
        today=today,
        project_instructions=project_instructions,
        agent_instructions=agent_instructions,
        skill_instructions=tuple(skills),
        attachments=context_for_session(db, session.id) if getattr(session, "id", None) else (),
        artifacts=_media_artifacts(db, session),
    )


#: How many of the conversation's creations are described, newest kept. Enough for "the video" and "the one before".
MEDIA_ARTIFACTS_LIMIT = 5
_KIND_NAMES = {"video": "video", "image": "image", "audio": "audio"}
_IN_PROGRESS = {"queued", "running"}


def _size(size_bytes: int | None) -> str:
    if not size_bytes:
        return ""
    return f"{size_bytes / 1_000_000:.1f} MB" if size_bytes >= 1_000_000 else f"{max(1, round(size_bytes / 1000))} KB"


def _describe(job, artifact) -> str:
    kind = _KIND_NAMES.get(job.kind, job.kind or "file")
    if job.status in _IN_PROGRESS:
        return f"{kind}: still being created, not delivered yet"
    if job.status != "completed":
        return f"{kind}: not delivered ({job.status})"
    if artifact is None:
        return f"{kind}: delivered, but the file is no longer stored"
    facts = [(artifact.content_type or "").split("/")[-1].upper() or "file"]
    if artifact.width and artifact.height:
        facts.append(f"{artifact.width}x{artifact.height}")
    if artifact.duration_seconds:
        facts.append(f"{artifact.duration_seconds:.1f} s")
    size = _size(artifact.size_bytes)
    if size:
        facts.append(size)
    return f"{kind}: delivered — " + ", ".join(facts)


def _media_artifacts(db: DBSession, session) -> tuple[str, ...]:
    """What this conversation already created, from the job and file records — never from what a reply said.

    The stored reply for a creation is only "creating your video…": without this, a question about the result
    ("what size is it?") is answered with an invented format (seen 2026-09-28: "1920x1080, 7-9 MB" for a
    1280x544, 652 KB clip). Only the conversation owner's own jobs are read. Oldest first, newest last.
    """
    session_id, user_id = getattr(session, "id", None), getattr(session, "user_id", None)
    if not session_id or not user_id:
        return ()
    try:
        from src.models.media import Artifact, MediaJob
        from src.models.message import Message

        job_ids = [row[0] for row in (
            db.query(Message.media_job_id)
            .filter(Message.session_id == session_id, Message.media_job_id.isnot(None))
            .order_by(Message.created_at.desc())
            .limit(MEDIA_ARTIFACTS_LIMIT)
            .all()
        )]
        if not job_ids:
            return ()
        jobs = {job.id: job for job in db.query(MediaJob).filter(MediaJob.id.in_(job_ids),
                                                                  MediaJob.user_id == user_id).all()}
        artifact_ids = [job.artifact_id for job in jobs.values() if job.artifact_id]
        artifacts = ({a.id: a for a in db.query(Artifact).filter(Artifact.id.in_(artifact_ids),
                                                                 Artifact.user_id == user_id).all()}
                     if artifact_ids else {})
        ordered = [jobs[job_id] for job_id in reversed(job_ids) if job_id in jobs]
        return tuple(_describe(job, artifacts.get(job.artifact_id)) for job in ordered)
    except Exception:
        # The block informs the model; failing to build it must never break the turn.
        logger.warning("prompting: media artifacts unavailable", exc_info=True)
        return ()


def build_relevant_memory(
    db: DBSession, user_id: str, query: str, *, limit: int = DEFAULT_LIMIT
) -> MemoryContext:
    """The memories that bear on this request, not the user's whole history.

    Reads a bounded recent pool and lets relevance narrow it — see
    memory_context.py for why lexical selection is the chosen trade. A user
    with no memories produces an empty context, which renders as nothing at
    all rather than an empty heading.

    A person who switched memory off in Settings gets no memories in any prompt;
    their saved memories are kept, not deleted, until they delete them.
    """
    from src.services import preferences_service

    if not preferences_service.memory_enabled(db, user_id):
        return MemoryContext.build([], query, limit=limit)
    rows = (
        db.query(Memory.content)
        .filter(Memory.user_id == user_id)
        .order_by(Memory.created_at.desc())
        .limit(_MEMORY_POOL)
        .all()
    )
    # Reversed so the list is oldest-first, which is what select_relevant's
    # recency fallback assumes when it takes a tail slice.
    contents = [r[0] for r in reversed(rows) if r and r[0]]
    return MemoryContext.build(contents, query, limit=limit)
