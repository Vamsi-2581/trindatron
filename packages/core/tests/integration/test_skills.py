"""
Integration tests for Skills (Phase E).
Built-in skills are defined in code; custom skills are stored in DB.
"""
from __future__ import annotations

from src.services.skill_service import ALLOWED_TOOL_IDS, BUILTIN_SKILL_IDS


# ── helpers ───────────────────────────────────────────────────────────────────

def _register_and_login(client, email="skill_user@test.com", password="securepass123"):
    resp = client.post("/auth/register", json={"email": email, "password": password, "full_name": "T"})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_skill(client, headers, name="My Skill", tool_ids=None, instructions=None):
    body = {"name": name, "tool_ids": tool_ids or []}
    if instructions:
        body["instructions"] = instructions
    resp = client.post("/skills", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Unauthenticated ───────────────────────────────────────────────────────────

def test_list_skills_requires_auth(client):
    resp = client.get("/skills")
    assert resp.status_code == 401


def test_create_skill_requires_auth(client):
    resp = client.post("/skills", json={"name": "X", "tool_ids": []})
    assert resp.status_code == 401


# ── Built-in skills ───────────────────────────────────────────────────────────

def test_list_skills_includes_builtins(client, auth_headers):
    resp = client.get("/skills", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    builtins = [s for s in data["skills"] if s["is_builtin"]]
    assert len(builtins) == 5
    names = {s["name"] for s in builtins}
    assert {"Research", "Code", "Writing", "Analysis", "Math"} == names


def test_builtin_skill_tool_ids_are_valid(client, auth_headers):
    resp = client.get("/skills", headers=auth_headers)
    assert resp.status_code == 200
    for skill in resp.json()["skills"]:
        if skill["is_builtin"]:
            for tid in skill["tool_ids"]:
                assert tid in ALLOWED_TOOL_IDS, f"Built-in skill '{skill['name']}' has invalid tool_id: {tid}"


def test_get_builtin_skill_by_id(client, auth_headers):
    builtin_id = next(iter(BUILTIN_SKILL_IDS))
    resp = client.get(f"/skills/{builtin_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_builtin"] is True
    assert data["id"] == builtin_id


def test_builtin_skill_cannot_be_edited(client, auth_headers):
    builtin_id = next(iter(BUILTIN_SKILL_IDS))
    resp = client.patch(f"/skills/{builtin_id}", json={"name": "Hacked"}, headers=auth_headers)
    assert resp.status_code == 403


def test_builtin_skill_cannot_be_deleted(client, auth_headers):
    builtin_id = next(iter(BUILTIN_SKILL_IDS))
    resp = client.delete(f"/skills/{builtin_id}", headers=auth_headers)
    assert resp.status_code == 403


# ── Custom skills ─────────────────────────────────────────────────────────────

def test_create_custom_skill(client, auth_headers):
    resp = client.post(
        "/skills",
        json={"name": "Searcher", "tool_ids": ["web_search"], "description": "Web only", "icon_emoji": "🔎"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Searcher"
    assert data["tool_ids"] == ["web_search"]
    assert data["is_builtin"] is False
    assert data["icon_emoji"] == "🔎"


def test_create_custom_skill_with_empty_tool_ids(client, auth_headers):
    resp = client.post("/skills", json={"name": "Writer", "tool_ids": []}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["tool_ids"] == []


def test_create_custom_skill_rejects_unknown_tool_id(client, auth_headers):
    resp = client.post(
        "/skills",
        json={"name": "Bad", "tool_ids": ["unknown_tool", "another_fake"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "unknown_tool" in resp.json()["detail"] or "another_fake" in resp.json()["detail"]


def test_create_custom_skill_rejects_fabricated_tool_id(client, auth_headers):
    """Ensure users cannot reference tools that don't exist in the system."""
    resp = client.post(
        "/skills",
        json={"name": "Evil", "tool_ids": ["delete_database", "exfiltrate_secrets"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_list_skills_shows_custom_and_builtin(client, auth_headers):
    _create_skill(client, auth_headers, name="My Custom")
    resp = client.get("/skills", headers=auth_headers)
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["skills"]]
    assert "My Custom" in names
    assert "Research" in names


def test_custom_skill_isolated_between_users(client, auth_headers):
    _create_skill(client, auth_headers, name="UserASkill")
    headers_b = _register_and_login(client, email="skill_b@test.com")
    resp = client.get("/skills", headers=headers_b)
    custom = [s for s in resp.json()["skills"] if not s["is_builtin"]]
    assert len(custom) == 0


def test_update_custom_skill(client, auth_headers):
    skill = _create_skill(client, auth_headers, name="Old Name")
    resp = client.patch(f"/skills/{skill['id']}", json={"name": "New Name"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"


def test_update_custom_skill_rejects_invalid_tool_id(client, auth_headers):
    skill = _create_skill(client, auth_headers)
    resp = client.patch(f"/skills/{skill['id']}", json={"tool_ids": ["fake_tool"]}, headers=auth_headers)
    assert resp.status_code == 422


def test_delete_custom_skill(client, auth_headers):
    skill = _create_skill(client, auth_headers)
    resp = client.delete(f"/skills/{skill['id']}", headers=auth_headers)
    assert resp.status_code == 204
    # Confirm it's gone
    resp = client.get(f"/skills/{skill['id']}", headers=auth_headers)
    assert resp.status_code == 404


def test_other_user_cannot_access_custom_skill(client, auth_headers):
    skill = _create_skill(client, auth_headers)
    headers_b = _register_and_login(client, email="skill_intruder@test.com")
    resp = client.get(f"/skills/{skill['id']}", headers=headers_b)
    assert resp.status_code == 404
