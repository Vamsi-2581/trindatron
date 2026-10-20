"""Computer use — the virtual desktop, exercised through the product path.

The desktop tools existed in packages/agent for a while without being
reachable: they were never registered in agent_service.py, so no model could
call them and no user could use them. These tests cover the wiring that made
them real, and they go through `_run_tool` — the same dispatch the ReAct loop
uses — rather than constructing the tool objects directly, because "the tool
class works" was already true and was never the thing that was broken.

The container tests are marked `desktop_sandbox` and skip when Docker or the
image is missing. That is a real condition, not a convenience: the sandbox is
environment-dependent, which is exactly why the product checks for it at
runtime instead of assuming.

    docker build -t trinity-desktop-sandbox:latest packages/core/docker/desktop-sandbox/
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from src.services.agent_service import (
    DESKTOP_TOOL_NAMES,
    _AGENT_TOOLS,
    _desktop_available,
    _describe_tool_action,
    _run_tool,
)
from src.services.skill_service import ALLOWED_TOOL_IDS
from trinity_agent.permissions.gateway import PermissionGateway
from trinity_agent.permissions.manifest import PermissionLevel
from trinity_sandbox import DesktopSandbox


# ── Wiring: the part that was actually missing ───────────────────────────

def test_every_desktop_tool_is_offered_to_the_model():
    """The manifest the model sees must contain them, or nothing can call
    them — which was the state of the world before this."""
    names = {t["function"]["name"] for t in _AGENT_TOOLS}
    assert DESKTOP_TOOL_NAMES <= names, DESKTOP_TOOL_NAMES - names


def test_every_desktop_tool_has_a_usable_spec():
    specs = {
        t["function"]["name"]: t["function"]
        for t in _AGENT_TOOLS
        if t["function"]["name"] in DESKTOP_TOOL_NAMES
    }
    assert len(specs) == len(DESKTOP_TOOL_NAMES)
    for name, fn in specs.items():
        assert fn["description"].strip(), name
        params = fn["parameters"]
        assert params["type"] == "object", name
        # Every declared requirement must exist as a property, or the model
        # is being asked for a field the tool does not accept.
        for required in params.get("required", []):
            assert required in params["properties"], f"{name}.{required}"


def test_desktop_tools_are_known_to_the_permission_gate():
    """agent_service only consults the permission manifest for a name in
    ALLOWED_TOOL_IDS. Leaving these out would silently skip their approval
    pause — the tools would run, just without asking."""
    assert DESKTOP_TOOL_NAMES <= ALLOWED_TOOL_IDS


def test_every_desktop_action_requires_approval():
    gateway = PermissionGateway()
    for name in DESKTOP_TOOL_NAMES:
        assert gateway.level(name) is PermissionLevel.CONFIRM, name


def test_clipboard_and_credential_tools_stay_forbidden():
    """These are named in the manifest as FORBIDDEN and have no
    implementation. Nothing in this work should have quietly enabled them."""
    gateway = PermissionGateway()
    for name in (
        "desktop_clipboard_read",
        "desktop_clipboard_write",
        "desktop_type_password",
        "desktop_form_submit",
    ):
        assert gateway.level(name) is PermissionLevel.FORBIDDEN, name
        assert name not in {t["function"]["name"] for t in _AGENT_TOOLS}
        assert name not in ALLOWED_TOOL_IDS


# ── What the user is asked to approve ────────────────────────────────────

def test_approval_text_says_what_will_happen_and_where():
    assert "(200, 150)" in _describe_tool_action("desktop_click", {"x": 200, "y": 150})
    assert "ctrl+s" in _describe_tool_action("desktop_key", {"keys": ["ctrl", "s"]})
    assert "notes.txt" in _describe_tool_action("desktop_open", {"path": "notes.txt"})


def test_typed_text_is_never_shown_in_the_approval_prompt():
    """Same rule browser_fill already follows: the prompt is rendered in the
    UI and written to the activity trail, and typed text can be a password."""
    described = _describe_tool_action("desktop_type", {"text": "hunter2-secret"})
    assert "hunter2" not in described
    assert "14" in described  # the length, so the user still knows the scale


# ── Entitlement gating ───────────────────────────────────────────────────

@pytest.mark.parametrize("plan", ["free", "start"])
def test_plans_without_trinity_work_do_not_get_computer_use(plan):
    """A desktop container is the most expensive thing an agent can ask for,
    so it rides on the existing supports_trinity_work flag rather than a
    newly invented entitlement field."""
    assert asyncio.run(_desktop_available(plan)) is False


# ── Dispatch ─────────────────────────────────────────────────────────────

def test_dispatch_refuses_cleanly_without_a_sandbox():
    """No sandbox is a refusal, never a crash — the model can carry on with
    the tools that do work."""
    with tempfile.TemporaryDirectory() as tmp:
        ok, msg = asyncio.run(
            _run_tool("desktop_click", {"x": 1, "y": 1}, Path(tmp), desktop_sandbox=None)
        )
    assert ok is False
    assert "not available" in msg.lower()


def test_an_unknown_desktop_name_is_still_an_unknown_tool():
    with tempfile.TemporaryDirectory() as tmp:
        ok, msg = asyncio.run(_run_tool("desktop_teleport", {}, Path(tmp)))
    assert ok is False
    assert "unknown tool" in msg.lower()


# ── The real container ───────────────────────────────────────────────────

def _sandbox_available() -> bool:
    try:
        return asyncio.run(DesktopSandbox().image_available())
    except Exception:
        return False


requires_sandbox = pytest.mark.skipif(
    not _sandbox_available(),
    reason="Docker or trinity-desktop-sandbox:latest unavailable",
)


@requires_sandbox
def test_the_virtual_desktop_really_runs_every_tool():
    """The vertical slice, end to end, against a real container.

    Each call goes through `_run_tool`, so what is proven here is that the
    product path works — dispatch, registry, sandbox and container included —
    not merely that the tool classes do.
    """
    async def run() -> list[tuple[str, bool, str]]:
        workspace = Path(tempfile.mkdtemp(prefix="computer-use-test-"))
        sandbox = DesktopSandbox()
        assert await sandbox.start("pytest-computer-use", workspace), "sandbox did not start"
        try:
            assert await sandbox.is_running()
            calls = [
                ("desktop_screenshot", {"filename": "screen.png"}),
                ("desktop_get_windows", {}),
                ("desktop_move", {"x": 200, "y": 150}),
                ("desktop_click", {"x": 200, "y": 150}),
                ("desktop_double_click", {"x": 210, "y": 160}),
                ("desktop_right_click", {"x": 220, "y": 170}),
                ("desktop_type", {"text": "trindatron"}),
                ("desktop_key", {"keys": ["ctrl", "a"]}),
                ("desktop_scroll", {"clicks": 2}),
                ("desktop_drag", {"start_x": 100, "start_y": 100, "end_x": 300, "end_y": 240}),
                ("desktop_open", {"path": "screen.png"}),
            ]
            results = []
            for name, args in calls:
                ok, out = await _run_tool(name, args, workspace, desktop_sandbox=sandbox)
                results.append((name, ok, out))

            shot = workspace / "screen.png"
            assert shot.exists(), "screenshot produced no file"
            assert shot.stat().st_size > 1000, "screenshot file is suspiciously small"
            return results
        finally:
            await sandbox.stop()
            assert not await sandbox.is_running(), "container outlived the test"

    results = asyncio.run(run())
    failed = [(n, out) for n, ok, out in results if not ok]
    assert not failed, f"desktop tools failed: {failed}"
    assert len(results) == len(DESKTOP_TOOL_NAMES)


@requires_sandbox
def test_screenshot_output_is_labelled_as_untrusted_visual_content():
    """Anything the agent sees on a screen is external content, and the
    sanitizer's wrapper is what tells the model not to obey it."""
    async def run() -> str:
        workspace = Path(tempfile.mkdtemp(prefix="computer-use-wrap-"))
        sandbox = DesktopSandbox()
        assert await sandbox.start("pytest-computer-use-wrap", workspace)
        try:
            ok, out = await _run_tool(
                "desktop_screenshot", {"filename": "s.png"}, workspace, desktop_sandbox=sandbox
            )
            assert ok, out
            return out
        finally:
            await sandbox.stop()

    assert "external_visual_content" in asyncio.run(run())
