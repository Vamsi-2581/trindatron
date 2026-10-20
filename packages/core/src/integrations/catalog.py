"""What TRINDATRON can plug into, and the real state of each piece.

Three kinds, deliberately kept apart:

  PLUGIN     a capability TRINDATRON itself runs for the person — web search,
             the browser, files in the workspace, code in a sandbox, a virtual
             computer. Each is a group of real agent tools (trinity_agent), with
             the permissions they need and the actions that wait for approval
             (trinity_agent.permissions.manifest). A person can switch a plugin
             off; switching one on can never grant more than the plan and the
             manifest allow.
  CONNECTOR  a link to a service or data source outside TRINDATRON (storage,
             calendar, email, a search provider) that needs the person's own
             authorization. The connector kinds exist in src/connectors/ with no
             concrete adapter yet, so every connector reports that honestly.
  EXTENSION  something that extends the application rather than the AI (a
             desktop companion, a browser extension). None is distributed today.

Status values are the same everywhere: "available" (on for this person),
"disabled" (the person switched it off), "plan" (not in their plan),
"unavailable" (in the plan, not reachable here right now), "not_available"
(not integrated yet). Nothing here names a vendor or a model.
"""
from __future__ import annotations

from dataclasses import dataclass

from trinity_agent.permissions.manifest import DEFAULT_MANIFEST, PermissionLevel


@dataclass(frozen=True)
class PluginSpec:
    id: str
    tools: tuple[str, ...]
    capability: str  # key in the capability matrix (src/capability/matrix.py)
    permissions: tuple[str, ...]


PLUGINS: tuple[PluginSpec, ...] = (
    PluginSpec("web_search", ("web_search",), "agent", ("read_public_web",)),
    PluginSpec("browser", ("browser_navigate", "browser_get_text", "browser_screenshot", "browser_click", "browser_fill"),
               "browser_control", ("open_websites", "read_pages", "click_and_fill")),
    PluginSpec("files", ("read_file", "list_directory", "write_file"), "agent", ("read_workspace_files",
                                                                                 "write_workspace_files")),
    PluginSpec("code_execution", ("execute_shell",), "code_execution", ("run_commands_in_sandbox",)),
    PluginSpec("computer", ("desktop_screenshot", "desktop_click", "desktop_double_click", "desktop_right_click",
                            "desktop_type", "desktop_key", "desktop_scroll", "desktop_drag", "desktop_move",
                            "desktop_open", "desktop_get_windows"),
               "computer_control", ("see_virtual_screen", "control_virtual_computer")),
)
PLUGIN_IDS = frozenset(plugin.id for plugin in PLUGINS)
_TOOL_TO_PLUGIN = {tool: plugin.id for plugin in PLUGINS for tool in plugin.tools}

CONNECTOR_KINDS: tuple[str, ...] = ("storage", "calendar", "email", "search")


def plugin_for_tool(tool_name: str) -> str | None:
    return _TOOL_TO_PLUGIN.get(tool_name)


def disabled_tool_names(disabled_plugins: set[str]) -> set[str]:
    return {tool for plugin in PLUGINS if plugin.id in disabled_plugins for tool in plugin.tools}


def approval_actions(plugin: PluginSpec) -> list[str]:
    return [tool for tool in plugin.tools if DEFAULT_MANIFEST.get(tool) is PermissionLevel.CONFIRM]


def _connector_adapters() -> dict[str, list[str]]:
    """Concrete connector adapters registered in this process, by kind. None exist yet (src/connectors/)."""
    return {kind: [] for kind in CONNECTOR_KINDS}


def integrations_view(matrix: dict[str, dict], disabled_plugins: set[str]) -> dict:
    plugins = []
    for plugin in PLUGINS:
        state = matrix.get(plugin.capability) or {}
        matrix_status = state.get("status", "not_integrated")
        if matrix_status == "not_integrated":
            status = "not_available"
        elif matrix_status == "plan":
            status = "plan"
        elif matrix_status != "available":
            status = "unavailable"
        elif plugin.id in disabled_plugins:
            status = "disabled"
        else:
            status = "available"
        plugins.append({
            "id": plugin.id,
            "status": status,
            "enabled": plugin.id not in disabled_plugins,
            "permissions": list(plugin.permissions),
            "approval_required": approval_actions(plugin),
            "scope": state.get("scope"),
        })
    adapters = _connector_adapters()
    connectors = [{"id": kind, "status": "available" if adapters[kind] else "not_available", "connected": False}
                  for kind in CONNECTOR_KINDS]
    return {"plugins": plugins, "connectors": connectors, "extensions": []}
