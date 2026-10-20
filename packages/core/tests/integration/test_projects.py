"""
Integration tests for the Projects / Workspaces feature (Phase D).
"""
from __future__ import annotations

import io
from unittest.mock import patch

from trinity_router import RouterStreamChunk


# ── helpers ───────────────────────────────────────────────────────────────────

def _register_and_login(client, email="proj@test.com", password="securepass123"):
    resp = client.post("/auth/register", json={"email": email, "password": password, "full_name": "T"})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, name="My Project", description=None, instructions=None):
    body = {"name": name}
    if description:
        body["description"] = description
    if instructions:
        body["instructions"] = instructions
    resp = client.post("/projects", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stream_mock(texts=None, cost_usd=0.0, tcu=0.0):
    texts = texts or ["Hello!"]
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-test",
        input_tokens=5, output_tokens=5, cached_tokens=0,
        cost_usd=cost_usd, tcu=tcu,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


# ── Projects CRUD ─────────────────────────────────────────────────────────────

def test_create_project(client, auth_headers):
    resp = client.post("/projects", json={"name": "Alpha", "description": "desc", "instructions": "Be concise"}, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Alpha"
    assert data["description"] == "desc"
    assert data["instructions"] == "Be concise"
    assert data["is_archived"] is False
    assert "id" in data


def test_create_project_requires_auth(client):
    resp = client.post("/projects", json={"name": "X"})
    assert resp.status_code == 401


def test_list_projects(client, auth_headers):
    _create_project(client, auth_headers, "P1")
    _create_project(client, auth_headers, "P2")
    resp = client.get("/projects", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = [p["name"] for p in data["projects"]]
    assert "P1" in names and "P2" in names


def test_list_projects_isolated_between_users(client, auth_headers):
    _create_project(client, auth_headers, "UserAProject")
    headers_b = _register_and_login(client, email="userb_proj@test.com")
    resp = client.get("/projects", headers=headers_b)
    assert resp.json()["total"] == 0


def test_get_project(client, auth_headers):
    project = _create_project(client, auth_headers, "GetMe")
    resp = client.get(f"/projects/{project['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "GetMe"


def test_other_user_cannot_get_project(client, auth_headers):
    project = _create_project(client, auth_headers)
    headers_b = _register_and_login(client, email="intruder_proj@test.com")
    resp = client.get(f"/projects/{project['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_update_project(client, auth_headers):
    project = _create_project(client, auth_headers, "Old Name")
    resp = client.patch(
        f"/projects/{project['id']}",
        json={"name": "New Name", "instructions": "Updated instructions"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "New Name"
    assert data["instructions"] == "Updated instructions"


def test_archive_project(client, auth_headers):
    project = _create_project(client, auth_headers, "ToArchive")
    resp = client.delete(f"/projects/{project['id']}", headers=auth_headers)
    assert resp.status_code == 204

    # Archived project should not appear in the list
    list_resp = client.get("/projects", headers=auth_headers)
    assert list_resp.json()["total"] == 0

    # But can still be fetched directly (soft delete)
    get_resp = client.get(f"/projects/{project['id']}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["is_archived"] is True


# ── Project Files ─────────────────────────────────────────────────────────────

def test_upload_file_to_project(client, auth_headers):
    project = _create_project(client, auth_headers)
    content = b"hello world file content"
    resp = client.post(
        f"/projects/{project['id']}/files",
        files={"file": ("test.txt", io.BytesIO(content), "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test.txt"
    assert data["mime_type"] == "text/plain"
    assert data["size_bytes"] == len(content)


def test_project_detail_includes_files(client, auth_headers):
    project = _create_project(client, auth_headers)
    client.post(
        f"/projects/{project['id']}/files",
        files={"file": ("doc.txt", io.BytesIO(b"contents"), "text/plain")},
        headers=auth_headers,
    )
    resp = client.get(f"/projects/{project['id']}", headers=auth_headers)
    assert len(resp.json()["files"]) == 1
    assert resp.json()["files"][0]["name"] == "doc.txt"


def test_delete_project_file(client, auth_headers):
    project = _create_project(client, auth_headers)
    upload_resp = client.post(
        f"/projects/{project['id']}/files",
        files={"file": ("to_delete.txt", io.BytesIO(b"bye"), "text/plain")},
        headers=auth_headers,
    )
    file_id = upload_resp.json()["id"]

    del_resp = client.delete(f"/projects/{project['id']}/files/{file_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    resp = client.get(f"/projects/{project['id']}", headers=auth_headers)
    assert len(resp.json()["files"]) == 0


def test_upload_file_requires_project_ownership(client, auth_headers):
    project = _create_project(client, auth_headers)
    headers_b = _register_and_login(client, email="intruder_file@test.com")
    resp = client.post(
        f"/projects/{project['id']}/files",
        files={"file": ("bad.txt", io.BytesIO(b"evil"), "text/plain")},
        headers=headers_b,
    )
    assert resp.status_code == 404


# ── Sessions linked to Project ────────────────────────────────────────────────

def test_create_session_with_project_id(client, auth_headers):
    project = _create_project(client, auth_headers)
    resp = client.post("/sessions", json={"project_id": project["id"]}, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == project["id"]


def test_list_project_sessions(client, auth_headers):
    project = _create_project(client, auth_headers)
    # Create 2 sessions linked to project, 1 unlinked
    client.post("/sessions", json={"project_id": project["id"]}, headers=auth_headers)
    client.post("/sessions", json={"project_id": project["id"]}, headers=auth_headers)
    client.post("/sessions", json={}, headers=auth_headers)

    resp = client.get(f"/projects/{project['id']}/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def test_project_context_injected_in_chat(client, auth_headers):
    """When a session belongs to a project with instructions, they appear in the system prompt."""
    instructions = "Always respond in exactly 3 words."
    project = _create_project(client, auth_headers, instructions=instructions)
    session_resp = client.post("/sessions", json={"project_id": project["id"]}, headers=auth_headers)
    session_id = session_resp.json()["id"]

    captured_messages = []

    async def _capturing_stream(request):
        captured_messages.extend(request.messages)
        yield RouterStreamChunk(text="Three word reply")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=3, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    with patch("src.services.chat_service._router.stream", _capturing_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=auth_headers)

    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert instructions in system_messages[0]["content"]


def test_chat_without_project_unaffected(client, auth_headers):
    """Sessions without a project do not inject any extra context."""
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

    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert "Project Context" not in system_messages[0]["content"]


# ── Workspaces (stubs) ────────────────────────────────────────────────────────

def test_create_code_workspace_stub(client, auth_headers):
    project = _create_project(client, auth_headers)
    resp = client.post(
        "/workspaces/code",
        json={"project_id": project["id"], "language_hint": "python", "test_command": "pytest"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == project["id"]
    assert data["language_hint"] == "python"
    assert data["test_command"] == "pytest"
    assert data["git_enabled"] is False


def test_create_code_workspace_requires_project_ownership(client, auth_headers):
    project = _create_project(client, auth_headers)
    headers_b = _register_and_login(client, email="intruder_ws@test.com")
    resp = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=headers_b)
    assert resp.status_code == 404


def test_create_work_workspace_stub(client, auth_headers):
    project = _create_project(client, auth_headers)
    resp = client.post(
        "/workspaces/work",
        json={"project_id": project["id"], "requires_approval": True, "approval_policy": "always"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == project["id"]
    assert data["requires_approval"] is True
    assert data["approval_policy"] == "always"


def test_get_code_workspace(client, auth_headers):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers).json()
    resp = client.get(f"/workspaces/code/{ws['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == ws["id"]


def test_update_code_workspace(client, auth_headers):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers).json()
    resp = client.patch(
        f"/workspaces/code/{ws['id']}",
        json={"language_hint": "typescript", "git_enabled": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["language_hint"] == "typescript"
    assert resp.json()["git_enabled"] is True


def test_duplicate_code_workspace_rejected(client, auth_headers):
    project = _create_project(client, auth_headers)
    client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers)
    resp = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers)
    assert resp.status_code == 409


def test_workspace_requires_auth(client):
    resp = client.get("/workspaces/code/nonexistent")
    assert resp.status_code == 401


def test_get_project_code_workspace_returns_workspace(client, auth_headers):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers).json()
    resp = client.get(f"/projects/{project['id']}/workspace/code", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == ws["id"]
    assert data["project_id"] == project["id"]


def test_get_project_code_workspace_returns_404_when_none(client, auth_headers):
    project = _create_project(client, auth_headers)
    resp = client.get(f"/projects/{project['id']}/workspace/code", headers=auth_headers)
    assert resp.status_code == 404


def test_get_project_code_workspace_requires_auth(client, auth_headers):
    project = _create_project(client, auth_headers)
    resp = client.get(f"/projects/{project['id']}/workspace/code")
    assert resp.status_code == 401


def test_get_project_code_workspace_not_accessible_by_other_user(client):
    h1 = _register_and_login(client, "ws_owner@test.com")
    h2 = _register_and_login(client, "ws_other@test.com")
    project = _create_project(client, h1)
    client.post("/workspaces/code", json={"project_id": project["id"]}, headers=h1)
    resp = client.get(f"/projects/{project['id']}/workspace/code", headers=h2)
    assert resp.status_code == 404


# ── File browser tests ────────────────────────────────────────────────────────

def test_list_workspace_files_returns_entries(client, auth_headers, tmp_path):
    project = _create_project(client, auth_headers)
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "subdir").mkdir()
    ws = client.post("/workspaces/code", json={
        "project_id": project["id"],
        "root_path": str(tmp_path),
    }, headers=auth_headers).json()

    resp = client.get(f"/workspaces/code/{ws['id']}/files", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    names = [e["name"] for e in data["entries"]]
    assert "main.py" in names
    assert "subdir" in names
    dirs = [e for e in data["entries"] if e["is_dir"]]
    files = [e for e in data["entries"] if not e["is_dir"]]
    assert any(d["name"] == "subdir" for d in dirs)
    assert any(f["name"] == "main.py" for f in files)


def test_read_workspace_file_returns_content(client, auth_headers, tmp_path):
    project = _create_project(client, auth_headers)
    content = "def hello(): return 'world'"
    (tmp_path / "hello.py").write_text(content)
    ws = client.post("/workspaces/code", json={
        "project_id": project["id"],
        "root_path": str(tmp_path),
    }, headers=auth_headers).json()

    resp = client.get(f"/workspaces/code/{ws['id']}/files/hello.py", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["content"] == content
    assert resp.json()["path"] == "hello.py"


def test_path_traversal_blocked_on_list(client, auth_headers, tmp_path):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={
        "project_id": project["id"],
        "root_path": str(tmp_path),
    }, headers=auth_headers).json()

    resp = client.get(
        f"/workspaces/code/{ws['id']}/files",
        params={"path": "../../etc"},
        headers=auth_headers,
    )
    assert resp.status_code == 403


def test_list_files_without_root_path_returns_422(client, auth_headers):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={"project_id": project["id"]}, headers=auth_headers).json()
    resp = client.get(f"/workspaces/code/{ws['id']}/files", headers=auth_headers)
    assert resp.status_code == 422


def test_read_file_not_found_returns_404(client, auth_headers, tmp_path):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={
        "project_id": project["id"],
        "root_path": str(tmp_path),
    }, headers=auth_headers).json()
    resp = client.get(f"/workspaces/code/{ws['id']}/files/nonexistent.py", headers=auth_headers)
    assert resp.status_code == 404


def test_list_files_requires_auth(client, auth_headers, tmp_path):
    project = _create_project(client, auth_headers)
    ws = client.post("/workspaces/code", json={
        "project_id": project["id"],
        "root_path": str(tmp_path),
    }, headers=auth_headers).json()
    resp = client.get(f"/workspaces/code/{ws['id']}/files")
    assert resp.status_code == 401
