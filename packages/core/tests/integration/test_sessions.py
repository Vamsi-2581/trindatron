def test_create_session_default(client, auth_headers):
    resp = client.post("/sessions", json={}, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "New conversation"
    assert "id" in data
    assert "model_default" not in data, "which model answers is internal and never returned"


def test_create_session_custom(client, auth_headers):
    resp = client.post("/sessions", json={"title": "My project", "model": "claude-3-5-haiku-20241022"}, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "My project"
    # A model a client sends is accepted and ignored; no model name is echoed back.
    assert "model_default" not in data
    assert "claude" not in resp.text.lower()


def test_list_sessions_empty(client, auth_headers):
    resp = client.get("/sessions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert data["total"] == 0


def test_list_sessions_after_create(client, auth_headers):
    client.post("/sessions", json={"title": "Session A"}, headers=auth_headers)
    client.post("/sessions", json={"title": "Session B"}, headers=auth_headers)
    resp = client.get("/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def test_get_session_detail(client, auth_headers):
    create_resp = client.post("/sessions", json={"title": "Detail test"}, headers=auth_headers)
    session_id = create_resp.json()["id"]

    resp = client.get(f"/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["title"] == "Detail test"
    assert data["messages"] == []


def test_get_session_not_found(client, auth_headers):
    resp = client.get("/sessions/nonexistent-id", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_session(client, auth_headers):
    create_resp = client.post("/sessions", json={}, headers=auth_headers)
    session_id = create_resp.json()["id"]

    del_resp = client.delete(f"/sessions/{session_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    get_resp = client.get(f"/sessions/{session_id}", headers=auth_headers)
    assert get_resp.status_code == 404


def test_sessions_isolated_between_users(client, auth_headers):
    # Create session for user 1
    client.post("/sessions", json={"title": "User 1 session"}, headers=auth_headers)

    # Register user 2 and get their token
    reg = client.post("/auth/register", json={"email": "user2@test.com", "password": "password123"})
    token2 = reg.json()["access_token"]
    headers2 = {"Authorization": f"Bearer {token2}"}

    # User 2 should see no sessions
    resp = client.get("/sessions", headers=headers2)
    assert resp.json()["total"] == 0


def test_session_usage_empty(client, auth_headers):
    create_resp = client.post("/sessions", json={}, headers=auth_headers)
    session_id = create_resp.json()["id"]

    resp = client.get(f"/sessions/{session_id}/usage", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 0
    assert data["total_cost_usd"] == 0.0
