from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.database import get_db
from src.models.user import User
from src.services.memory_service import (
    build_memory_context,
    delete_memory,
    get_user_memories,
    store_memory,
    _MAX_CONTENT_CHARS,
)

router = APIRouter(prefix="/users/me/memories", tags=["memories"])


class MemoryCreate(BaseModel):
    content: str
    source_session_id: str | None = None

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content cannot be empty")
        if len(v) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        return v


class MemoryResponse(BaseModel):
    id: str
    content: str
    source_session_id: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_obj(cls, m) -> "MemoryResponse":
        return cls(
            id=m.id,
            content=m.content,
            source_session_id=m.source_session_id,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
        )


@router.get("", response_model=list[MemoryResponse])
def list_memories(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memories = get_user_memories(db, current_user.id, limit=limit, offset=offset)
    return [MemoryResponse.from_orm_obj(m) for m in memories]


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
def create_memory(
    body: MemoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        memory = store_memory(
            db,
            user_id=current_user.id,
            content=body.content,
            source_session_id=body.source_session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return MemoryResponse.from_orm_obj(memory)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = delete_memory(db, user_id=current_user.id, memory_id=memory_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")


@router.get("/context")
def memory_context(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the formatted memory block that would be injected into system prompts."""
    return {"context": build_memory_context(db, current_user.id)}
