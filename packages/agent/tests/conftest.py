import sys
import os
from pathlib import Path

# Make src importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from trinity_agent.permissions.gateway import PermissionGateway
from trinity_agent.permissions.manifest import PermissionLevel
from trinity_agent.security.audit import AuditLog
from trinity_agent.tools.registry import ToolRegistry


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def gateway_auto():
    """Gateway that auto-approves everything."""
    manifest = {name: PermissionLevel.AUTO for name in [
        "read_file", "write_file", "list_directory",
        "execute_shell", "browser_navigate", "browser_get_text",
        "browser_screenshot", "web_search", "desktop_screenshot",
    ]}
    return PermissionGateway(manifest=manifest)


@pytest.fixture()
def gateway_forbidden():
    """Gateway that forbids everything."""
    manifest = {name: PermissionLevel.FORBIDDEN for name in [
        "read_file", "write_file", "list_directory",
        "execute_shell", "browser_navigate", "browser_get_text",
        "browser_screenshot", "web_search", "desktop_screenshot",
    ]}
    return PermissionGateway(manifest=manifest)


@pytest.fixture()
def audit_log() -> AuditLog:
    return AuditLog()
