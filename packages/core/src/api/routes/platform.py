"""Product routers added after src/main.py was frozen.

src/main.py belongs to the Presenton work whose files are held byte-for-byte
(see docs/INTEGRATION.md). Instead of editing it, the routers below are
mounted onto the prefix-less system router (routes/health.py), which main.py
already includes — the same technique routes/auth.py uses for /auth/google.
Paths are exactly what they would be if main.py included them directly.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.routes.attachments import router as attachments_router
from src.api.routes.media import router as media_router
from src.api.routes.support import router as support_router
from src.api.routes.teams import router as teams_router
from src.database import get_db
from src.models.user import User
from src.services import capability_service

router = APIRouter()
router.include_router(media_router)
router.include_router(attachments_router)
router.include_router(teams_router)
router.include_router(support_router)


@router.get("/users/me/capabilities", tags=["users"])
async def my_capabilities(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """What this user can do right now. Abstract names only; see capability_service."""
    return await capability_service.user_capabilities(db, current_user)
