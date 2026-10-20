"""
Integration tests for Agent Definitions (Phase E).
"""
from __future__ import annotations

from unittest.mock import patch

from trinity_router import RouterStreamChunk

from src.services.skill_service import BUILTIN_SKILL_IDS


# ── helpers ───────────────────────────────────────────────────────────────────

def _register_and_login(client, email="agent_user@test.com", password="securepass123"):
    resp = client.post("/auth/register", json={"email": email, "password": password, "full_name": "T"})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_agent(client, headers, name="My Agent", skill_ids=None, instructions=None):
    body = {"name": name, "skill_ids": skill_ids or []}
    if instructions:
        body["system_instructions"] = instructions
    resp = client.post("/agents", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_project(client, headers, name="Test Project"):
    resp = client.post("/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_custom_skill(client, headers, name="Custom Skill", tool_ids=None):
    resp = client.post("/skills", json={"name": name, "tool_ids": tool_ids or []}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stream_mock():
    async def _stream(request):
        yield RouterStreamChunk(text="Hello")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )
    return _stream


# ── Authentication ─────────────────────────────────────────────────────────────

def test_create_agent_requires_auth(client):
    resp = client.post("/agents", json={"name": "X"})
    assert resp.status_code == 401


def test_list_agents_requires_auth(client):
    resp = client.get("/agents")
    assert resp.status_code == 401


# ── Agent CRUD ────────────────────────────────────────────────────────────────

def test_create_agent_definition(client, auth_headers):
    resp = client.post(
        "/agents",
        json={
            "name": "Research Bot",
            "description": "A research specialist",
            "avatar_emoji": "🤖",
            "system_instructions": "You are a research expert.",
            "model_preference": "power",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Research Bot"
    assert data["avatar_emoji"] == "🤖"
    assert data["model_preference"] == "power"
    assert data["is_archived"] is False
    assert data["skill_ids"] == []
    assert "id" in data


def test_create_agent_with_builtin_skill(client, auth_headers):
    research_skill_id = "00000000-0000-0000-0000-000000000001"
    assert research_skill_id in BUILTIN_SKILL_IDS
    resp = client.post(
        "/agents",
        json={"name": "Researcher", "skill_ids": [research_skill_id]},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert research_skill_id in resp.json()["skill_ids"]


def test_create_agent_with_custom_skill(client, auth_headers):
    skill = _create_custom_skill(client, auth_headers, name="My Skill", tool_ids=["web_search"])
    resp = client.post("/agents", json={"name": "Agent", "skill_ids": [skill["id"]]}, headers=auth_headers)
    assert resp.status_code == 201
    assert skill["id"] in resp.json()["skill_ids"]


def test_create_agent_rejects_skill_from_other_user(client, auth_headers):
    """A user cannot reference a skill owned by someone else."""
    headers_b = _register_and_login(client, email="agent_b@test.com")
    skill_b = _create_custom_skill(client, headers_b, name="BSkill")
    resp = client.post("/agents", json={"name": "X", "skill_ids": [skill_b["id"]]}, headers=auth_headers)
    assert resp.status_code == 422


def test_create_agent_rejects_unknown_skill_id(client, auth_headers):
    resp = client.post(
        "/agents",
        json={"name": "X", "skill_ids": ["nonexistent-skill-id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_create_agent_invalid_model_preference(client, auth_headers):
    resp = client.post(
        "/agents",
        json={"name": "X", "model_preference": "turbo"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_list_agent_definitions(client, auth_headers):
    _create_agent(client, auth_headers, name="A1")
    _create_agent(client, auth_headers, name="A2")
    resp = client.get("/agents", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = {a["name"] for a in data["agents"]}
    assert {"A1", "A2"} == names


def test_agent_definitions_isolated_between_users(client, auth_headers):
    _create_agent(client, auth_headers, name="UserAAgent")
    headers_b = _register_and_login(client, email="agent_isolation@test.com")
    resp = client.get("/agents", headers=headers_b)
    assert resp.json()["total"] == 0


def test_get_agent_definition(client, auth_headers):
    agent = _create_agent(client, auth_headers, name="GetMe")
    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "GetMe"


def test_other_user_cannot_get_agent_definition(client, auth_headers):
    agent = _create_agent(client, auth_headers, name="Private")
    headers_b = _register_and_login(client, email="agent_spy@test.com")
    resp = client.get(f"/agents/{agent['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_update_agent_definition(client, auth_headers):
    agent = _create_agent(client, auth_headers, name="Old")
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"name": "New", "model_preference": "fast"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "New"
    assert data["model_preference"] == "fast"


def test_archive_agent_definition(client, auth_headers):
    agent = _create_agent(client, auth_headers, name="ToArchive")
    resp = client.delete(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 204
    # Should not appear in list
    agents = client.get("/agents", headers=auth_headers).json()["agents"]
    assert not any(a["id"] == agent["id"] for a in agents)
    # But GET still works (archived visible by direct lookup)
    # Note: get_agent_definition does not filter by is_archived, so it returns archived
    direct = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    # Archived agents are returned by GET (they're just excluded from list)
    assert direct.status_code == 200
    assert direct.json()["is_archived"] is True


# ── Project ↔ Agent associations ──────────────────────────────────────────────

def test_add_agent_to_project(client, auth_headers):
    project = _create_project(client, auth_headers)
    agent = _create_agent(client, auth_headers)
    resp = client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == project["id"]
    assert data["agent_definition_id"] == agent["id"]


def test_list_project_agents(client, auth_headers):
    project = _create_project(client, auth_headers)
    agent = _create_agent(client, auth_headers, name="ProjAgent")
    client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    resp = client.get(f"/projects/{project['id']}/agents", headers=auth_headers)
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert agent["id"] in ids


def test_add_agent_requires_project_ownership(client, auth_headers):
    headers_b = _register_and_login(client, email="pa_intruder@test.com")
    project = _create_project(client, auth_headers)
    agent = _create_agent(client, headers_b)
    # User B tries to add their agent to User A's project
    resp = client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_add_agent_from_other_user_to_own_project_fails(client, auth_headers):
    """User A cannot add User B's agent to User A's project."""
    headers_b = _register_and_login(client, email="pa_b_agent@test.com")
    project = _create_project(client, auth_headers)
    agent_b = _create_agent(client, headers_b, name="AgentB")
    resp = client.post(f"/projects/{project['id']}/agents/{agent_b['id']}", headers=auth_headers)
    assert resp.status_code == 404


def test_remove_agent_from_project(client, auth_headers):
    project = _create_project(client, auth_headers)
    agent = _create_agent(client, auth_headers)
    client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    resp = client.delete(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 204
    agents = client.get(f"/projects/{project['id']}/agents", headers=auth_headers).json()
    assert not any(a["id"] == agent["id"] for a in agents)


def test_duplicate_agent_association_is_idempotent(client, auth_headers):
    """Adding the same agent twice to a project doesn't error."""
    project = _create_project(client, auth_headers)
    agent = _create_agent(client, auth_headers)
    client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    resp = client.post(f"/projects/{project['id']}/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 201


# ── Session with Agent Definition ─────────────────────────────────────────────

def test_create_session_with_agent_definition_id(client, auth_headers):
    agent = _create_agent(client, auth_headers)
    resp = client.post(
        "/sessions",
        json={"agent_definition_id": agent["id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["agent_definition_id"] == agent["id"]


def test_session_rejects_agent_from_other_user(client, auth_headers):
    """Creating a session with another user's agent_definition_id must be
    rejected outright (Security Hardening — BOLA-04, HIGH). This test
    previously asserted the OLD, vulnerable "fail-open" behavior (201,
    session created with the foreign id silently accepted) on the mistaken
    assumption that a DIFFERENT ownership check elsewhere would prevent
    any real leak — it didn't: chat_service.py unconditionally reads
    session.agent_definition.system_instructions into the system prompt
    regardless of ownership. session_service.py's create_session() now
    validates project_id/agent_definition_id belong to the requesting
    user before persisting, matching agent_instance_service.py's own
    long-standing pattern."""
    headers_b = _register_and_login(client, email="session_agent_spy@test.com")
    agent_b = _create_agent(client, headers_b, name="AgentB")
    resp = client.post(
        "/sessions",
        json={"agent_definition_id": agent_b["id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ── Context injection ─────────────────────────────────────────────────────────

def test_agent_instructions_injected_in_chat(client, auth_headers):
    """Agent's system_instructions appear in the system prompt."""
    agent = _create_agent(client, auth_headers, instructions="Always answer in Portuguese.")
    session_id = client.post(
        "/sessions",
        json={"agent_definition_id": agent["id"]},
        headers=auth_headers,
    ).json()["id"]

    captured_messages = []

    async def _capturing_stream(request):
        captured_messages.extend(request.messages)
        yield RouterStreamChunk(text="Olá")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    with patch("src.services.chat_service._router.stream", _capturing_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=auth_headers)

    system_msgs = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_msgs) == 1
    assert "Always answer in Portuguese." in system_msgs[0]["content"]


def test_agent_skill_instructions_injected_in_chat(client, auth_headers):
    """Skill instructions are appended to the system prompt when agent has skills."""
    research_skill_id = "00000000-0000-0000-0000-000000000001"
    agent = _create_agent(client, auth_headers, skill_ids=[research_skill_id])
    session_id = client.post(
        "/sessions",
        json={"agent_definition_id": agent["id"]},
        headers=auth_headers,
    ).json()["id"]

    captured_messages = []

    async def _capturing_stream(request):
        captured_messages.extend(request.messages)
        yield RouterStreamChunk(text="Found")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    with patch("src.services.chat_service._router.stream", _capturing_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "Search for AI"}, headers=auth_headers)

    system_msgs = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_msgs) == 1
    # Research skill instructions should be present
    assert "research" in system_msgs[0]["content"].lower()


def test_chat_without_agent_unaffected(client, auth_headers):
    """Sessions without agent_definition_id have exactly the same system prompt as before."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    captured_messages = []

    async def _capturing_stream(request):
        captured_messages.extend(request.messages)
        yield RouterStreamChunk(text="Hi")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=1, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    with patch("src.services.chat_service._router.stream", _capturing_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=auth_headers)

    system_msgs = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_msgs) == 1
    assert "Agent Instructions" not in system_msgs[0]["content"]
    assert "Skill:" not in system_msgs[0]["content"]
