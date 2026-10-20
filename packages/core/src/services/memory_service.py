from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy.orm import Session as DbSession

from src.models.memory import Memory


_MAX_CONTENT_CHARS = 2_000
_MAX_CONTEXT_MEMORIES = 20


def store_memory(
    db: DbSession,
    user_id: str,
    content: str,
    source_session_id: str | None = None,
) -> Memory:
    content = content.strip()
    if not content:
        raise ValueError("Memory content cannot be empty")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError(f"Memory content exceeds {_MAX_CONTENT_CHARS} characters")

    now = datetime.now(timezone.utc)
    memory = Memory(
        user_id=user_id,
        content=content,
        source_session_id=source_session_id,
        created_at=now,
        updated_at=now,
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


def get_user_memories(
    db: DbSession,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[Memory]:
    return (
        db.query(Memory)
        .filter(Memory.user_id == user_id)
        .order_by(Memory.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def delete_memory(db: DbSession, user_id: str, memory_id: str) -> bool:
    memory = (
        db.query(Memory)
        .filter(Memory.id == memory_id, Memory.user_id == user_id)
        .first()
    )
    if not memory:
        return False
    db.delete(memory)
    db.commit()
    return True


def build_memory_context(db: DbSession, user_id: str) -> str:
    """Recent memories as a flat block, for the GET /memories/context preview.

    NOT what chat and agent inject any more. Those select only the memories
    relevant to the request (src/prompting/memory_context.py) instead of
    sending the most recent twenty regardless of subject; this remains the
    "show me everything you remember" view, which is a different question and
    the one this endpoint is actually asked.
    """
    memories = (
        db.query(Memory)
        .filter(Memory.user_id == user_id)
        .order_by(Memory.created_at.desc())
        .limit(_MAX_CONTEXT_MEMORIES)
        .all()
    )
    if not memories:
        return ""
    lines = ["# Memórias do usuário (informações persistentes entre sessões)"]
    for m in reversed(memories):
        lines.append(f"- {m.content}")
    return "\n".join(lines)
