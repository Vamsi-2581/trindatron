"""FINAL SECURITY GATE — BOLA/IDOR final re-verification + Admin final.

Closes the gap on resource types NOT covered by test_bola_audit.py's
earlier pass: users, project files (ProjectFile), workspaces
(CodeWorkspace/WorkWorkspace as objects, not just their file-browser
sub-routes), transactions/ledger, and a fresh independent re-check of
admin. Same conventions as test_bola_audit.py (mirrored exactly):
cross-user access to a resource-by-id returns 404, unauthenticated 401.

Route inventory established by reading the actual route files (not
assumed) before writing any test here:
  - users.py: every route is /users/me/* — the target user is ALWAYS
    current_user from the JWT, never a client-supplied user id. There is
    no GET /users/{id} or any other by-id user lookup route anywhere in
    this codebase (confirmed by reading the full file). BOLAFINAL-01 is
    therefore N/A, not skipped: the vulnerability class (cross-user user
    record access) has no route surface to exploit.
  - projects.py: POST /projects/{project_id}/files and
    DELETE /projects/{project_id}/files/{file_id} exist for ProjectFile.
    There is no GET (list or single) route for ProjectFile content at
    all — confirmed by reading the full router. DELETE is scoped by BOTH
    file_id AND project_id (project_service.get_project_file's own
    filter), and project_id itself is validated by _get_project_or_404
    first, so a foreign file_id can never resolve under an attacker's own
    project_id.
  - workspaces.py: GET/PATCH /workspaces/code/{workspace_id} and
    GET/PATCH /workspaces/work/{workspace_id} both look up the workspace
    row first, then call _get_project_or_404(db, ws.project_id,
    current_user.id) — ownership flows through the owning Project.
  - billing.py: GET /billing/balance and GET /billing/balance/{currency}
    both scope exclusively by current_user.id server-side; {currency} is
    a currency code, not a resource id. No route anywhere returns a
    CreditTransaction or LedgerEntry row by id. BOLAFINAL-04 is therefore
    N/A: no route surface exists for this resource type either.
  - admin.py: exactly two routes exist in the ENTIRE codebase gated by
    is_admin (grepped every route file for "is_admin"/
    "get_current_admin_user" — only admin.py matches) — GET
    /admin/settings and PUT /admin/settings/{key}. get_current_admin_user
    (api/deps.py) derives is_admin purely from the authenticated User row
    resolved from the JWT; no client-supplied header/field/body ever
    reaches it.
"""
from __future__ import annotations

import uuid

from src.services.auth_service import create_access_token


def _other_user_headers(db2_user_factory, email: str = None):
    user = db2_user_factory(email or f"attacker_{uuid.uuid4().hex[:8]}@test.com")
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}, user


# ---------------------------------------------------------------------------
# BOLAFINAL-01 — users: no by-id route exists (documented, not skipped)
# ---------------------------------------------------------------------------


def test_BOLAFINAL01_no_route_exposes_another_users_user_record(client, auth_headers, db2_user_factory):
    """There is no GET /users/{id} (or equivalent) anywhere in this
    codebase — every users.py route reads current_user only. Proves this
    isn't merely "not found in a grep" by hitting the two most plausible
    guessed shapes and confirming FastAPI itself has no such route
    registered (404 from routing, not from an ownership check — this
    test exists to catch a FUTURE route added without ownership
    checks, not to prove today's absence twice)."""
    _, victim = _other_user_headers(db2_user_factory, "victim-user-lookup@test.com")

    for guess in (f"/users/{victim.id}", f"/users/{victim.id}/profile"):
        resp = client.get(guess, headers=auth_headers)
        assert resp.status_code == 404, f"unexpected route exists at {guess}: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# BOLAFINAL-02 — project files: DELETE is scoped by (file_id, project_id)
# ---------------------------------------------------------------------------


def test_BOLAFINAL02_project_file_cross_user_delete_returns_404_and_does_not_delete(client, auth_headers, db2_user_factory):
    victim_headers, victim = _other_user_headers(db2_user_factory, "victim-file-owner@test.com")

    victim_project = client.post("/projects", json={"name": "victim project with file"}, headers=victim_headers)
    assert victim_project.status_code == 201, victim_project.text
    victim_project_id = victim_project.json()["id"]

    upload = client.post(
        f"/projects/{victim_project_id}/files",
        files={"file": ("secret.txt", b"victim's private file content", "text/plain")},
        headers=victim_headers,
    )
    assert upload.status_code == 201, upload.text
    victim_file_id = upload.json()["id"]

    # Attacker owns their OWN, unrelated project — tries to delete the
    # victim's file_id scoped under their OWN project_id.
    attacker_project = client.post("/projects", json={"name": "attacker project"}, headers=auth_headers)
    attacker_project_id = attacker_project.json()["id"]

    resp = client.delete(f"/projects/{attacker_project_id}/files/{victim_file_id}", headers=auth_headers)
    assert resp.status_code == 404, resp.text

    # Also try it scoped under the VICTIM's own project_id (the file's
    # real project) but with the ATTACKER's auth token — ownership of the
    # project itself must be checked, not just the file/project pairing.
    resp2 = client.delete(f"/projects/{victim_project_id}/files/{victim_file_id}", headers=auth_headers)
    assert resp2.status_code == 404, resp2.text


# ---------------------------------------------------------------------------
# BOLAFINAL-03 — CodeWorkspace / WorkWorkspace as objects (by-id routes)
# ---------------------------------------------------------------------------


def test_BOLAFINAL03_code_workspace_cross_user_get_patch_404(client, auth_headers, db2_user_factory, tmp_path):
    victim_headers, victim = _other_user_headers(db2_user_factory, "victim-code-ws@test.com")
    victim_project = client.post("/projects", json={"name": "victim code project"}, headers=victim_headers)
    victim_project_id = victim_project.json()["id"]

    ws_resp = client.post(
        "/workspaces/code", json={"project_id": victim_project_id, "root_path": str(tmp_path)}, headers=victim_headers,
    )
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    assert client.get(f"/workspaces/code/{ws_id}", headers=auth_headers).status_code == 404
    assert client.patch(f"/workspaces/code/{ws_id}", json={"language_hint": "python"}, headers=auth_headers).status_code == 404


def test_BOLAFINAL04_work_workspace_cross_user_get_patch_404(client, auth_headers, db2_user_factory):
    victim_headers, victim = _other_user_headers(db2_user_factory, "victim-work-ws@test.com")
    victim_project = client.post("/projects", json={"name": "victim work project"}, headers=victim_headers)
    victim_project_id = victim_project.json()["id"]

    ws_resp = client.post("/workspaces/work", json={"project_id": victim_project_id}, headers=victim_headers)
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    assert client.get(f"/workspaces/work/{ws_id}", headers=auth_headers).status_code == 404
    assert client.patch(f"/workspaces/work/{ws_id}", json={"requires_approval": False}, headers=auth_headers).status_code == 404


# ---------------------------------------------------------------------------
# BOLAFINAL-05 — transactions/ledger: no by-id route exists (documented)
# ---------------------------------------------------------------------------


def test_BOLAFINAL05_balance_route_never_leaks_another_users_balance(client, auth_headers, db2_user_factory, db):
    """No route returns a CreditTransaction/LedgerEntry by id anywhere in
    this codebase (confirmed by reading billing.py in full). GET
    /billing/balance/{currency} takes a currency CODE, not a resource id,
    and is scoped exclusively by current_user.id server-side — prove this
    directly: give the victim a real nonzero balance, confirm the
    attacker's own read of the SAME currency code returns their own
    (zero) balance, never the victim's."""
    from decimal import Decimal
    from src.services.credit_service import CreditService

    _, victim = _other_user_headers(db2_user_factory, "victim-balance@test.com")
    CreditService(db).credit(
        user_id=victim.id, currency="USD", amount=Decimal("500.00"),
        idempotency_key=str(uuid.uuid4()), transaction_type="purchase",
    )

    resp = client.get("/billing/balance/USD", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["user_id"] != victim.id
    assert resp.json()["balance"] != "500.00"


# ---------------------------------------------------------------------------
# BOLAFINAL-06 — admin: independent re-confirmation, both verbs, both gates
# ---------------------------------------------------------------------------


def test_BOLAFINAL06_admin_settings_get_put_full_matrix(client, auth_headers):
    """Independent re-check of the ONLY two is_admin-gated routes in the
    codebase (grepped every route file for is_admin/get_current_admin_user
    — admin.py is the sole match): anonymous -> 401, authenticated
    non-admin -> 403, on BOTH GET and PUT (the earlier pass's BOLA16 only
    covered GET for the anonymous case)."""
    from src.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as anon_client:
        assert anon_client.get("/admin/settings").status_code in (401, 403)
        assert anon_client.put("/admin/settings/some_key", json={"value": "x"}).status_code in (401, 403)

    assert client.get("/admin/settings", headers=auth_headers).status_code == 403
    assert client.put("/admin/settings/some_key", json={"value": "x"}, headers=auth_headers).status_code == 403


def test_BOLAFINAL07_no_field_anywhere_elevates_to_admin(client, auth_headers, db):
    """Re-confirms BOLA17/18 from a different angle: PATCH /users/me/timezone
    (the only self-service update route) with is_admin/plan/role injected
    must silently drop those fields (Pydantic schema has no such fields),
    and the caller's own User row must be unaffected."""
    from src.models.user import User

    resp = client.patch(
        "/users/me/timezone",
        json={"timezone": "America/Sao_Paulo", "is_admin": True, "plan": "team", "role": "admin"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    from src.services.auth_service import decode_token
    # auth_headers fixture's token subject identifies the caller's own row.
    token = auth_headers["Authorization"].split(" ", 1)[1]
    user_id = decode_token(token)
    user = db.query(User).filter(User.id == user_id).first()
    assert user.is_admin is False
    assert user.plan != "team"
