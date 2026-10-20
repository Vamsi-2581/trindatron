r"""RED TEAM — Sandbox/Filesystem/SSRF audit (one work-stream of the
FINAL SECURITY HARDENING + RED TEAM mission).

Findings are documented inline above each test. Report-only file — no
src/** changes made by this fork; a separate synthesis pass applies fixes
based on these results.

SANDBOX-01 (CRITICAL, test-proven below): POST /workspaces/code accepts
a client-supplied `root_path` with ZERO validation (schemas/project.py's
CreateCodeWorkspaceRequest.root_path has no validator; project_service.py's
create_code_workspace() persists it verbatim). GET /workspaces/code/{id}/files
and GET /workspaces/code/{id}/files/{filepath} (project_service.py's
list_workspace_files/read_workspace_file) then treat that root_path as a
trusted boundary — they only stop the *subpath* from escaping root_path
(_resolve_safe_path's Path.resolve()+relative_to() check, which IS correct
in isolation), but never validate that root_path ITSELF is a safe location
to begin with. An authenticated user who owns nothing but their own project
can set root_path to any absolute host path (their own home directory, a
drive root, this very repository's working directory) and then browse/read
ANY file the server process can read — .env files, other projects' source,
SSH keys, credentials — entirely outside the Docker sandbox's own isolation
(the sandbox's `_workspace_root_is_safe()` denylist in code_service.py is
NEVER consulted by the file-browser routes, only by the shell-execute
routes). Also confirmed: UpdateCodeWorkspaceRequest lets an already-created
workspace's root_path be re-pointed post-creation via PATCH, with the same
zero validation.

SANDBOX-02 (LOW, code-inspection-only): code_service.py's own
_workspace_root_is_safe() denylist (_BLOCKED_SYSTEM_PATHS) is POSIX-only
(/etc, /usr, /bin, /root, /proc, /sys, /dev, /boot, /var/run, /var/log,
/lib, /lib64) and does not block ANY Windows path (C:\Windows, C:\Users\*,
a bare drive root C:\, UNC paths), the current user's own home directory,
/home or /Users, or simply this repository's own working directory (which
contains .env / .git). A denylist is inherently incomplete; even on the
intended Linux-prod target it misses /home. This is the SAME underlying
"client picks root_path" design as SANDBOX-01, just for the EXECUTE path,
which at least attempts a check (whereas the file-browser path attempts
none at all).

SANDBOX-03 (LOW, code-inspection-only): execution_service.py's
build_execution_provider()-equivalent constructs
`DockerExecutionProvider(settings.agent_workspace_root)` without a
`sandbox=` argument, so it always falls back to `DockerSandbox()` with its
HARDCODED defaults (256m/0.5cpu/64 pids) — settings.agent_sandbox_memory_
limit/cpu_limit/pids_limit (wired correctly in agent_service.py and
code_service.py) are silently ignored for the Durable Execution Runtime's
"shell" job_type path. Not an exploitable bypass (a real, non-zero limit
IS still enforced) — a configuration-drift/correctness bug, not a security
hole. Documented for completeness per the mission's exhaustiveness
requirement.

SSRF: no code path was found where an agent tool, webhook, or API route
accepts an arbitrary URL and performs a server-side fetch against it.
NOWPayments/Binance Pay adapters' outbound calls use a FIXED base_url from
Settings (never client-supplied) — confirmed by reading
src/billing/providers/nowpayments_adapter.py and binance_pay_adapter.py
again in this pass; no `base_url` override is ever accepted from a request
body. No "browser agent" or "fetch this URL" tool exists in
packages/core/src (grep for "browser" tool implementations and any
`httpx.get`/`requests.get` fed by a request field turned up none beyond the
two payment adapters). Classified N/A — this codebase currently has no
user-influenced outbound-URL surface to attack. If a URL-accepting tool is
added later, it MUST validate after DNS resolution (block private/loopback/
link-local ranges post-resolution, not by string-matching the hostname) —
noted for future work, not a current finding.
"""
from __future__ import annotations


def _create_project(client, auth_headers, name: str = "sandbox-audit-project") -> str:
    resp = client.post("/projects", json={"name": name}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_code_workspace(client, auth_headers, project_id: str, root_path: str) -> str:
    resp = client.post(
        "/workspaces/code",
        json={"project_id": project_id, "root_path": root_path},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_SANDBOX01_arbitrary_root_path_rejected_by_default(client, auth_headers, tmp_path, monkeypatch):
    """FIX regression test (SANDBOX-01, CRITICAL): before the fix, root_path
    was accepted with zero validation and the file browser happily served
    an arbitrary host directory's contents. After the fix
    (code_workspace_arbitrary_root_enabled defaults to False — see
    config.py/project_service.py), setting a custom root_path at all must
    be rejected on a production-style deployment, so the file browser
    never even gets a workspace to browse."""
    from src.config import settings
    monkeypatch.setattr(settings, "code_workspace_arbitrary_root_enabled", False)

    outside_dir = tmp_path / "not_the_users_workspace_at_all"
    outside_dir.mkdir()
    (outside_dir / "totally-unrelated-secret.txt").write_text("this file belongs to no project and no user\n", encoding="utf-8")

    project_id = _create_project(client, auth_headers)
    resp = client.post(
        "/workspaces/code", json={"project_id": project_id, "root_path": str(outside_dir)}, headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


def test_SANDBOX01b_root_path_cannot_be_set_via_patch_either(client, auth_headers, tmp_path, monkeypatch):
    """FIX regression test — the UPDATE path must be closed too, not just
    creation. A workspace created WITHOUT a root_path (allowed either way)
    must not be re-pointable at an arbitrary directory via PATCH."""
    from src.config import settings

    project_id = _create_project(client, auth_headers, name="repoint-audit-project")
    ws_id = _create_code_workspace_no_root(client, auth_headers, project_id)

    monkeypatch.setattr(settings, "code_workspace_arbitrary_root_enabled", False)
    outside_dir = tmp_path / "repointed_target"
    outside_dir.mkdir()
    (outside_dir / "reachable_after_patch.txt").write_text("reached via PATCH root_path\n", encoding="utf-8")

    patch_resp = client.patch(
        f"/workspaces/code/{ws_id}", json={"root_path": str(outside_dir)}, headers=auth_headers,
    )
    assert patch_resp.status_code == 422, patch_resp.text


def test_SANDBOX01c_own_app_directory_blocked_even_when_arbitrary_root_enabled(client, auth_headers):
    """Defense-in-depth (SANDBOX-01/SANDBOX-02): even when an operator
    deliberately enables arbitrary root_path for local/desktop use, the
    application's own working directory (where .env/secrets live) must
    still be unconditionally blocked by the hardened
    _workspace_root_is_safe() denylist — this is the layer that protects
    a desktop-style deployment even with the feature turned on."""
    import os as _os

    project_id = _create_project(client, auth_headers, name="own-app-dir-audit-project")
    ws_id = _create_code_workspace(client, auth_headers, project_id, root_path=str(_os.getcwd()))

    list_resp = client.get(f"/workspaces/code/{ws_id}/files", headers=auth_headers)
    assert list_resp.status_code == 403, list_resp.text


def test_SANDBOX01d_production_hard_blocks_arbitrary_root_even_if_flag_is_true(client, auth_headers, tmp_path, monkeypatch):
    """FIX regression test (SANDBOX-01, final gate): production is an
    UNCONDITIONAL hard block on root_path — even if an operator
    accidentally sets CODE_WORKSPACE_ARBITRARY_ROOT_ENABLED=true in a
    production environment (env var copy-paste, shared .env template),
    the deployment must still refuse it. This must hold independent of
    the flag's value."""
    from src.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "code_workspace_arbitrary_root_enabled", True)  # misconfigured on purpose

    project_id = _create_project(client, auth_headers, name="prod-hard-block-audit-project")
    resp = client.post(
        "/workspaces/code", json={"project_id": project_id, "root_path": str(tmp_path)}, headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


def _create_code_workspace_no_root(client, auth_headers, project_id: str) -> str:
    resp = client.post("/workspaces/code", json={"project_id": project_id}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]
