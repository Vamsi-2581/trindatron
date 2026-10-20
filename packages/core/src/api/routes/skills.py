from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.database import get_db
from src.models.user import User
from src.schemas.skill import CreateSkillRequest, SkillListResponse, SkillResponse, UpdateSkillRequest
from src.services.skill_service import (
    BUILTIN_SKILL_IDS,
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    update_skill,
)

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillListResponse)
def list_all_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    skills = list_skills(db, current_user.id)
    return SkillListResponse(skills=skills, total=len(skills))


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
def create_custom_skill(
    body: CreateSkillRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        skill = create_skill(
            db,
            user_id=current_user.id,
            name=body.name,
            tool_ids=body.tool_ids,
            description=body.description,
            instructions=body.instructions,
            icon_emoji=body.icon_emoji,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return get_skill(db, skill.id, current_user.id)


@router.get("/{skill_id}", response_model=SkillResponse)
def get_skill_detail(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    skill = get_skill(db, skill_id, current_user.id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return skill


@router.patch("/{skill_id}", response_model=SkillResponse)
def update_custom_skill(
    skill_id: str,
    body: UpdateSkillRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if skill_id in BUILTIN_SKILL_IDS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Built-in skills cannot be modified")
    try:
        updated = update_skill(
            db,
            skill_id=skill_id,
            user_id=current_user.id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return get_skill(db, skill_id, current_user.id)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_custom_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if skill_id in BUILTIN_SKILL_IDS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Built-in skills cannot be deleted")
    deleted = delete_skill(db, skill_id=skill_id, user_id=current_user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
