"""
Skill service.

Built-in skills are defined in code (never in the DB) and always available to all users.
Custom skills are stored in the `skills` table with user_id != NULL.

Security: tool_ids submitted by users are validated against ALLOWED_TOOL_IDS.
          Unknown or fabricated tool IDs are rejected at create/update time.
          At runtime, tool filtering also intersects with ALLOWED_TOOL_IDS as a second line of defence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.models.skill import Skill
from src.schemas.skill import SkillResponse

# ── Tool security: canonical set of valid tool IDs ────────────────────────────
# Must stay in sync with _AGENT_TOOLS in agent_service.py.
# Any tool_id not in this set is rejected at skill creation/update time.

ALLOWED_TOOL_IDS: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "list_directory",
    "execute_shell",
    "web_search",
    "browser_navigate",
    "browser_get_text",
    "browser_screenshot",
    "browser_click",
    "browser_fill",
    # Computer use. These must be listed here for the permission gate to
    # apply DEFAULT_MANIFEST to them at all — agent_service.py only consults
    # the gateway for a name in this set, and an unknown name is treated as a
    # dispatch error rather than a security decision. Leaving them out would
    # mean every desktop action skipped its CONFIRM pause.
    "desktop_screenshot",
    "desktop_get_windows",
    "desktop_move",
    "desktop_click",
    "desktop_double_click",
    "desktop_right_click",
    "desktop_type",
    "desktop_key",
    "desktop_scroll",
    "desktop_drag",
    "desktop_open",
})

# ── Built-in skills (defined in code, not in DB) ─────────────────────────────

@dataclass(frozen=True)
class _BuiltinSkill:
    id: str
    name: str
    description: str
    instructions: str
    tool_ids: tuple[str, ...]
    icon_emoji: str


_BUILTIN_SKILLS: dict[str, _BuiltinSkill] = {
    "00000000-0000-0000-0000-000000000001": _BuiltinSkill(
        id="00000000-0000-0000-0000-000000000001",
        name="Research",
        description="Search the web and synthesize information from multiple sources.",
        instructions=(
            "You specialize in research and information retrieval. "
            "Search for up-to-date information, synthesize multiple sources, "
            "and clearly cite your findings."
        ),
        tool_ids=("web_search",),
        icon_emoji="🔍",
    ),
    "00000000-0000-0000-0000-000000000002": _BuiltinSkill(
        id="00000000-0000-0000-0000-000000000002",
        name="Code",
        description="Read, write, and execute code in the session workspace.",
        instructions=(
            "You specialize in software development. "
            "Write clean, maintainable code. "
            "Explain your approach before writing. "
            "Test your logic mentally before outputting it."
        ),
        tool_ids=("read_file", "write_file", "list_directory", "execute_shell"),
        icon_emoji="💻",
    ),
    "00000000-0000-0000-0000-000000000003": _BuiltinSkill(
        id="00000000-0000-0000-0000-000000000003",
        name="Writing",
        description="Produce clear, structured, and engaging written content.",
        instructions=(
            "You specialize in writing and content creation. "
            "Adapt your style to the user's needs. "
            "Focus on clarity, structure, and impact."
        ),
        tool_ids=(),
        icon_emoji="✍️",
    ),
    "00000000-0000-0000-0000-000000000004": _BuiltinSkill(
        id="00000000-0000-0000-0000-000000000004",
        name="Analysis",
        description="Break down complex problems and draw evidence-based conclusions.",
        instructions=(
            "You specialize in data analysis and critical thinking. "
            "Break down complex problems systematically. "
            "Support every conclusion with evidence."
        ),
        tool_ids=("read_file",),
        icon_emoji="📊",
    ),
    "00000000-0000-0000-0000-000000000005": _BuiltinSkill(
        id="00000000-0000-0000-0000-000000000005",
        name="Math",
        description="Solve mathematical problems step by step with full explanations.",
        instructions=(
            "You specialize in mathematics and quantitative reasoning. "
            "Show your work step by step. "
            "Verify calculations before presenting results."
        ),
        tool_ids=(),
        icon_emoji="🧮",
    ),
}

BUILTIN_SKILL_IDS: frozenset[str] = frozenset(_BUILTIN_SKILLS.keys())


def _builtin_to_response(s: _BuiltinSkill) -> SkillResponse:
    return SkillResponse(
        id=s.id,
        user_id=None,
        name=s.name,
        description=s.description,
        instructions=s.instructions,
        tool_ids=list(s.tool_ids),
        icon_emoji=s.icon_emoji,
        is_builtin=True,
        created_at=None,
        updated_at=None,
    )


def _custom_to_response(s: Skill) -> SkillResponse:
    return SkillResponse(
        id=s.id,
        user_id=s.user_id,
        name=s.name,
        description=s.description,
        instructions=s.instructions,
        tool_ids=list(s.tool_ids or []),
        icon_emoji=s.icon_emoji,
        is_builtin=False,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


# ── Validation ────────────────────────────────────────────────────────────────

def validate_tool_ids(tool_ids: list[str]) -> None:
    """Raise ValueError if any tool_id is not in ALLOWED_TOOL_IDS."""
    unknown = set(tool_ids) - ALLOWED_TOOL_IDS
    if unknown:
        raise ValueError(f"Unknown or disallowed tool IDs: {', '.join(sorted(unknown))}")


def validate_skill_ids_for_user(db: DBSession, skill_ids: list[str], user_id: str) -> None:
    """
    Raise ValueError if any skill_id is neither a built-in nor a custom skill owned by user_id.
    This is the authorization check at AgentDefinition save time.
    """
    if not skill_ids:
        return
    non_builtin = [sid for sid in skill_ids if sid not in BUILTIN_SKILL_IDS]
    if non_builtin:
        owned = {
            row.id
            for row in db.query(Skill.id)
            .filter(Skill.id.in_(non_builtin), Skill.user_id == user_id)
            .all()
        }
        unauthorized = set(non_builtin) - owned
        if unauthorized:
            raise ValueError(f"Unknown or unauthorized skill IDs: {', '.join(sorted(unauthorized))}")


# ── Service functions ─────────────────────────────────────────────────────────

def list_skills(db: DBSession, user_id: str) -> list[SkillResponse]:
    """Return all built-in skills + custom skills owned by the user."""
    builtins = [_builtin_to_response(s) for s in _BUILTIN_SKILLS.values()]
    custom = [
        _custom_to_response(s)
        for s in db.query(Skill).filter(Skill.user_id == user_id).order_by(Skill.created_at).all()
    ]
    return builtins + custom


def get_skill(db: DBSession, skill_id: str, user_id: str) -> SkillResponse | None:
    """Return a skill by ID — built-in (accessible to all) or custom (only owner)."""
    if skill_id in BUILTIN_SKILL_IDS:
        return _builtin_to_response(_BUILTIN_SKILLS[skill_id])
    custom = db.query(Skill).filter(Skill.id == skill_id, Skill.user_id == user_id).first()
    return _custom_to_response(custom) if custom else None


def create_skill(db: DBSession, user_id: str, name: str, tool_ids: list[str], **kwargs) -> Skill:
    validate_tool_ids(tool_ids)
    skill = Skill(
        user_id=user_id,
        name=name,
        tool_ids=tool_ids,
        description=kwargs.get("description"),
        instructions=kwargs.get("instructions"),
        icon_emoji=kwargs.get("icon_emoji"),
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def update_skill(db: DBSession, skill_id: str, user_id: str, **updates) -> Skill | None:
    skill = db.query(Skill).filter(Skill.id == skill_id, Skill.user_id == user_id).first()
    if not skill:
        return None
    if "tool_ids" in updates and updates["tool_ids"] is not None:
        validate_tool_ids(updates["tool_ids"])
    for key, value in updates.items():
        if value is not None or key == "tool_ids":
            setattr(skill, key, value)
    skill.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(skill)
    return skill


def delete_skill(db: DBSession, skill_id: str, user_id: str) -> bool:
    skill = db.query(Skill).filter(Skill.id == skill_id, Skill.user_id == user_id).first()
    if not skill:
        return False
    db.delete(skill)
    db.commit()
    return True


def load_skills_for_context(db: DBSession, skill_ids: list[str], user_id: str) -> list[SkillResponse]:
    """
    Load skills for system prompt injection and tool filtering.
    Built-ins are always returned. Custom skills are checked for ownership.
    Unresolvable IDs are silently skipped (defensive — they were validated at save time).
    """
    result: list[SkillResponse] = []
    custom_ids = []
    for sid in skill_ids:
        if sid in BUILTIN_SKILL_IDS:
            result.append(_builtin_to_response(_BUILTIN_SKILLS[sid]))
        else:
            custom_ids.append(sid)
    if custom_ids:
        custom_skills = (
            db.query(Skill)
            .filter(Skill.id.in_(custom_ids), Skill.user_id == user_id)
            .all()
        )
        result.extend(_custom_to_response(s) for s in custom_skills)
    return result
